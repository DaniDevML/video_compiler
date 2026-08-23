"""
Real YouTube round-trip benchmark: encode -> upload -> wait -> download -> decode.

This is the measurement the local benchmarks cannot make. Everything else in
bench/ simulates YouTube's transcode with ffmpeg; this one actually puts the
video through the service and checks whether the bytes come back.

Videos are uploaded UNLISTED. They stay on your channel until you delete them.

Results append to bench/youtube_results.json so a size ladder accumulates.

Usage:
    python bench/bench_youtube.py 8            # one size, in MB
    python bench/bench_youtube.py 8 64 256     # a ladder, stops on first failure
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np

import video_codec as vc
import video_encoder as ve
import video_decoder as vd
from youtube_api import (upload_video, download_video, fetch_description,
                         extract_video_id, yt_dlp_command)

HERE = os.path.dirname(os.path.abspath(__file__))
RESULTS = os.path.join(HERE, 'youtube_results.json')

# Scratch goes next to the repo, not on the system temp drive. A 1 GB payload
# needs room for a ~3 GB encoded video plus a ~3 GB download, and the default
# Windows temp directory frequently lives on a system drive without that much
# headroom. Set VIDCOMPILER_SCRATCH to override.
SCRATCH = os.environ.get(
    'VIDCOMPILER_SCRATCH',
    os.path.join(os.path.dirname(os.path.dirname(HERE)), 'bench_scratch'))
os.makedirs(SCRATCH, exist_ok=True)
tempfile.tempdir = SCRATCH
# youtube_api and video_decoder make their own temp dirs; steer those too.
os.environ['TMP'] = os.environ['TEMP'] = os.environ['TMPDIR'] = SCRATCH


def free_bytes(path):
    return shutil.disk_usage(path).free

# YouTube publishes lower resolutions first; the 1080p rendition we need can
# lag the upload by minutes. Poll for it rather than guessing a fixed sleep.
POLL_INTERVAL = 20
POLL_TIMEOUT = 45 * 60


def log(msg):
    print(f'  {msg}', flush=True)


def human(n):
    for unit in ('B', 'KB', 'MB', 'GB'):
        if abs(n) < 1024 or unit == 'GB':
            return f'{n:.2f} {unit}' if unit != 'B' else f'{n:.0f} B'
        n /= 1024


def make_payload(path, size_bytes, seed=4242):
    """Incompressible random bytes, written in chunks so 1 GB stays feasible."""
    rng = np.random.default_rng(seed)
    h = hashlib.sha256()
    chunk = 8 << 20
    written = 0
    with open(path, 'wb') as f:
        while written < size_bytes:
            n = min(chunk, size_bytes - written)
            buf = rng.integers(0, 256, n, dtype=np.uint8).tobytes()
            f.write(buf)
            h.update(buf)
            written += n
    return h.hexdigest()


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for blk in iter(lambda: f.read(8 << 20), b''):
            h.update(blk)
    return h.hexdigest()


def available_heights(video_id):
    """Heights YouTube is currently serving, via yt-dlp's format list."""
    kwargs = {}
    if sys.platform == 'win32':
        kwargs['creationflags'] = subprocess.CREATE_NO_WINDOW
    try:
        r = subprocess.run(
            [*yt_dlp_command(), '--no-playlist', '-J',
             f'https://www.youtube.com/watch?v={video_id}'],
            capture_output=True, text=True, timeout=180, **kwargs)
        if r.returncode != 0:
            return []
        info = json.loads(r.stdout)
        return sorted({f.get('height') for f in info.get('formats', [])
                       if f.get('height')})
    except Exception:
        return []


def wait_for_1080p(video_id):
    """Block until YouTube serves a >=1080p rendition. Returns seconds waited."""
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < POLL_TIMEOUT:
        heights = available_heights(video_id)
        if heights and max(heights) >= vc.FRAME_HEIGHT:
            log(f'1080p ready after {time.perf_counter()-t0:.0f}s '
                f'(heights: {heights})')
            return time.perf_counter() - t0
        log(f'waiting for 1080p... {time.perf_counter()-t0:.0f}s elapsed, '
            f'heights so far: {heights or "none yet"}')
        time.sleep(POLL_INTERVAL)
    raise TimeoutError(
        f'YouTube did not publish a 1080p rendition within {POLL_TIMEOUT//60} '
        'minutes. Longer videos take longer to process; try again later '
        'against the same video id.')


def run(size_mb):
    size_bytes = int(size_mb * 1024 * 1024)

    # payload + encoded video (~3x) + downloaded copy (~3x), plus slack
    needed = int(size_bytes * 7.5)
    have = free_bytes(SCRATCH)
    if have < needed:
        print(f'\nSkipping {size_mb} MB: needs ~{human(needed)} of scratch on '
              f'{SCRATCH}, only {human(have)} free.')
        return {'size_mb': size_mb, 'status': 'skipped_disk',
                'needed_bytes': needed, 'free_bytes': have}

    work = tempfile.mkdtemp(prefix='ytbench_')
    src = os.path.join(work, 'payload.bin')
    video = os.path.join(work, 'out.mp4')
    result = {'size_mb': size_mb, 'started': time.strftime('%Y-%m-%d %H:%M:%S')}

    try:
        print(f'\n{"="*68}\n== {size_mb} MB payload\n{"="*68}')
        log('generating incompressible payload...')
        want = make_payload(src, size_bytes)
        result['payload_bytes'] = size_bytes
        result['sha256'] = want

        # warm JIT / encoder probe so they are not charged to the timing
        ve.rs_encode(b'\x00' * 4096)
        ve._get_encoder()

        t0 = time.perf_counter()
        ve.encode_files_to_video([src], video, progress=None)
        result['encode_s'] = time.perf_counter() - t0
        vsize = os.path.getsize(video)
        result['video_bytes'] = vsize
        result['expansion'] = vsize / size_bytes
        frames = 1 + -(-size_bytes // vc.BYTES_PER_FRAME)
        result['approx_frames'] = frames
        result['duration_s'] = frames / vc.FPS
        log(f'encoded in {result["encode_s"]:.1f}s -> {human(vsize)} '
            f'({result["expansion"]:.2f}x), ~{result["duration_s"]/60:.1f} min video')

        t0 = time.perf_counter()
        video_id, url = upload_video(
            video, title=f'vidcompiler benchmark {size_mb}MB '
                         f'{time.strftime("%Y%m%d-%H%M%S")}',
            progress=lambda m: log(m))
        result['upload_s'] = time.perf_counter() - t0
        result['video_id'] = video_id
        result['url'] = url
        result['upload_mbps'] = vsize * 8 / result['upload_s'] / 1e6
        log(f'uploaded in {result["upload_s"]:.1f}s '
            f'({result["upload_mbps"]:.1f} Mbit/s) -> {url}')

        result['processing_wait_s'] = wait_for_1080p(video_id)

        description = fetch_description(url)
        result['sidecar_present'] = bool(vc.decode_sidecar(description))
        log(f'description sidecar present: {result["sidecar_present"]}')

        dl = os.path.join(work, 'dl.mp4')
        t0 = time.perf_counter()
        download_video(url, dl, progress=None)
        result['download_s'] = time.perf_counter() - t0
        dsize = os.path.getsize(dl)
        result['downloaded_bytes'] = dsize
        result['download_mbps'] = dsize * 8 / result['download_s'] / 1e6
        log(f'downloaded {human(dsize)} in {result["download_s"]:.1f}s '
            f'({result["download_mbps"]:.1f} Mbit/s)')

        out = os.path.join(work, 'out')
        t0 = time.perf_counter()
        vd.decode_video_to_files(dl, out, progress=None,
                                 description=description)
        result['decode_s'] = time.perf_counter() - t0
        got = sha256_file(os.path.join(out, 'payload.bin'))
        result['recovered'] = (got == want)
        log(f'decoded in {result["decode_s"]:.1f}s -> '
            f'{"BYTES IDENTICAL" if result["recovered"] else "DATA MISMATCH"}')
        result['status'] = 'ok' if result['recovered'] else 'corrupt'

    except Exception as e:
        result['status'] = 'error'
        result['error'] = f'{type(e).__name__}: {e}'
        log(f'FAILED: {result["error"]}')
    finally:
        shutil.rmtree(work, ignore_errors=True)
        prev = []
        if os.path.exists(RESULTS):
            try:
                prev = json.load(open(RESULTS))
            except Exception:
                prev = []
        prev.append(result)
        with open(RESULTS, 'w') as f:
            json.dump(prev, f, indent=2)
    return result


def decode_only(video_id, size_mb):
    """Re-measure the decode of a video already on YouTube.

    Uploading a multi-gigabyte test again just to time the decode would be
    wasteful, and the payload is generated from a fixed seed, so the expected
    hash can simply be recomputed.
    """
    url = f'https://www.youtube.com/watch?v={video_id}'
    size_bytes = int(size_mb * 1024 * 1024)
    work = tempfile.mkdtemp(prefix='ytdec_')
    try:
        print(f'
{"="*68}
== decode-only: {size_mb} MB from {url}
{"="*68}')
        log('regenerating the expected payload...')
        want = make_payload(os.path.join(work, 'expected.bin'), size_bytes)

        description = fetch_description(url)
        log(f'description sidecar present: '
            f'{bool(vc.decode_sidecar(description))}')

        dl = os.path.join(work, 'dl.mp4')
        t0 = time.perf_counter()
        download_video(url, dl, progress=None)
        dt_dl = time.perf_counter() - t0
        dsize = os.path.getsize(dl)
        log(f'downloaded {human(dsize)} in {dt_dl:.1f}s '
            f'({dsize*8/dt_dl/1e6:.1f} Mbit/s)')

        out = os.path.join(work, 'out')
        t0 = time.perf_counter()
        vd.decode_video_to_files(dl, out, progress=None,
                                 description=description)
        dt = time.perf_counter() - t0
        got = sha256_file(os.path.join(out, 'payload.bin'))
        ok = got == want
        log(f'decoded in {dt:.1f}s -> '
            f'{"BYTES IDENTICAL" if ok else "DATA MISMATCH"}')
        log(f'decode throughput: {size_bytes/dt/1e6:.2f} MB/s')
        return ok
    finally:
        shutil.rmtree(work, ignore_errors=True)


def main(sizes):
    print('Videos are uploaded UNLISTED and stay on your channel until deleted.')
    print(f'Scratch directory: {SCRATCH}  ({human(free_bytes(SCRATCH))} free)')
    for mb in sizes:
        r = run(mb)
        if r.get('status') != 'ok':
            print(f'\nStopping the ladder: {mb} MB did not complete cleanly '
                  f'({r.get("status")}).')
            return 1
    print(f'\nAll sizes completed. Results in {RESULTS}')
    return 0


if __name__ == '__main__':
    argv = sys.argv[1:]
    if argv and argv[0] == '--decode-only':
        sys.exit(0 if decode_only(argv[1], float(argv[2])) else 1)
    sys.exit(main([float(a) for a in argv] or [8.0]))

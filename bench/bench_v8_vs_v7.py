"""
v8 against v7, on the same payloads, end to end.

Both formats are run through the real encoder and decoder and verified
byte-identical, at sizes up to 3 GB. What is being compared is not just speed:

  payload/frame   how much each frame carries
  video bytes     what actually goes on the wire -- the number that decides
                  upload time, which dominates everything else at real sizes
  videos needed   how many uploads the archive costs, from each format's
                  own per-video ceiling (YouTube caps unverified accounts at
                  15 minutes)

v8 wins the first and third and loses the second. This measures by how much
rather than arguing about it.

Local compute only; no network. Upload time is estimated from measured bytes
and the 18.8 Mbit/s that bench_youtube.py measured against the live service,
and is labelled as an estimate wherever it appears.

Usage: python bench/bench_v8_vs_v7.py [sizes_in_MB ...]
"""
import gc
import hashlib
import json
import math
import os
import shutil
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np

import video_codec as vc
import video_encoder as ve
import video_decoder as vd

HERE = os.path.dirname(os.path.abspath(__file__))
RESULTS = os.path.join(HERE, 'v8_vs_v7_results.json')
WORK = os.environ.get('VIDCOMPILER_BENCH_WORK', 'E:/vid_compiler/benchwork')
WORK = os.path.join(WORK, 'v8v7')

# Measured against the live service in bench/bench_youtube.py, on the 1 GB run.
UPLOAD_MBIT = 18.8
MAX_FRAMES_PER_VIDEO = 26940          # YouTube's 15 minutes at 30 fps

DEFAULT_SIZES = [10, 100, 1000, 3000]

CONTENDERS = [
    ('v7', vc.PROFILE_V5, False),
    ('v8', vc.PROFILE_V8, True),
]


def human(n):
    return f'{n / 1e9:.2f} GB' if n >= 1e9 else f'{n / 1e6:.0f} MB'


def write_random(path, nbytes, chunk=64 << 20):
    rng = np.random.default_rng(20260827)
    h = hashlib.sha256()
    left = nbytes
    with open(path, 'wb', buffering=1 << 20) as f:
        while left > 0:
            b = rng.bytes(min(chunk, left))
            f.write(b)
            h.update(b)
            left -= len(b)
    return h.hexdigest()


def videos_needed(payload_bytes, profile):
    """How many uploads this archive costs at this format's per-video ceiling."""
    per_video = int(MAX_FRAMES_PER_VIDEO * profile.bytes * vc.CHUNK_IN / 255)
    return max(1, math.ceil(payload_bytes / per_video)), per_video


def run(mb):
    nbytes = int(mb * 1e6)
    case = os.path.join(WORK, f'case_{mb}')
    shutil.rmtree(case, ignore_errors=True)
    os.makedirs(case)

    src = os.path.join(case, f'payload_{mb}MB.bin')
    print(f'\n=== {human(nbytes)} ===', flush=True)
    t = time.perf_counter()
    write_random(src, nbytes)
    print(f'  generated in {time.perf_counter() - t:.1f}s', flush=True)

    archive = ve.create_archive([src])
    rec = {'mb': mb, 'payload_bytes': nbytes, 'archive_bytes': len(archive),
           'formats': {}}

    for name, prof, audio_payload in CONTENDERS:
        vid = os.path.join(case, f'{name}.mp4')
        msgs = []
        try:
            t = time.perf_counter()
            ve.encode_bytes_to_video(archive, vid, progress=msgs.append,
                                     profile=prof, audio_payload=audio_payload)
            t_enc = time.perf_counter() - t
            video_bytes = os.path.getsize(vid)

            t = time.perf_counter()
            got = vd.decode_video_to_bytes(vid, progress=None)
            t_dec = time.perf_counter() - t
            ok = (got == archive)
            del got
            gc.collect()

            audio_line = [m for m in msgs if m.startswith('Audio channel:')]
            audio_carried = (int(audio_line[0].split()[2].replace(',', ''))
                             if audio_line else 0)
            n_vids, per_video = videos_needed(nbytes, prof)

            f = {
                'bytes_per_frame': prof.bytes,
                'video_bytes': video_bytes,
                'expansion': video_bytes / nbytes,
                't_encode': t_enc, 't_decode': t_dec,
                'enc_mbps': nbytes / t_enc / 1e6,
                'dec_mbps': nbytes / t_dec / 1e6,
                'audio_payload_bytes': audio_carried,
                'videos_needed': n_vids,
                'per_video_capacity': per_video,
                'est_upload_s': video_bytes * 8 / (UPLOAD_MBIT * 1e6),
                'identical': ok, 'ok': ok,
            }
            print(f'  {name}: {prof.bytes:>7,} B/frame  video {human(video_bytes):>8} '
                  f'({f["expansion"]:.2f}x)  enc {t_enc:6.1f}s  dec {t_dec:6.1f}s  '
                  f'{n_vids} video(s)  audio {audio_carried:,} B  '
                  f'{"IDENTICAL" if ok else "*** MISMATCH ***"}', flush=True)
        except Exception as exc:
            f = {'ok': False, 'error': f'{exc.__class__.__name__}: {exc}'}
            print(f'  {name}: FAILED {f["error"]}', flush=True)
        finally:
            if os.path.exists(vid):
                os.unlink(vid)
            sc = vid + '.sidecar'
            if os.path.exists(sc):
                os.unlink(sc)
        rec['formats'][name] = f

    del archive
    gc.collect()
    shutil.rmtree(case, ignore_errors=True)
    return rec


def main():
    sizes = [float(a) for a in sys.argv[1:]] or DEFAULT_SIZES
    os.makedirs(WORK, exist_ok=True)

    results = []
    if os.path.exists(RESULTS):
        try:
            results = json.load(open(RESULTS))['runs']
        except Exception:
            results = []

    print(f'Free on work volume: {shutil.disk_usage(WORK).free / 1e9:.0f} GB')
    print('Warming up (not recorded)...', flush=True)
    run(2)

    for mb in sizes:
        rec = run(mb)
        results = [r for r in results if r['mb'] != mb] + [rec]
        results.sort(key=lambda r: r['mb'])
        with open(RESULTS, 'w') as f:
            json.dump({'runs': results, 'upload_mbit': UPLOAD_MBIT,
                       'note': 'local compute; upload time is an estimate'},
                      f, indent=2)

    print(f'\nWrote {RESULTS}\n')
    hdr = (f'{"size":>8} {"format":>6} {"B/frame":>9} {"video":>9} {"exp":>6} '
           f'{"encode":>8} {"decode":>8} {"vids":>5} {"est upload":>11}')
    print(hdr)
    for r in results:
        for name in ('v7', 'v8'):
            f = r['formats'].get(name, {})
            if not f.get('ok'):
                print(f'{human(r["payload_bytes"]):>8} {name:>6}  '
                      f'{f.get("error", "failed")}')
                continue
            print(f'{human(r["payload_bytes"]):>8} {name:>6} '
                  f'{f["bytes_per_frame"]:>9,} {human(f["video_bytes"]):>9} '
                  f'{f["expansion"]:>5.2f}x {f["t_encode"]:>7.1f}s '
                  f'{f["t_decode"]:>7.1f}s {f["videos_needed"]:>5} '
                  f'{f["est_upload_s"] / 60:>10.1f}m')


if __name__ == '__main__':
    main()

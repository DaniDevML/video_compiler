"""
Does uploading shards in parallel beat one big upload?

This is the measurement that decides whether sharding is worth using, because
upload dominates end-to-end time at any real payload size. A single HTTP stream
to YouTube has measured 18.4-18.8 Mbit/s on large transfers here; the question
is whether several concurrent streams sum to more than that, or whether the
link is already saturated and sharding just adds videos.

Runs a complete sharded round trip against the real service: encode N shards,
upload them concurrently, wait for every 1080p rendition, download and decode
concurrently, and verify the reassembled archive.

Videos are uploaded UNLISTED and stay on the channel until deleted.

Usage: python bench/bench_shards_upload.py [size_mb] [n_shards]
"""
import concurrent.futures as fut
import hashlib
import json
import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np

import shards
import video_codec as vc
import video_encoder as ve

HERE = os.path.dirname(os.path.abspath(__file__))
SCRATCH = os.environ.get(
    'VIDCOMPILER_SCRATCH',
    os.path.join(os.path.dirname(os.path.dirname(HERE)), 'bench_scratch'))
os.makedirs(SCRATCH, exist_ok=True)
tempfile.tempdir = SCRATCH
os.environ['TMP'] = os.environ['TEMP'] = os.environ['TMPDIR'] = SCRATCH

from youtube_api import upload_video, download_video, fetch_description  # noqa
from bench_youtube import wait_for_1080p, human                          # noqa

RESULTS = os.path.join(HERE, 'shard_upload_results.json')

# Measured serial baseline for a single large upload on this connection.
SERIAL_MBPS = 18.6


def log(msg):
    print(f'  {msg}', flush=True)


def main(mb=96.0, n_shards=3):
    rng = np.random.default_rng(2024)
    data = rng.integers(0, 256, int(mb * 1024 * 1024), dtype=np.uint8).tobytes()
    want = hashlib.sha256(data).hexdigest()

    work = tempfile.mkdtemp(prefix='shardup_')
    src_dir = os.path.join(work, 'src')
    os.makedirs(src_dir)
    src = os.path.join(src_dir, 'payload.bin')
    with open(src, 'wb') as f:
        f.write(data)

    result = {'size_mb': mb, 'n_shards': n_shards,
              'started': time.strftime('%Y-%m-%d %H:%M:%S')}
    print(f'{mb:.0f} MB payload across {n_shards} shards, uploaded in parallel')
    print(f'serial baseline for one stream: {SERIAL_MBPS} Mbit/s\n')

    try:
        ve.rs_encode(b'\x00' * 4096)
        ve._get_encoder()

        out = os.path.join(work, 'shards')
        t0 = time.perf_counter()
        jobs = shards.encode_files_to_shards([src], out, n_shards=n_shards,
                                             progress=None,
                                             max_workers=n_shards)
        result['encode_s'] = time.perf_counter() - t0
        total_video = sum(j['bytes'] for j in jobs)
        result['video_bytes'] = total_video
        log(f'encoded {n_shards} shards in {result["encode_s"]:.1f}s, '
            f'{human(total_video)} total')

        # ---- parallel upload ----
        def up(j):
            t = time.perf_counter()
            vid, url = upload_video(
                j['path'],
                title=f'vidcompiler shard {j["index"]+1}/{n_shards} '
                      f'{time.strftime("%Y%m%d-%H%M%S")}',
                progress=None)
            return dict(index=j['index'], video_id=vid, url=url,
                        bytes=j['bytes'], seconds=time.perf_counter() - t)

        t0 = time.perf_counter()
        with fut.ThreadPoolExecutor(max_workers=n_shards) as ex:
            ups = list(ex.map(up, jobs))
        wall = time.perf_counter() - t0
        result['upload_wall_s'] = wall
        result['uploads'] = ups
        agg = total_video * 8 / wall / 1e6
        result['upload_aggregate_mbps'] = agg
        for u in ups:
            log(f'shard {u["index"]}: {human(u["bytes"])} in '
                f'{u["seconds"]:.1f}s ({u["bytes"]*8/u["seconds"]/1e6:.1f} '
                f'Mbit/s) -> {u["video_id"]}')
        log(f'parallel upload wall time {wall:.1f}s, '
            f'aggregate {agg:.1f} Mbit/s')
        log(f'vs one serial stream at {SERIAL_MBPS} Mbit/s: '
            f'{agg/SERIAL_MBPS:.2f}x')

        # ---- wait for every rendition ----
        t0 = time.perf_counter()
        with fut.ThreadPoolExecutor(max_workers=n_shards) as ex:
            list(ex.map(lambda u: wait_for_1080p(u['video_id']), ups))
        result['processing_s'] = time.perf_counter() - t0
        log(f'all {n_shards} renditions ready after '
            f'{result["processing_s"]:.0f}s')

        # ---- parallel download ----
        def dl(u):
            p = os.path.join(work, f'dl{u["index"]}.mp4')
            d = fetch_description(u['url'])
            download_video(u['url'], p, progress=None)
            return u['index'], p, d

        t0 = time.perf_counter()
        with fut.ThreadPoolExecutor(max_workers=n_shards) as ex:
            got = sorted(ex.map(dl, ups))
        result['download_wall_s'] = time.perf_counter() - t0
        dl_bytes = sum(os.path.getsize(p) for _, p, _ in got)
        result['download_aggregate_mbps'] = dl_bytes * 8 / result['download_wall_s'] / 1e6
        log(f'downloaded {human(dl_bytes)} in '
            f'{result["download_wall_s"]:.1f}s '
            f'({result["download_aggregate_mbps"]:.1f} Mbit/s aggregate)')

        # ---- parallel decode ----
        paths = [p for _, p, _ in got]
        descs = [d for _, _, d in got]
        out_dir = os.path.join(work, 'out')
        t0 = time.perf_counter()
        shards.decode_shards_to_files(paths, out_dir, descs, progress=None,
                                      max_workers=n_shards)
        result['decode_s'] = time.perf_counter() - t0
        rec = hashlib.sha256(
            open(os.path.join(out_dir, 'payload.bin'), 'rb').read()).hexdigest()
        result['recovered'] = rec == want
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
    return 0 if result.get('status') == 'ok' else 1


if __name__ == '__main__':
    sys.exit(main(float(sys.argv[1]) if len(sys.argv) > 1 else 96.0,
                  int(sys.argv[2]) if len(sys.argv) > 2 else 3))

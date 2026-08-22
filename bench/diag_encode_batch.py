"""
Tune the encode batch size.

The encoder generates frames in batches and hands them to a writer thread. Too
large a batch means ffmpeg waits while a big block is generated, and hundreds
of megabytes sit queued; too small means more per-call overhead.

Usage: python bench/diag_encode_batch.py [size_mb]
"""
import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np

import video_codec as vc
import video_encoder as ve


def main(mb=8.0):
    rng = np.random.default_rng(1234)
    data = rng.integers(0, 256, int(mb * 1024 * 1024), dtype=np.uint8).tobytes()
    work = tempfile.mkdtemp()
    src = os.path.join(work, 'p.bin')
    with open(src, 'wb') as f:
        f.write(data)

    ve.rs_encode(b'\x00' * 4096)
    ve._get_encoder()
    orig = ve.ENCODE_BATCH
    print(f'{mb} MB payload, {vc.BYTES_PER_FRAME:,} B/frame\n')
    print(f'{"ENCODE_BATCH":>13} {"MB in flight":>13} {"time":>8} {"MB/s":>8}')
    print('-' * 46)
    try:
        for batch in (2, 4, 8, 16, 32, 64):
            ve.ENCODE_BATCH = batch
            best = float('inf')
            for _ in range(2):
                out = os.path.join(work, 'v.mp4')
                t0 = time.perf_counter()
                ve.encode_files_to_video([src], out, progress=None)
                best = min(best, time.perf_counter() - t0)
                os.unlink(out)
            inflight = batch * vc.YUV_FRAME_BYTES / 1e6
            print(f'{batch:>13} {inflight:>13.1f} {best:>7.2f}s '
                  f'{len(data)/best/1e6:>7.2f}')
    finally:
        ve.ENCODE_BATCH = orig
        shutil.rmtree(work, ignore_errors=True)


if __name__ == '__main__':
    main(float(sys.argv[1]) if len(sys.argv) > 1 else 8.0)

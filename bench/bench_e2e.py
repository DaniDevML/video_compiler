"""
End-to-end benchmark of the public API: files -> video -> files.

Designed to run unchanged against either the v4 tree or the v5 tree, so the
two can be compared like for like. Reports encode time, decode time, the size
of the uploaded video, and verifies the recovered bytes.

Usage: python bench/bench_e2e.py [size_mb] [repeats] [label]
"""
import hashlib
import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np

import video_encoder as ve
import video_decoder as vd
import video_codec as vc


def sha(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for blk in iter(lambda: f.read(1 << 20), b''):
            h.update(blk)
    return h.hexdigest()


def main(mb=8.0, repeats=1, label='current'):
    rng = np.random.default_rng(1234)
    payload = rng.integers(0, 256, int(mb * 1024 * 1024), dtype=np.uint8).tobytes()

    work = tempfile.mkdtemp()
    src = os.path.join(work, 'payload.bin')
    with open(src, 'wb') as f:
        f.write(payload)
    want = hashlib.sha256(payload).hexdigest()

    # Warm the RS JIT and the hardware-encoder probe so neither is charged to
    # the first timed run.
    ve.rs_encode(b'\x00' * 4096)
    ve._get_encoder()

    print(f'=== {label} ===')
    print(f'payload {len(payload):,} B incompressible, '
          f'{vc.BYTES_PER_FRAME:,} B/frame, native={vc.NATIVE_AVAILABLE}')

    enc_times, dec_times, sizes = [], [], []
    for i in range(repeats):
        video = os.path.join(work, f'v{i}.mp4')
        out = os.path.join(work, f'out{i}')

        t0 = time.perf_counter()
        ve.encode_files_to_video([src], video, progress=None)
        enc_times.append(time.perf_counter() - t0)
        sizes.append(os.path.getsize(video))

        t0 = time.perf_counter()
        vd.decode_video_to_files(video, out, progress=None)
        dec_times.append(time.perf_counter() - t0)

        got = sha(os.path.join(out, 'payload.bin'))
        if got != want:
            print('  DATA MISMATCH - recovered bytes differ from the original')
            sys.exit(2)
        os.unlink(video)
        shutil.rmtree(out, ignore_errors=True)

    e, d, s = min(enc_times), min(dec_times), min(sizes)
    n = len(payload)
    print(f'  encode      {e:7.2f} s   {n/e/1e6:6.2f} MB/s')
    print(f'  decode      {d:7.2f} s   {n/d/1e6:6.2f} MB/s')
    print(f'  video size  {s/1e6:7.2f} MB  {s/n:6.2f}x payload')
    print(f'  data verified: OK (sha256 match, best of {repeats})')
    shutil.rmtree(work, ignore_errors=True)
    return dict(encode=e, decode=d, size=s, payload=n)


if __name__ == '__main__':
    main(float(sys.argv[1]) if len(sys.argv) > 1 else 8.0,
         int(sys.argv[2]) if len(sys.argv) > 2 else 1,
         sys.argv[3] if len(sys.argv) > 3 else 'current')

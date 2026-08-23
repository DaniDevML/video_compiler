"""
Does sharding actually make anything faster locally?

Splitting the work across videos only pays if the stages can genuinely run at
once. Two things could stop that: consumer NVIDIA cards cap concurrent NVENC
sessions (historically at two or three), and the pixel and Reed-Solomon stages
already use every core, so extra shards just re-divide the same CPU.

Measures encode and decode wall time for the same payload at 1, 2, 3 and 4
shards. Upload is measured separately, against the real service, by
bench/bench_shards_upload.py -- that is the one that matters, since upload
dominates end-to-end.

Usage: python bench/bench_shards.py [size_mb]
"""
import hashlib
import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np

import shards
import video_codec as vc
import video_encoder as ve

SCRATCH = os.environ.get(
    'VIDCOMPILER_SCRATCH',
    os.path.join(os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.abspath(__file__)))), 'bench_scratch'))
os.makedirs(SCRATCH, exist_ok=True)
tempfile.tempdir = SCRATCH


def read_sidecar(path):
    with open(path + '.sidecar', encoding='utf-8') as f:
        return f.read()


def main(mb=64.0):
    rng = np.random.default_rng(808)
    data = rng.integers(0, 256, int(mb * 1024 * 1024), dtype=np.uint8).tobytes()
    want = hashlib.sha256(data).hexdigest()

    work = tempfile.mkdtemp(prefix='shardbench_')
    src_dir = os.path.join(work, 'src')
    os.makedirs(src_dir)
    src = os.path.join(src_dir, 'payload.bin')
    with open(src, 'wb') as f:
        f.write(data)

    ve.rs_encode(b'\x00' * 4096)
    ve._get_encoder()

    print(f'{mb:.0f} MB payload, profile {vc.DEFAULT_PROFILE.bytes:,} B/frame')
    print(f'encoder: {ve._HW_CODEC}\n')
    print(f'{"shards":>7} {"encode":>9} {"MB/s":>8} {"decode":>9} {"MB/s":>8} '
          f'{"total video":>12}  data')
    print('-' * 66)

    baseline = {}
    try:
        for n in (1, 2, 3, 4):
            out = os.path.join(work, f'n{n}')
            t0 = time.perf_counter()
            jobs = shards.encode_files_to_shards([src], out, n_shards=n,
                                                 progress=None, max_workers=n)
            t_enc = time.perf_counter() - t0

            paths = [j['path'] for j in jobs]
            descs = [read_sidecar(p) for p in paths]
            total_video = sum(j['bytes'] for j in jobs)

            got_dir = os.path.join(work, f'o{n}')
            t0 = time.perf_counter()
            shards.decode_shards_to_files(paths, got_dir, descs,
                                          progress=None, max_workers=n)
            t_dec = time.perf_counter() - t0

            got = hashlib.sha256(
                open(os.path.join(got_dir, 'payload.bin'), 'rb').read()
            ).hexdigest()

            print(f'{n:>7} {t_enc:>8.2f}s {len(data)/t_enc/1e6:>7.2f} '
                  f'{t_dec:>8.2f}s {len(data)/t_dec/1e6:>7.2f} '
                  f'{total_video/1e6:>11.1f}M  '
                  f'{"ok" if got == want else "MISMATCH"}')
            if n == 1:
                baseline = dict(enc=t_enc, dec=t_dec)
            shutil.rmtree(out, ignore_errors=True)
            shutil.rmtree(got_dir, ignore_errors=True)
    finally:
        shutil.rmtree(work, ignore_errors=True)

    if baseline:
        print(f'\nbaseline (1 shard): encode {baseline["enc"]:.2f}s, '
              f'decode {baseline["dec"]:.2f}s')


if __name__ == '__main__':
    main(float(sys.argv[1]) if len(sys.argv) > 1 else 64.0)

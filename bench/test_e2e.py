"""
End-to-end pipeline test: files -> video -> files, byte-identical.

Covers the cases most likely to break the packed-bit rewrite: payloads that do
not land on a frame boundary, highly compressible and incompressible data,
multiple files, and an empty file.

Usage: python bench/test_e2e.py
"""
import hashlib
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np

import video_encoder as ve
import video_decoder as vd
import video_codec as vc

fails = 0


def digest(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for blk in iter(lambda: f.read(1 << 20), b''):
            h.update(blk)
    return h.hexdigest()


def run_case(name, files):
    """files: list of (filename, bytes)"""
    global fails
    work = tempfile.mkdtemp()
    src_dir = os.path.join(work, 'src')
    out_dir = os.path.join(work, 'out')
    os.makedirs(src_dir)
    paths = []
    for fname, data in files:
        p = os.path.join(src_dir, fname)
        with open(p, 'wb') as f:
            f.write(data)
        paths.append(p)

    video = os.path.join(work, 'v.mp4')
    try:
        ve.encode_files_to_video(paths, video, progress=None)
        size = os.path.getsize(video)
        total = sum(len(d) for _, d in files)
        vd.decode_video_to_files(video, out_dir, progress=None)

        ok = True
        for fname, data in files:
            got = os.path.join(out_dir, fname)
            if not os.path.exists(got):
                print(f'  FAIL  {name}: {fname} missing from output')
                ok = False
                continue
            if digest(got) != hashlib.sha256(data).hexdigest():
                print(f'  FAIL  {name}: {fname} content differs')
                ok = False
        if ok:
            exp = size / total if total else float('nan')
            print(f'  PASS  {name}  ({total:,} B payload, '
                  f'{size:,} B video, {exp:.2f}x)')
        else:
            fails += 1
    except Exception as e:
        print(f'  FAIL  {name}: {type(e).__name__}: {e}')
        fails += 1
    finally:
        shutil.rmtree(work, ignore_errors=True)


rng = np.random.default_rng(42)

print(f'packed path: {vc.PACKED_AVAILABLE}   C threads: {vc.NATIVE_THREADS}')
print(f'{vc.BYTES_PER_FRAME:,} bytes/frame, upload qp {ve.UPLOAD_QP}\n')

# A payload of exactly one frame's worth of post-ECC data is the boundary the
# frame padding logic is most likely to get wrong.
print('running cases...')
run_case('tiny text', [('hello.txt', b'hello world\n')])
run_case('empty file', [('empty.bin', b'')])
run_case('incompressible 300 KB',
         [('rand.bin', rng.integers(0, 256, 300_000, dtype=np.uint8).tobytes())])
run_case('compressible 2 MB', [('zeros.bin', b'A' * 2_000_000)])
run_case('multi-file',
         [('a.bin', rng.integers(0, 256, 50_000, dtype=np.uint8).tobytes()),
          ('b.txt', b'second file\n' * 5000),
          ('c.bin', rng.integers(0, 256, 123_457, dtype=np.uint8).tobytes())])
run_case('incompressible 3 MB',
         [('big.bin', rng.integers(0, 256, 3_000_000, dtype=np.uint8).tobytes())])

print(f'\n{"ALL E2E TESTS PASSED" if fails == 0 else f"{fails} CASE(S) FAILED"}')
sys.exit(1 if fails else 0)

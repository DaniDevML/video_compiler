"""
Sharding correctness: split across videos, reassemble byte-identically.

The cases that matter are the ones where reassembly can go quietly wrong:
shards supplied out of order, a shard missing, and shards from two different
uploads mixed together. Silently returning a corrupt archive would be worse
than failing, so each of those must be detected.
"""
import hashlib
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np

import shards
import video_codec as vc

fails = 0


def check(name, cond):
    global fails
    print(f'  {"PASS" if cond else "FAIL"}  {name}')
    if not cond:
        fails += 1


def read_sidecar(path):
    with open(path + '.sidecar', encoding='utf-8') as f:
        return f.read()


print(f'profile {vc.DEFAULT_PROFILE.bytes:,} B/frame, '
      f'max {shards.max_bytes_per_video()/1e6:.0f} MB per video\n')

print('[1] manifest round-trip')
m = shards.pack_manifest(123456789, 0xDEADBEEF, 7, 3, 1000, 2000)
g = shards.parse_manifest('blurb\n' + m + '\nmore')
check('fields preserved',
      g and g['total_size'] == 123456789 and g['crc32'] == 0xDEADBEEF
      and g['n_shards'] == 7 and g['index'] == 3
      and g['offset'] == 1000 and g['length'] == 2000)
check('junk rejected without raising',
      shards.parse_manifest('nothing here') is None
      and shards.parse_manifest(shards.MANIFEST_PREFIX + 'zzz!!') is None)

print('\n[2] planning')
cap = shards.max_bytes_per_video()
check('small archive stays in one video', len(shards.plan_shards(1000)) == 1)
check('requested count honoured', len(shards.plan_shards(1000, 4)) == 4)
big = shards.plan_shards(cap * 3 - 1)
check('oversized archive is split automatically', len(big) >= 3)
covered = sum(ln for _, ln in shards.plan_shards(1_000_003, 4))
check('slices cover the archive exactly', covered == 1_000_003)

print('\n[3] end-to-end across shards')
rng = np.random.default_rng(5150)
work = tempfile.mkdtemp()
try:
    src_dir = os.path.join(work, 'src')
    os.makedirs(src_dir)
    data = rng.integers(0, 256, 700_000, dtype=np.uint8).tobytes()
    src = os.path.join(src_dir, 'payload.bin')
    with open(src, 'wb') as f:
        f.write(data)
    want = hashlib.sha256(data).hexdigest()

    out = os.path.join(work, 'shards')
    jobs = shards.encode_files_to_shards([src], out, n_shards=3, progress=None)
    check('three shard videos written',
          len(jobs) == 3 and all(os.path.exists(j['path']) for j in jobs))

    paths = [j['path'] for j in jobs]
    descs = [read_sidecar(p) for p in paths]

    got_dir = os.path.join(work, 'out')
    shards.decode_shards_to_files(paths, got_dir, descs, progress=None)
    got = hashlib.sha256(
        open(os.path.join(got_dir, 'payload.bin'), 'rb').read()).hexdigest()
    check('reassembled byte-identical', got == want)
    shutil.rmtree(got_dir, ignore_errors=True)

    print('\n[4] shards supplied out of order')
    order = [2, 0, 1]
    got_dir = os.path.join(work, 'out2')
    shards.decode_shards_to_files([paths[i] for i in order], got_dir,
                                  [descs[i] for i in order], progress=None)
    got = hashlib.sha256(
        open(os.path.join(got_dir, 'payload.bin'), 'rb').read()).hexdigest()
    check('manifest puts them back in order', got == want)
    shutil.rmtree(got_dir, ignore_errors=True)

    print('\n[5] failure modes are detected, not silently wrong')
    try:
        shards.decode_shards_to_bytes(paths[:2], descs[:2], progress=None)
        check('missing shard rejected', False)
    except RuntimeError as e:
        check(f'missing shard rejected ({str(e)[:38]}...)', True)

    # shards from a different archive
    other = os.path.join(work, 'other')
    other_src = os.path.join(src_dir, 'other.bin')
    with open(other_src, 'wb') as f:
        f.write(rng.integers(0, 256, 700_000, dtype=np.uint8).tobytes())
    ojobs = shards.encode_files_to_shards([other_src], other, n_shards=3,
                                          progress=None)
    mixed_paths = [paths[0], paths[1], ojobs[2]['path']]
    mixed_descs = [descs[0], descs[1], read_sidecar(ojobs[2]['path'])]
    try:
        shards.decode_shards_to_bytes(mixed_paths, mixed_descs, progress=None)
        check('mismatched shards rejected', False)
    except RuntimeError as e:
        check(f'mismatched shards rejected ({str(e)[:34]}...)', True)

finally:
    shutil.rmtree(work, ignore_errors=True)

print(f'\n{"ALL SHARD TESTS PASSED" if fails == 0 else f"{fails} FAILED"}')
sys.exit(1 if fails else 0)

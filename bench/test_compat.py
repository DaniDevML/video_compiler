"""
Backward compatibility: videos produced by the previous release must still
decode with the rewritten decoder.

Encodes with the v4 tree (a git worktree at the previous commit) in a separate
interpreter, then decodes with the current tree and compares bytes.

Usage: python bench/test_compat.py [path_to_v4_tree]
"""
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np

import video_decoder as vd

DEFAULT_V4 = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    'baseline_v4')

ENCODE_SCRIPT = """
import sys
sys.path.insert(0, {tree!r})
import video_encoder as ve
ve.encode_files_to_video([{src!r}], {dst!r}, progress=None)
print('ok')
"""


def main(v4_tree=DEFAULT_V4):
    if not os.path.isdir(v4_tree):
        print(f'v4 tree not found at {v4_tree}; skipping compatibility test.')
        print('Create it with:  git worktree add ../baseline_v4 optimization-v3')
        return 0

    work = tempfile.mkdtemp()
    rng = np.random.default_rng(77)
    data = rng.integers(0, 256, 500_000, dtype=np.uint8).tobytes()
    src = os.path.join(work, 'legacy.bin')
    with open(src, 'wb') as f:
        f.write(data)
    video = os.path.join(work, 'legacy.mp4')

    print(f'encoding with the v4 tree at {v4_tree} ...')
    script = ENCODE_SCRIPT.format(tree=v4_tree, src=src, dst=video)
    r = subprocess.run([sys.executable, '-c', script],
                       capture_output=True, text=True, cwd=v4_tree)
    if r.returncode != 0 or not os.path.exists(video):
        print('  could not encode with the v4 tree:')
        print(r.stderr[-800:])
        shutil.rmtree(work, ignore_errors=True)
        return 1

    print(f'  v4 video: {os.path.getsize(video):,} bytes')
    print('decoding with the current tree ...')
    out = os.path.join(work, 'out')
    try:
        vd.decode_video_to_files(video, out, progress=None)
        got = open(os.path.join(out, 'legacy.bin'), 'rb').read()
        ok = hashlib.sha256(got).hexdigest() == hashlib.sha256(data).hexdigest()
        print(f'  {"PASS" if ok else "FAIL"}  v4 video decodes byte-identically'
              ' with the v5 decoder')
        return 0 if ok else 1
    except Exception as e:
        print(f'  FAIL  {type(e).__name__}: {e}')
        return 1
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == '__main__':
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_V4))

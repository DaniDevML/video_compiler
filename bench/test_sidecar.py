"""
Tests for the description sidecar header.

The sidecar must be a pure accelerator: decoding has to give identical results
with it, without it, and when the description contains junk.
"""
import hashlib
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np

import video_codec as vc
import video_encoder as ve
import video_decoder as vd

fails = 0


def check(name, cond):
    global fails
    print(f'  {"PASS" if cond else "FAIL"}  {name}')
    if not cond:
        fails += 1


print('[1] sidecar round-trip')
hdr = vc.pack_header(123456, 42, 654321, 0xCAFEBABE, audio_bytes=7)
text = vc.encode_sidecar(hdr)
got = vc.decode_sidecar(text)
check('encode -> decode preserves fields',
      got is not None and got['archive_size'] == 123456
      and got['num_data_frames'] == 42 and got['encoded_size'] == 654321
      and got['crc32'] == 0xCAFEBABE)
check('survives surrounding description text',
      vc.decode_sidecar('Some blurb\n\n' + text + '\n#tags')['crc32']
      == 0xCAFEBABE)

print('\n[2] malformed input is rejected, never raises')
for bad in ('', 'no header here', vc.SIDECAR_PREFIX + 'not-base64!!',
            vc.SIDECAR_PREFIX + 'AAAA', 'vidcompiler-header:' + 'A' * 200):
    try:
        r = vc.decode_sidecar(bad)
        ok = r is None
    except Exception as e:
        ok = False
        print(f'    raised {type(e).__name__}')
    check(f'rejects {bad[:34]!r}', ok)

print('\n[3] end-to-end: with sidecar, without, and with a corrupt one')
rng = np.random.default_rng(9)
data = rng.integers(0, 256, 400_000, dtype=np.uint8).tobytes()
work = tempfile.mkdtemp()
src = os.path.join(work, 'f.bin')
with open(src, 'wb') as f:
    f.write(data)
video = os.path.join(work, 'v.mp4')
ve.encode_files_to_video([src], video, progress=None)

check('encoder wrote the .sidecar file', os.path.exists(video + '.sidecar'))
with open(video + '.sidecar', encoding='utf-8') as f:
    real = f.read().strip()

want = hashlib.sha256(data).hexdigest()
for label, desc in [('no description', ''),
                    ('valid sidecar', 'blurb\n\n' + real),
                    ('corrupt sidecar', vc.SIDECAR_PREFIX + 'AAAAAAAA')]:
    out = tempfile.mkdtemp()
    try:
        vd.decode_video_to_files(video, out, progress=None, description=desc)
        p = os.path.join(out, 'f.bin')
        got_hash = hashlib.sha256(open(p, 'rb').read()).hexdigest()
        check(f'{label}: recovered bytes identical', got_hash == want)
    except Exception as e:
        check(f'{label}: decoded without error', False)
        print(f'    {type(e).__name__}: {e}')
    finally:
        shutil.rmtree(out, ignore_errors=True)

shutil.rmtree(work, ignore_errors=True)
print(f'\n{"ALL SIDECAR TESTS PASSED" if fails == 0 else f"{fails} FAILED"}')
sys.exit(1 if fails else 0)

"""
The explorer's full path, minus YouTube: file -> archive -> encrypt -> video,
and back.

The unit tests cover encryption and the index separately. What this checks is
the join between them -- that ciphertext survives the pixel codec as happily
as a tar does, that the decoder can tell an encrypted archive from a plain one
without being told, and that a wrong passphrase fails cleanly at the end of a
real decode rather than producing plausible rubbish.

Ciphertext is worth testing specifically: it is incompressible by
construction, which is the worst case for the archive stage and the densest
possible input to the frame encoder.

Usage: python bench/test_explorer_e2e.py
"""
import hashlib
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import crypto_box as cb                                          # noqa: E402
import shards                                                    # noqa: E402
import video_decoder as vd                                       # noqa: E402
from video_encoder import create_archive                         # noqa: E402

fails = 0
PASS = 'a passphrase with some length to it'


def check(name, cond):
    global fails
    print(f'  {"PASS" if cond else "FAIL"}  {name}')
    if not cond:
        fails += 1


def round_trip(name, payload_bytes, passphrase, n_shards=1):
    """Push a file through the whole local pipeline and read it back."""
    work = tempfile.mkdtemp(prefix='fsx_')
    try:
        src = os.path.join(work, name)
        with open(src, 'wb') as f:
            f.write(payload_bytes)

        payload = create_archive([src])
        if passphrase:
            payload = cb.encrypt(payload, passphrase)

        out_dir = os.path.join(work, 'vids')
        os.makedirs(out_dir)
        jobs = shards.encode_bytes_to_shards(
            payload, out_dir, n_shards=n_shards, progress=None,
            max_workers=n_shards)

        recovered = shards.decode_shards_to_bytes(
            [j['path'] for j in jobs],
            [open(j['path'] + '.sidecar').read() if os.path.exists(j['path'] + '.sidecar') else ''
             for j in jobs],
            progress=None, max_workers=1)
        return recovered, work
    except Exception:
        shutil.rmtree(work, ignore_errors=True)
        raise


def sha(b):
    return hashlib.sha256(b).hexdigest()


CASES = [
    ('plain.txt', b'the quick brown fox\n' * 500, None, 1),
    ('secret.txt', b'the quick brown fox\n' * 500, PASS, 1),
    ('random.bin', os.urandom(300_000), PASS, 1),
    ('empty.bin', b'', PASS, 1),
]

print('Round trips through the real codec')
for name, data, passphrase, n in CASES:
    label = f'{name} ({"encrypted" if passphrase else "plain"}, {len(data):,} B)'
    recovered, work = round_trip(name, data, passphrase, n)
    try:
        check(f'{label}: archive comes back',
              cb.is_encrypted(recovered) == bool(passphrase))

        if passphrase:
            archive = cb.decrypt(recovered, passphrase)
        else:
            archive = recovered

        out = os.path.join(work, 'out')
        os.makedirs(out)
        vd._extract_archive(archive, out, lambda _m: None)
        got = os.path.join(out, name)
        check(f'{label}: file extracted', os.path.exists(got))
        if os.path.exists(got):
            with open(got, 'rb') as f:
                back = f.read()
            check(f'{label}: bytes identical', sha(back) == sha(data))
    finally:
        shutil.rmtree(work, ignore_errors=True)

print('\nEncryption survives sharding')
data = os.urandom(400_000)
recovered, work = round_trip('sharded.bin', data, PASS, n_shards=3)
try:
    check('3 shards reassemble into valid ciphertext', cb.is_encrypted(recovered))
    archive = cb.decrypt(recovered, PASS)
    out = os.path.join(work, 'out')
    os.makedirs(out)
    vd._extract_archive(archive, out, lambda _m: None)
    with open(os.path.join(out, 'sharded.bin'), 'rb') as f:
        check('bytes identical across 3 videos', sha(f.read()) == sha(data))
finally:
    shutil.rmtree(work, ignore_errors=True)

print('\nThe wrong passphrase fails, it does not guess')
recovered, work = round_trip('locked.bin', os.urandom(120_000), PASS)
try:
    try:
        cb.decrypt(recovered, 'the wrong passphrase entirely')
        check('wrong passphrase rejected after a real decode', False)
    except cb.WrongPassphrase:
        check('wrong passphrase rejected after a real decode', True)
    except Exception as exc:
        check(f'wrong passphrase rejected (got {exc.__class__.__name__})', False)
finally:
    shutil.rmtree(work, ignore_errors=True)

print('\nWhat YouTube would hold')
plain = b'CONFIDENTIAL: the secret is 42\n' * 200
work = tempfile.mkdtemp(prefix='fsx_')
try:
    src = os.path.join(work, 'confidential-filename.txt')
    with open(src, 'wb') as f:
        f.write(plain)
    sealed = cb.encrypt(create_archive([src]), PASS)
    check('no plaintext content in the uploaded bytes', b'CONFIDENTIAL' not in sealed)
    check('no filename in the uploaded bytes',
          b'confidential-filename' not in sealed)
finally:
    shutil.rmtree(work, ignore_errors=True)

print()
if fails:
    print(f'{fails} check(s) FAILED')
    sys.exit(1)
print('All explorer end-to-end checks passed.')

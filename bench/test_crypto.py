"""
Encryption correctness.

The interesting cases are not "does a round trip work" -- they are the ways an
attacker or a damaged download could get plaintext out of the decoder anyway.
Every tamper below must be rejected, not merely produce garbage: a chunked AEAD
that authenticates each chunk in isolation still permits reordering, splicing
and truncation, and those are the failures worth testing.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import crypto_box as cb

fails = 0


def check(name, cond):
    global fails
    print(f'  {"PASS" if cond else "FAIL"}  {name}')
    if not cond:
        fails += 1


def rejects(name, fn, *expected):
    """Assert that fn() raises one of `expected`, not that it merely fails."""
    try:
        fn()
    except expected:
        check(name, True)
        return
    except Exception as exc:
        check(f'{name} (raised {exc.__class__.__name__} instead)', False)
        return
    check(f'{name} (returned plaintext!)', False)


PASS = 'correct horse battery staple'
SMALL_CHUNK = 4096          # forces many chunks out of small test payloads

print('Round trips')
for label, data in [
    ('empty', b''),
    ('one byte', b'x'),
    ('exactly one chunk', os.urandom(SMALL_CHUNK)),
    ('one chunk + 1', os.urandom(SMALL_CHUNK + 1)),
    ('many chunks', os.urandom(SMALL_CHUNK * 9 + 17)),
    ('compressible', b'A' * (SMALL_CHUNK * 3)),
]:
    ct = cb.encrypt(data, PASS, chunk=SMALL_CHUNK)
    check(f'{label}: recovers byte-identically',
          cb.decrypt(ct, PASS) == data)
    check(f'{label}: ciphertext is not the plaintext',
          data == b'' or ct[cb.HEADER_SIZE:cb.HEADER_SIZE + len(data)] != data)

print('\nDetection')
plain = os.urandom(1000)
ct = cb.encrypt(plain, PASS, chunk=SMALL_CHUNK)
check('is_encrypted recognises its own output', cb.is_encrypted(ct))
check('is_encrypted rejects a plain tar', not cb.is_encrypted(b'plain data here'))
check('is_encrypted rejects a short buffer', not cb.is_encrypted(b'VIDCRYP1'[:4]))
check('header reports the true plaintext size',
      cb.header_info(ct)['plain_size'] == len(plain))

print('\nWrong keys and tampering')
rejects('wrong passphrase is rejected',
        lambda: cb.decrypt(ct, 'not the passphrase'), cb.WrongPassphrase)
rejects('empty passphrase is rejected',
        lambda: cb.decrypt(ct, ''), ValueError)

body = bytearray(ct)
body[cb.HEADER_SIZE + 5] ^= 0x01
rejects('a flipped ciphertext bit is rejected',
        lambda: cb.decrypt(bytes(body), PASS),
        cb.WrongPassphrase, cb.CorruptCiphertext)

# The header is authenticated as associated data, so editing it -- for example
# to claim weaker KDF parameters -- must not go unnoticed.
hdr = bytearray(ct)
hdr[10] = 4                                     # log2(N) 15 -> 4
rejects('a weakened KDF parameter is rejected',
        lambda: cb.decrypt(bytes(hdr), PASS),
        cb.WrongPassphrase, cb.CorruptCiphertext)

# Truncation: drop the last chunk. The header's plaintext length catches the
# short read even before the final-chunk flag does.
sealed = SMALL_CHUNK + cb.TAG_BYTES
rejects('truncation is rejected',
        lambda: cb.decrypt(ct[:-sealed], PASS),
        cb.CorruptCiphertext)

# Reordering: swap two whole sealed chunks. Each still authenticates on its
# own; only the index in the associated data makes this fail.
multi = cb.encrypt(os.urandom(SMALL_CHUNK * 4), PASS, chunk=SMALL_CHUNK)
head = multi[:cb.HEADER_SIZE]
chunks = [multi[cb.HEADER_SIZE + i * sealed: cb.HEADER_SIZE + (i + 1) * sealed]
          for i in range(4)]
swapped = head + chunks[1] + chunks[0] + chunks[2] + chunks[3]
rejects('reordered chunks are rejected',
        lambda: cb.decrypt(swapped, PASS),
        cb.WrongPassphrase, cb.CorruptCiphertext)

duplicated = head + chunks[0] + chunks[0] + chunks[2] + chunks[3]
rejects('a duplicated chunk is rejected',
        lambda: cb.decrypt(duplicated, PASS),
        cb.WrongPassphrase, cb.CorruptCiphertext)

# Splicing: chunks from a different file, encrypted under the same passphrase.
other = cb.encrypt(os.urandom(SMALL_CHUNK * 4), PASS, chunk=SMALL_CHUNK)
foreign = other[cb.HEADER_SIZE: cb.HEADER_SIZE + sealed]
spliced = head + chunks[0] + foreign + chunks[2] + chunks[3]
rejects('a chunk spliced from another file is rejected',
        lambda: cb.decrypt(spliced, PASS),
        cb.WrongPassphrase, cb.CorruptCiphertext)

rejects('a random buffer is not mistaken for ciphertext',
        lambda: cb.decrypt(os.urandom(500), PASS), cb.CorruptCiphertext)

print('\nSalting')
a = cb.encrypt(plain, PASS, chunk=SMALL_CHUNK)
b = cb.encrypt(plain, PASS, chunk=SMALL_CHUNK)
check('the same input twice gives different ciphertext', a != b)
check('both still decrypt', cb.decrypt(a, PASS) == cb.decrypt(b, PASS) == plain)

print('\nVerifier')
salt = cb.header_info(ct)['salt']
v = cb.verifier(PASS, salt)
check('verifier matches for the right passphrase', cb.verifier(PASS, salt) == v)
check('verifier differs for the wrong one', cb.verifier('other', salt) != v)
check('verifier differs under a different salt',
      cb.verifier(PASS, cb.new_salt()) != v)
check('verifier is not the key itself', len(v) == 64 and v != salt.hex())

print()
if fails:
    print(f'{fails} check(s) FAILED')
    sys.exit(1)
print('All encryption checks passed.')

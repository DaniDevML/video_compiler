"""Authenticated encryption for archives on their way to YouTube.

The threat model is simple and worth stating, because it decides the design:
an unlisted YouTube video is *not* private. Anyone with the link can fetch it,
YouTube itself stores it indefinitely, and the whole point of this project is
that the bytes are recoverable. Encryption here is what makes "recoverable by
anyone" into "recoverable by you".

So the archive is encrypted **before** it is turned into frames, which means
YouTube never holds plaintext -- not the file contents, not the tar structure,
not the filenames. What it holds is indistinguishable from random bytes, which
is also the input the pixel codec handles best.

Construction
------------
AES-256-GCM over fixed-size chunks, keyed by scrypt over the passphrase.

Chunking is not an optimisation, it is a requirement: GCM has a hard limit of
about 64 GB per (key, nonce) pair, and holding an entire multi-gigabyte
ciphertext before its single tag can be checked would mean trusting unverified
plaintext. Per-chunk tags let the decoder reject damage as it goes.

Three things are bound into every chunk's associated data, and each one closes
an attack that per-chunk tags would otherwise open:

* the file header -- so the KDF parameters cannot be swapped for weaker ones
* the chunk index -- so chunks cannot be reordered or duplicated
* a final-chunk flag -- so the stream cannot be truncated

The plaintext length is recorded in the header as well, which catches
truncation a second way.
"""

import hashlib
import os
import struct

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

MAGIC = b'VIDCRYP1'

# '<8s' magic, 'B' version, 'B' kdf id, 'B' log2(N), 'B' r, 'B' p, 3 pad,
# '16s' salt, '4s' nonce prefix, 'I' chunk size, 'Q' plaintext length.
HEADER_FORMAT = '<8sBBBBB3x16s4sIQ'
HEADER_SIZE = struct.calcsize(HEADER_FORMAT)   # 48

VERSION = 1
KDF_SCRYPT = 1

# scrypt at N=2^15, r=8, p=1 costs about 32 MB and a tenth of a second. That is
# a deliberate trade: it is imperceptible once per file, and it multiplies the
# cost of guessing a weak passphrase by the same factor.
SCRYPT_LOG_N = 15
SCRYPT_R = 8
SCRYPT_P = 1

KEY_BYTES = 32          # AES-256
SALT_BYTES = 16
NONCE_PREFIX = 4        # + 8-byte counter = 12-byte GCM nonce
TAG_BYTES = 16
CHUNK = 1 << 20         # 1 MiB of plaintext per chunk; 16 bytes of tag per MiB


class WrongPassphrase(Exception):
    """The passphrase does not decrypt this archive."""


class CorruptCiphertext(Exception):
    """The ciphertext is damaged, truncated, or was not produced by us."""


def _derive(passphrase: str, salt: bytes, log_n: int, r: int, p: int) -> bytes:
    if not passphrase:
        raise ValueError('A passphrase is required to encrypt or decrypt.')
    kdf = Scrypt(salt=salt, length=KEY_BYTES, n=1 << log_n, r=r, p=p)
    return kdf.derive(passphrase.encode('utf-8'))


def _aad(header: bytes, index: int, final: bool) -> bytes:
    return header + struct.pack('<QB', index, 1 if final else 0)


def is_encrypted(data: bytes) -> bool:
    """Cheap check for the magic. Lets the decoder ask before it needs a key."""
    return len(data) >= HEADER_SIZE and data[:len(MAGIC)] == MAGIC


def header_info(data: bytes) -> dict:
    """Parse the public header. Reveals parameters and size, never content."""
    if not is_encrypted(data):
        raise CorruptCiphertext('Not an encrypted archive.')
    (_magic, version, kdf, log_n, r, p, salt, prefix, chunk,
     plain_size) = struct.unpack(HEADER_FORMAT, data[:HEADER_SIZE])
    return dict(version=version, kdf=kdf, log_n=log_n, r=r, p=p,
                salt=salt, nonce_prefix=prefix, chunk=chunk,
                plain_size=plain_size)


def encrypt(data: bytes, passphrase: str, progress=None, chunk: int = CHUNK) -> bytes:
    """Encrypt an archive. Returns header + sealed chunks."""
    salt = os.urandom(SALT_BYTES)
    prefix = os.urandom(NONCE_PREFIX)
    header = struct.pack(HEADER_FORMAT, MAGIC, VERSION, KDF_SCRYPT,
                         SCRYPT_LOG_N, SCRYPT_R, SCRYPT_P,
                         salt, prefix, chunk, len(data))

    if progress:
        progress('Deriving key...')
    key = _derive(passphrase, salt, SCRYPT_LOG_N, SCRYPT_R, SCRYPT_P)
    aes = AESGCM(key)

    n_chunks = max(1, (len(data) + chunk - 1) // chunk)
    out = bytearray(header)
    for i in range(n_chunks):
        piece = data[i * chunk:(i + 1) * chunk]
        nonce = prefix + struct.pack('<Q', i)
        out += aes.encrypt(nonce, piece, _aad(header, i, i == n_chunks - 1))
        if progress and n_chunks > 16 and i % (n_chunks // 16) == 0:
            progress(f'Encrypting... {100 * (i + 1) // n_chunks}%')

    if progress:
        progress(f'Encrypted: {len(data):,} -> {len(out):,} bytes '
                 f'(AES-256-GCM, {n_chunks:,} chunks).')
    return bytes(out)


def decrypt(data: bytes, passphrase: str, progress=None) -> bytes:
    """Decrypt an archive produced by `encrypt`.

    Raises WrongPassphrase if the key is wrong and CorruptCiphertext if the
    bytes are damaged. Those are the same GCM failure, so they are told apart
    by position: the very first chunk failing almost always means the wrong
    key, while a later one means the archive itself is bad.
    """
    info = header_info(data)
    if info['version'] != VERSION:
        raise CorruptCiphertext(
            f'Encrypted with version {info["version"]}, which this build does '
            f'not understand.')
    if info['kdf'] != KDF_SCRYPT:
        raise CorruptCiphertext(f'Unknown key derivation ({info["kdf"]}).')

    header = data[:HEADER_SIZE]
    body = data[HEADER_SIZE:]
    chunk = info['chunk']
    sealed = chunk + TAG_BYTES

    if chunk == 0 or chunk > (1 << 30):
        raise CorruptCiphertext('Implausible chunk size in header.')

    n_chunks = max(1, (info['plain_size'] + chunk - 1) // chunk)

    if progress:
        progress('Deriving key...')
    key = _derive(passphrase, info['salt'], info['log_n'], info['r'], info['p'])
    aes = AESGCM(key)

    out = bytearray()
    for i in range(n_chunks):
        piece = body[i * sealed:(i + 1) * sealed]
        if not piece:
            raise CorruptCiphertext(
                f'Archive ends after {i} of {n_chunks} chunks.')
        nonce = info['nonce_prefix'] + struct.pack('<Q', i)
        try:
            out += aes.decrypt(nonce, piece, _aad(header, i, i == n_chunks - 1))
        except InvalidTag:
            if i == 0:
                raise WrongPassphrase(
                    'Wrong passphrase, or this archive is not intact.')
            raise CorruptCiphertext(
                f'Chunk {i + 1} of {n_chunks} failed authentication -- the '
                f'archive is damaged.')
        if progress and n_chunks > 16 and i % (n_chunks // 16) == 0:
            progress(f'Decrypting... {100 * (i + 1) // n_chunks}%')

    if len(out) != info['plain_size']:
        raise CorruptCiphertext(
            f'Recovered {len(out):,} bytes where the header says '
            f'{info["plain_size"]:,}.')
    return bytes(out)


def new_salt() -> bytes:
    return os.urandom(SALT_BYTES)


def verifier(passphrase: str, salt: bytes) -> str:
    """A stored check that a passphrase is the right one for a file.

    Without this, a wrong passphrase is only discovered after the video has
    been downloaded and decoded -- minutes of work to learn about a typo.

    It runs the *same scrypt* as the real key, then hashes the result with a
    domain separator so the stored value is not the key. That makes guessing
    against a stolen index exactly as expensive as guessing against the
    ciphertext itself, which is the property that matters: a fast hash here
    would have handed an attacker a cheap oracle for a passphrase that is
    otherwise protected by a 32 MB KDF.

    Nor does it leak anything new. The salt lives in the ciphertext header,
    inside a video whose link is in the same index -- so anyone holding the
    index could already mount the identical attack on the archive.
    """
    if not passphrase:
        return ''
    key = _derive(passphrase, salt, SCRYPT_LOG_N, SCRYPT_R, SCRYPT_P)
    return hashlib.sha256(b'vidcompiler-verifier-v1' + key).hexdigest()

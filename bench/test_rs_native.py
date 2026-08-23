"""
The native Reed-Solomon decoder must agree with galois exactly.

Existing videos were encoded with galois, so the C decoder has to implement the
same code: same field, same generator root offset, same systematic layout. This
checks that over randomised error patterns, at every error count from zero up to
the correction limit and past it.

A decoder that silently returns wrong bytes is far worse than a slow one, so the
uncorrectable cases are checked as carefully as the correctable ones.
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ctypes

import numpy as np

import video_encoder as ve
from video_codec import CHUNK_IN, NROOTS

# VIDCOMPILER_RS_DLL points the test at a standalone build of rs.c. Useful when
# the main library is locked by a running job, which Windows does not allow
# overwriting.
_override = os.environ.get('VIDCOMPILER_RS_DLL')
if _override:
    _lib = ctypes.CDLL(_override)
    _lib.rs_decode_batch.restype = ctypes.c_int
    _lib.rs_decode_batch.argtypes = [
        ctypes.POINTER(ctypes.c_uint8), ctypes.POINTER(ctypes.c_int),
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    ]

    def rs_decode_batch_c(chunks, nroots):
        arr = np.ascontiguousarray(chunks, dtype=np.uint8).copy()
        n_chunks, n = arr.shape
        status = np.empty(n_chunks, dtype=np.int32)
        _lib.rs_decode_batch(
            arr.ctypes.data_as(ctypes.POINTER(ctypes.c_uint8)),
            status.ctypes.data_as(ctypes.POINTER(ctypes.c_int)),
            n_chunks, n, nroots, 0)
        return arr, status
    print(f'(using standalone RS build: {_override})\n')
else:
    from native import RS_AVAILABLE, rs_decode_batch_c
    assert RS_AVAILABLE, \
        'native RS decoder not built - run python native/build.py'

CHUNK = 255
T = NROOTS // 2
fails = 0


def check(name, cond):
    global fails
    print(f'  {"PASS" if cond else "FAIL"}  {name}')
    if not cond:
        fails += 1


def encode_chunks(n_chunks, seed):
    rng = np.random.default_rng(seed)
    payload = rng.integers(0, 256, n_chunks * CHUNK_IN, dtype=np.uint8).tobytes()
    enc = ve.rs_encode(payload)
    return (np.frombuffer(payload, dtype=np.uint8).reshape(n_chunks, CHUNK_IN),
            np.frombuffer(enc, dtype=np.uint8).reshape(n_chunks, CHUNK).copy())


def damage(arr, errs_per_chunk, rng):
    out = arr.copy()
    for i in range(len(out)):
        if errs_per_chunk == 0:
            continue
        pos = rng.choice(CHUNK, size=errs_per_chunk, replace=False)
        out[i, pos] ^= rng.integers(1, 256, size=errs_per_chunk, dtype=np.uint8)
    return out


print(f'RS(255,{CHUNK_IN}), corrects up to {T} symbol errors per chunk\n')

print('[1] every error count from 0 to the correction limit')
msg, clean = encode_chunks(200, seed=1)
for errs in range(0, T + 1):
    rng = np.random.default_rng(1000 + errs)
    bad = damage(clean, errs, rng)
    got, status = rs_decode_batch_c(bad, NROOTS)
    ok = np.array_equal(got[:, :CHUNK_IN], msg) and np.all(status == errs)
    check(f'{errs:>2} errors/chunk repaired, count reported correctly', ok)

print('\n[2] past the correction limit: must report failure, not guess')
for errs in (T + 1, T + 3, T + 10, 60):
    rng = np.random.default_rng(2000 + errs)
    bad = damage(clean, errs, rng)
    got, status = rs_decode_batch_c(bad, NROOTS)
    # Every chunk should either be flagged, or -- very rarely -- decode to
    # something. What must never happen is a chunk reported as successfully
    # corrected while holding the wrong message.
    claimed_ok = status >= 0
    wrong = np.any(got[claimed_ok][:, :CHUNK_IN] != msg[claimed_ok]) \
        if claimed_ok.any() else False
    check(f'{errs:>2} errors/chunk: no chunk claims success with wrong bytes',
          not wrong)

print('\n[3] burst errors (consecutive symbols), which cluster differently')
for burst in (5, 10, T):
    rng = np.random.default_rng(3000 + burst)
    bad = clean.copy()
    for i in range(len(bad)):
        s = int(rng.integers(0, CHUNK - burst))
        bad[i, s:s + burst] ^= rng.integers(1, 256, size=burst, dtype=np.uint8)
    got, status = rs_decode_batch_c(bad, NROOTS)
    check(f'burst of {burst} consecutive symbols repaired',
          np.array_equal(got[:, :CHUNK_IN], msg))

print('\n[4] agreement with galois on the same damaged input')
import video_decoder as vd
rng = np.random.default_rng(4242)
bad = damage(clean, T - 2, rng)
got_c, _ = rs_decode_batch_c(bad, NROOTS)
got_g = np.frombuffer(vd._rs_decode_parallel(vd._RS, bad),
                      dtype=np.uint8).reshape(-1, CHUNK_IN)
check('native and galois produce identical output',
      np.array_equal(got_c[:, :CHUNK_IN], got_g))

print('\n[5] clean data is left untouched')
got, status = rs_decode_batch_c(clean, NROOTS)
check('undamaged chunks unchanged and reported as 0 errors',
      np.array_equal(got, clean) and np.all(status == 0))

print('\n[6] speed against galois, realistic error density')
n = 20000
msg2, clean2 = encode_chunks(n, seed=9)
rng = np.random.default_rng(77)
flat = clean2.reshape(-1).copy()
nerr = int(flat.size * 8 * 1.1e-5)
pos = rng.choice(flat.size, size=nerr, replace=False)
flat[pos] ^= (1 << rng.integers(0, 8, size=nerr)).astype(np.uint8)
bad2 = flat.reshape(n, CHUNK)

rs_decode_batch_c(bad2[:64], NROOTS)          # warm
t0 = time.perf_counter()
got_c, status = rs_decode_batch_c(bad2, NROOTS)
t_c = time.perf_counter() - t0

t0 = time.perf_counter()
got_g = np.frombuffer(vd._rs_decode_parallel(vd._RS, bad2),
                      dtype=np.uint8).reshape(-1, CHUNK_IN)
t_g = time.perf_counter() - t0

mb = n * CHUNK_IN / 1e6
check('native output matches galois on realistic damage',
      np.array_equal(got_c[:, :CHUNK_IN], got_g))
check('native recovered the original message',
      np.array_equal(got_c[:, :CHUNK_IN], msg2))
print(f'    galois : {t_g:7.2f}s  ({mb/t_g:6.1f} MB/s)')
print(f'    native : {t_c:7.2f}s  ({mb/t_c:6.1f} MB/s)   '
      f'{t_g/t_c:.0f}x faster')

print(f'\n{"ALL NATIVE RS TESTS PASSED" if fails == 0 else f"{fails} FAILED"}')
sys.exit(1 if fails else 0)

"""
Reed-Solomon throughput: find a faster route to the same parity bytes.

The v4 path converts the payload to int64 before handing it to galois, which
inflates the working set eightfold before any maths happens. This compares
that against feeding uint8 straight through, and against splitting the work
across threads.

Every variant is checked to produce byte-identical parity to the v4 path.
"""
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import galois

CHUNK_IN = 215
NROOTS = 40
GF = galois.GF(2 ** 8)
RS = galois.ReedSolomon(255, CHUNK_IN)


def v4_encode(data: bytes) -> bytes:
    pad = (-len(data)) % CHUNK_IN
    padded = data + b'\x00' * pad
    chunks = np.frombuffer(padded, dtype=np.uint8).reshape(-1, CHUNK_IN).astype(int)
    enc = RS.encode(GF(chunks))
    return np.asarray(enc, dtype=np.uint8).tobytes()


def uint8_encode(data: bytes) -> bytes:
    """Skip the int64 round trip -- GF(2^8) accepts uint8 directly."""
    pad = (-len(data)) % CHUNK_IN
    padded = data + b'\x00' * pad
    chunks = np.frombuffer(padded, dtype=np.uint8).reshape(-1, CHUNK_IN)
    enc = RS.encode(chunks.view(GF))
    return np.asarray(enc, dtype=np.uint8).tobytes()


def threaded_encode(data: bytes, workers=6) -> bytes:
    """Split the chunk matrix across threads.

    Worth trying because galois dispatches to numba-compiled ufuncs that
    release the GIL for the duration of the call.
    """
    pad = (-len(data)) % CHUNK_IN
    padded = data + b'\x00' * pad
    chunks = np.frombuffer(padded, dtype=np.uint8).reshape(-1, CHUNK_IN)
    n = len(chunks)
    if n < workers * 4:
        return uint8_encode(data)
    bounds = np.linspace(0, n, workers + 1).astype(int)
    parts = [chunks[a:b] for a, b in zip(bounds[:-1], bounds[1:]) if b > a]
    with ThreadPoolExecutor(max_workers=len(parts)) as ex:
        outs = list(ex.map(lambda c: np.asarray(RS.encode(c.view(GF)),
                                                dtype=np.uint8), parts))
    return np.concatenate(outs).tobytes()


def bench(fn, data, repeats=3):
    best = float('inf')
    out = None
    for _ in range(repeats):
        t0 = time.perf_counter()
        out = fn(data)
        best = min(best, time.perf_counter() - t0)
    return best, out


def main(mb=8.0):
    rng = np.random.default_rng(5)
    data = rng.integers(0, 256, size=int(mb * 1024 * 1024),
                        dtype=np.uint8).tobytes()
    print(f'Payload: {len(data)/1e6:.2f} MB, RS({255},{CHUNK_IN})\n')

    # warm up numba JIT so compile time is not measured
    v4_encode(data[:CHUNK_IN * 8])
    uint8_encode(data[:CHUNK_IN * 8])

    ref_t, ref = bench(v4_encode, data)
    print(f'{"variant":<34} {"time":>8} {"MB/s":>9}  identical')
    print('-' * 58)
    print(f'{"v4 (int64 conversion)":<34} {ref_t:>7.3f}s '
          f'{len(data)/ref_t/1e6:>8.1f}  --')

    for label, fn in [('uint8 straight through', uint8_encode),
                      ('uint8 + 6 threads',
                       lambda d: threaded_encode(d, 6)),
                      ('uint8 + 12 threads',
                       lambda d: threaded_encode(d, 12))]:
        t, out = bench(fn, data)
        same = (out == ref)
        print(f'{label:<34} {t:>7.3f}s {len(data)/t/1e6:>8.1f}  '
              f'{"yes" if same else "NO - MISMATCH"}')
        if same:
            print(f'{"":<34} speedup vs v4: {ref_t/t:.2f}x')


if __name__ == '__main__':
    main(float(sys.argv[1]) if len(sys.argv) > 1 else 8.0)

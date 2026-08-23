"""
Reed-Solomon decode cost: what does it actually scale with?

An earlier attempt to correct only the damaged chunks was rejected on a
measurement that did not warm the JIT. galois dispatches to numba, which
compiles on first call for a given shape/type, so an unwarmed timing can be
dominated by compilation rather than work. This repeats the comparison with
every path warmed first.

Usage: python bench/bench_rs_decode.py [n_chunks] [ber]
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np

import video_encoder as ve
import video_decoder as vd
from video_codec import CHUNK_IN

CHUNK = 255


def make(n_chunks, ber, seed=5):
    rng = np.random.default_rng(seed)
    payload = rng.integers(0, 256, n_chunks * CHUNK_IN, dtype=np.uint8).tobytes()
    enc = ve.rs_encode(payload)
    arr = np.frombuffer(enc, dtype=np.uint8).copy().reshape(-1, CHUNK)
    flat = arr.reshape(-1)
    nerr = max(1, int(flat.size * 8 * ber))
    pos = rng.choice(flat.size, size=nerr, replace=False)
    flat[pos] ^= (1 << rng.integers(0, 8, size=nerr)).astype(np.uint8)
    return payload, arr, nerr


def best_of(fn, reps=3):
    t = float('inf')
    for _ in range(reps):
        t0 = time.perf_counter()
        fn()
        t = min(t, time.perf_counter() - t0)
    return t


def main(n_chunks=20000, ber=1.1e-5):
    payload, arr, nerr = make(n_chunks, ber)
    K = CHUNK_IN

    # identify damaged chunks by re-encoding the data half
    expect = np.asarray(
        vd._RS.encode(np.ascontiguousarray(arr[:, :K]).view(vd._GF)),
        dtype=np.uint8)[:, K:]
    dirty = np.any(expect != arr[:, K:], axis=1)
    sub = np.ascontiguousarray(arr[dirty])
    n_dirty = len(sub)

    print(f'{n_chunks:,} chunks ({n_chunks*K/1e6:.2f} MB payload), '
          f'{nerr} byte errors injected')
    print(f'{n_dirty:,} damaged chunks = {n_dirty/n_chunks*100:.2f}%\n')

    # ---- warm every path before timing anything ----
    small = np.ascontiguousarray(arr[:64])
    vd._RS.decode(small.view(vd._GF))
    vd._RS.decode(sub[:min(64, n_dirty)].view(vd._GF))
    vd._rs_decode_parallel(vd._RS, small)
    vd._RS.encode(np.ascontiguousarray(arr[:64, :K]).view(vd._GF))
    print('(JIT warmed)\n')

    print(f'{"path":<44} {"time":>8} {"MB/s":>9}')
    print('-' * 64)

    mb = n_chunks * K / 1e6

    t = best_of(lambda: vd._RS.decode(arr.view(vd._GF)))
    print(f'{"decode all, single call":<44} {t:>7.2f}s {mb/t:>8.1f}')
    t_all_single = t

    t = best_of(lambda: vd._rs_decode_parallel(vd._RS, arr))
    print(f'{"decode all, thread-split (current)":<44} {t:>7.2f}s {mb/t:>8.1f}')
    t_all = t

    t_screen = best_of(lambda: np.asarray(
        vd._RS.encode(np.ascontiguousarray(arr[:, :K]).view(vd._GF)),
        dtype=np.uint8))
    print(f'{"screen only (re-encode + compare)":<44} {t_screen:>7.2f}s '
          f'{mb/t_screen:>8.1f}')

    t_dirty = best_of(lambda: vd._rs_decode_parallel(vd._RS, sub))
    print(f'{"correct damaged only, thread-split":<44} {t_dirty:>7.2f}s')

    total = t_screen + t_dirty
    print(f'{"screened total":<44} {total:>7.2f}s {mb/total:>8.1f}')
    print(f'\ncurrent path        : {t_all:.2f}s')
    print(f'screened path       : {total:.2f}s   -> '
          f'{t_all/total:.2f}x {"faster" if total < t_all else "SLOWER"}')
    print(f'(single-call decode of everything: {t_all_single:.2f}s)')


if __name__ == '__main__':
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 20000,
         float(sys.argv[2]) if len(sys.argv) > 2 else 1.1e-5)

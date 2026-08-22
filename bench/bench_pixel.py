"""
Pixel engine benchmark: v4 C engine vs v5 C engine vs NumPy.

The v4 engine is compiled from the copy of frame_ops.c taken out of git, so
the comparison is measured against the real previous implementation rather
than an estimate.

Reported throughput is payload bytes per second -- the rate at which user data
moves through the pixel stage, not the rate of raw pixel writes.

Usage: python bench/bench_pixel.py [n_frames] [repeats]
"""
import ctypes
import os
import shutil
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np

import video_codec as vc
from native import (PACK_PAD, encode_plane_c, decode_plane_c,
                    encode_plane_packed_c, decode_plane_packed_c,
                    NATIVE_THREADS)

HERE = os.path.dirname(os.path.abspath(__file__))
V4_SRC = os.path.join(HERE, 'frame_ops_v4.c')
V4_DLL = os.path.join(HERE, 'frame_ops_v4.dll')


def build_v4():
    """Compile the previous engine for a like-for-like comparison."""
    if os.path.exists(V4_DLL):
        return True
    gcc = shutil.which('gcc')
    if not gcc:
        return False
    r = subprocess.run([gcc, '-O3', '-march=native', '-shared',
                        '-static', '-static-libgcc',
                        '-o', V4_DLL, V4_SRC], capture_output=True)
    return r.returncode == 0 and os.path.exists(V4_DLL)


def load_v4():
    lib = ctypes.CDLL(V4_DLL)
    p8 = ctypes.POINTER(ctypes.c_uint8)
    lib.encode_plane.restype = None
    lib.encode_plane.argtypes = [p8, p8] + [ctypes.c_int] * 8
    lib.decode_plane.restype = None
    lib.decode_plane.argtypes = [p8, p8] + [ctypes.c_int] * 9
    return lib


def ptr(a):
    return a.ctypes.data_as(ctypes.POINTER(ctypes.c_uint8))


def timeit(fn, repeats):
    best = float('inf')
    for _ in range(repeats):
        t0 = time.perf_counter()
        fn()
        best = min(best, time.perf_counter() - t0)
    return best


def main(n_frames=24, repeats=3):
    p = vc._PY                       # Y plane, 4x4 blocks, 3 bpp
    bpf = p['bpf']
    ph, pw, bs, bx, dr, bpp, m = (p['ph'], p['pw'], p['bs'], p['bx'],
                                  p['dr'], p['bpp'], p['m'])
    payload = n_frames * bpf / 8     # bytes of user data in the Y plane

    rng = np.random.default_rng(3)
    bits = rng.integers(0, 2, size=n_frames * bpf, dtype=np.uint8)
    packed = np.packbits(bits)
    src = np.concatenate([packed, np.zeros(PACK_PAD, dtype=np.uint8)])

    print(f'Y plane {pw}x{ph}, {bs}x{bs} blocks, {bpp} bpp')
    print(f'{n_frames} frames, {payload/1e6:.2f} MB payload, '
          f'{n_frames*ph*pw/1e6:.1f} MB of pixels, best of {repeats}')
    print(f'C library threads: {NATIVE_THREADS}\n')

    have_v4 = build_v4()
    results = {}

    # reference output for correctness cross-check
    ref_frames = encode_plane_c(bits, n_frames, ph, pw, bs, bx, dr,
                                vc.SYNC_ROWS, bpp)

    print(f'{"engine":<34} {"encode":>10} {"decode":>10}')
    print('-' * 56)

    # ---- NumPy ----
    t_enc = timeit(lambda: vc._encode_plane_np(bits, p), 1)
    frames_np = vc._encode_plane_np(bits, p)
    t_dec = timeit(lambda: vc._decode_plane_np(frames_np, p), 1)
    results['numpy'] = (t_enc, t_dec)
    print(f'{"NumPy fallback":<34} {payload/t_enc/1e6:>7.1f} MB/s '
          f'{payload/t_dec/1e6:>7.1f} MB/s')

    # ---- v4 C ----
    if have_v4:
        v4 = load_v4()
        out_px = np.empty(n_frames * ph * pw, dtype=np.uint8)
        out_bits = np.empty(n_frames * bpf, dtype=np.uint8)
        bits_c = np.ascontiguousarray(bits)

        def v4_enc():
            v4.encode_plane(ptr(bits_c), ptr(out_px), n_frames, ph, pw,
                            bs, bx, dr, vc.SYNC_ROWS, bpp)

        def v4_dec():
            v4.decode_plane(ptr(ref_frames), ptr(out_bits), n_frames, ph, pw,
                            bs, bx, dr, vc.SYNC_ROWS, bpp, m)

        t_enc = timeit(v4_enc, repeats)
        t_dec = timeit(v4_dec, repeats)
        results['v4'] = (t_enc, t_dec)
        assert np.array_equal(out_px.reshape(n_frames, ph, pw), ref_frames), \
            'v4 and v5 encoders disagree'
        print(f'{"v4 C (single-thread, byte/bit)":<34} '
              f'{payload/t_enc/1e6:>7.1f} MB/s {payload/t_dec/1e6:>7.1f} MB/s')

    # ---- v5 C, byte-per-bit ----
    t_enc = timeit(lambda: encode_plane_c(bits, n_frames, ph, pw, bs, bx, dr,
                                          vc.SYNC_ROWS, bpp), repeats)
    t_dec = timeit(lambda: decode_plane_c(ref_frames, ph, pw, bs, bx, dr,
                                          vc.SYNC_ROWS, bpp, m), repeats)
    results['v5_bits'] = (t_enc, t_dec)
    print(f'{"v5 C (threaded, byte/bit)":<34} {payload/t_enc/1e6:>7.1f} MB/s '
          f'{payload/t_dec/1e6:>7.1f} MB/s')

    # ---- v5 C, packed ----
    nbytes = n_frames * bpf // 8
    dec_out = np.zeros(nbytes + PACK_PAD, dtype=np.uint8)

    def v5_pack_enc():
        return encode_plane_packed_c(src, 0, bpf, n_frames, ph, pw, bs, bx,
                                     dr, vc.SYNC_ROWS, bpp)

    def v5_pack_dec():
        dec_out[:] = 0
        decode_plane_packed_c(ref_frames, dec_out, 0, bpf, ph, pw, bs, bx,
                              dr, vc.SYNC_ROWS, bpp, m)

    t_enc = timeit(v5_pack_enc, repeats)
    t_dec = timeit(v5_pack_dec, repeats)
    results['v5_packed'] = (t_enc, t_dec)
    assert np.array_equal(v5_pack_enc(), ref_frames), 'packed encode mismatch'
    v5_pack_dec()
    assert np.array_equal(dec_out[:nbytes], packed), 'packed decode mismatch'
    print(f'{"v5 C (threaded, packed bits)":<34} {payload/t_enc/1e6:>7.1f} MB/s '
          f'{payload/t_dec/1e6:>7.1f} MB/s')

    if 'v4' in results:
        e4, d4 = results['v4']
        e5, d5 = results['v5_packed']
        print(f'\nSpeedup v5 packed vs v4 C:  encode {e4/e5:.2f}x   '
              f'decode {d4/d5:.2f}x')
        en, dn = results['numpy']
        print(f'Speedup v5 packed vs NumPy: encode {en/e5:.2f}x   '
              f'decode {dn/d5:.2f}x')
    print('\n(correctness cross-checks passed)')


if __name__ == '__main__':
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 24,
         int(sys.argv[2]) if len(sys.argv) > 2 else 3)

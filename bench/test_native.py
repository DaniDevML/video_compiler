"""
Correctness tests for the native pixel engine.

Checks three things that must hold for the optimisation to be safe:
  1. The C encoder writes pixel-identical planes to the NumPy reference.
     This is the backward-compatibility property: v4 videos already uploaded
     must still decode, so the level tables may not shift.
  2. The packed-bit fast path produces exactly the same planes and bits as
     the byte-per-bit path.
  3. Encode -> decode is lossless, including under pixel noise up to half the
     level spacing.
"""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np

import video_codec as vc
from native import (NATIVE_AVAILABLE, PACKED_AVAILABLE, NATIVE_THREADS,
                    PACK_PAD, encode_plane_c, decode_plane_c,
                    encode_plane_packed_c, decode_plane_packed_c)

assert NATIVE_AVAILABLE, 'native library not built - run python native/build.py'
print(f'native available: {NATIVE_AVAILABLE}   packed path: {PACKED_AVAILABLE}'
      f'   C threads: {NATIVE_THREADS}')

rng = np.random.default_rng(0)
fails = 0


def check(name, cond):
    global fails
    print(f'  {"PASS" if cond else "FAIL"}  {name}')
    if not cond:
        fails += 1


# ---------------------------------------------------------------------------
print('\n[1] C encoder matches the NumPy reference pixel-for-pixel')
for bs, bpp, pw, ph in [(4, 1, 1920, 1080), (4, 2, 1920, 1080),
                        (4, 3, 1920, 1080), (4, 2, 960, 540),
                        (8, 3, 1920, 1080), (2, 2, 1920, 1080)]:
    p = vc._params(bs, pw, ph, bpp)
    n = 3
    bits = rng.integers(0, 2, size=n * p['bpf'], dtype=np.uint8)
    ref = vc._encode_plane_np(bits, p)
    got = encode_plane_c(bits, n, ph, pw, bs, p['bx'], p['dr'],
                         vc.SYNC_ROWS, bpp)
    check(f'encode block={bs} bpp={bpp} {pw}x{ph}', np.array_equal(ref, got))

# ---------------------------------------------------------------------------
print('\n[2] C decoder recovers the original bits exactly')
for bs, bpp, pw, ph in [(4, 1, 1920, 1080), (4, 2, 1920, 1080),
                        (4, 3, 1920, 1080), (4, 4, 1920, 1080),
                        (4, 5, 1920, 1080), (8, 3, 1920, 1080)]:
    p = vc._params(bs, pw, ph, bpp)
    n = 3
    bits = rng.integers(0, 2, size=n * p['bpf'], dtype=np.uint8)
    frames = encode_plane_c(bits, n, ph, pw, bs, p['bx'], p['dr'],
                            vc.SYNC_ROWS, bpp)
    out = decode_plane_c(frames, ph, pw, bs, p['bx'], p['dr'],
                         vc.SYNC_ROWS, bpp, p['m'])
    check(f'roundtrip block={bs} bpp={bpp}',
          np.array_equal(out.reshape(-1), bits))

# ---------------------------------------------------------------------------
print('\n[3] Round-trip survives noise up to half the level spacing')
for bpp in (1, 2, 3, 4):
    bs, pw, ph = 4, 1920, 1080
    p = vc._params(bs, pw, ph, bpp)
    n = 2
    bits = rng.integers(0, 2, size=n * p['bpf'], dtype=np.uint8)
    frames = encode_plane_c(bits, n, ph, pw, bs, p['bx'], p['dr'],
                            vc.SYNC_ROWS, bpp)
    gap = 255 // ((1 << bpp) - 1)
    amp = gap // 2 - 1            # just inside the decision boundary
    noise = rng.integers(-amp, amp + 1, size=frames.shape)
    noisy = np.clip(frames.astype(np.int16) + noise, 0, 255).astype(np.uint8)
    out = decode_plane_c(noisy, ph, pw, bs, p['bx'], p['dr'],
                         vc.SYNC_ROWS, bpp, p['m'])
    check(f'bpp={bpp} with +/-{amp} noise (gap {gap})',
          np.array_equal(out.reshape(-1), bits))

# ---------------------------------------------------------------------------
if PACKED_AVAILABLE:
    print('\n[4] Packed-bit path agrees with the byte-per-bit path')
    for bs, bpp in [(4, 2), (4, 3), (4, 4), (8, 3), (2, 2)]:
        pw, ph = 1920, 1080
        p = vc._params(bs, pw, ph, bpp)
        n = 3
        stride = p['bpf']                    # single plane: stride == plane bits
        assert stride % 8 == 0, 'test assumes byte-aligned frame stride'
        nbytes = n * stride // 8
        packed = rng.integers(0, 256, size=nbytes, dtype=np.uint8)
        src = np.concatenate([packed, np.zeros(PACK_PAD, dtype=np.uint8)])

        bits = np.unpackbits(packed)
        ref = encode_plane_c(bits, n, ph, pw, bs, p['bx'], p['dr'],
                             vc.SYNC_ROWS, bpp)
        got = encode_plane_packed_c(src, 0, stride, n, ph, pw, bs, p['bx'],
                                    p['dr'], vc.SYNC_ROWS, bpp)
        check(f'packed encode block={bs} bpp={bpp}', np.array_equal(ref, got))

        out = np.zeros(nbytes + PACK_PAD, dtype=np.uint8)
        decode_plane_packed_c(got, out, 0, stride, ph, pw, bs, p['bx'],
                              p['dr'], vc.SYNC_ROWS, bpp, p['m'])
        check(f'packed decode block={bs} bpp={bpp}',
              np.array_equal(out[:nbytes], packed))

# ---------------------------------------------------------------------------
print('\n[5] Full v4 YUV frame round-trip through the public API')
n = 3
bits = rng.integers(0, 2, size=n * vc.BITS_PER_FRAME, dtype=np.uint8)
yuv = vc.bits_to_yuv_frames(bits)
back = vc.yuv_frames_to_bits(yuv)
check('bits_to_yuv_frames -> yuv_frames_to_bits',
      np.array_equal(back.reshape(-1), bits))
check('sync detected on all frames', bool(np.all(vc.check_sync_yuv(yuv))))

hdr = vc.pack_header(1234, 7, 5678, 0xDEADBEEF, audio_bytes=42)
hf = vc.header_to_yuv_frame(hdr)
got = vc.yuv_frame_to_header(hf)
check('header frame round-trip',
      got['archive_size'] == 1234 and got['num_data_frames'] == 7
      and got['crc32'] == 0xDEADBEEF and got['magic'] == vc.MAGIC)

print(f'\n{"ALL TESTS PASSED" if fails == 0 else f"{fails} TEST(S) FAILED"}')
sys.exit(1 if fails else 0)

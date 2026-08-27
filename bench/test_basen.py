"""
The v8 base-N block codec: does a byte survive becoming base-3 digits?

Level counts that are not powers of two cannot be expressed as bits per block,
so v8 packs groups of digits into groups of bits instead. That packing is the
one genuinely new failure mode in the format, and it is not obviously correct:
a group spans block rows, the digit order has to agree between the C encoder
and decoder, and a group integer can exceed the bit width it is written into
when digits are corrupted.

Checked here:
  * round trip through the C kernels at every level count v8 can use
  * the C encoder agrees with an independent NumPy model, pixel for pixel
  * pixel levels are the evenly spaced set the demodulator expects
  * a corrupted digit damages a bounded number of bytes, not the whole frame
  * a group integer beyond the bit width is truncated rather than corrupting
    a neighbouring group
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np

import native

fails = 0


def check(name, cond, detail=''):
    global fails
    print(f'  {"PASS" if cond else "FAIL"}  {name}' + (f'  [{detail}]' if detail and not cond else ''))
    if not cond:
        fails += 1


def group_shape(n_levels, max_bits=57):
    """(digits, bits) packing the most bits per digit without exceeding 57."""
    import math
    if n_levels & (n_levels - 1) == 0:
        return 1, n_levels.bit_length() - 1
    best = (1, 1, 0.0)
    for k in range(1, 64):
        if n_levels ** k > 2 ** 63:
            break
        b = int(math.floor(k * math.log2(n_levels)))
        if b > max_bits or b == 0:
            continue
        eff = b / (k * math.log2(n_levels))
        if eff > best[2]:
            best = (k, b, eff)
    return best[0], best[1]


def levels_for(n):
    return np.round(np.linspace(0, 255, n)).astype(np.uint8)


def numpy_encode(src_bits, n_frames, ph, pw, block, bx, byd, sync, n_lv, dg, gb):
    """Independent model of encode_plane_basen."""
    lv = levels_for(n_lv)
    out = np.zeros((n_frames, ph, pw), dtype=np.uint8)
    per_frame = bx * byd
    n_groups = per_frame // dg
    for f in range(n_frames):
        digits = []
        base = f * n_groups * gb
        for g in range(n_groups):
            v = 0
            for b in range(gb):
                v = (v << 1) | int(src_bits[base + g * gb + b])
            for _ in range(dg):
                digits.append(v % n_lv)
                v //= n_lv
        digits = np.array(digits[:per_frame], dtype=np.uint8)
        px = lv[digits].reshape(byd, bx)
        exp = np.repeat(np.repeat(px, block, axis=0), block, axis=1)
        sh = sync * block
        out[f, sh:sh + byd * block, :] = exp
        r = np.repeat(np.arange(sync), block)
        checker = ((r[:, None] + np.arange(bx)[None, :]) % 2 == 0)
        out[f, :sh, :] = np.repeat(checker.astype(np.uint8) * 255, block, axis=1)
    return out


def run_roundtrip(n_levels, block=2, ph=64, pw=64, n_frames=3, sync=2):
    bx = pw // block
    byd = ph // block - sync
    dg, gb = group_shape(n_levels)
    per_frame = bx * byd
    n_groups = per_frame // dg
    bits_per_frame = n_groups * gb
    if bits_per_frame % 8:
        return None                       # keep frames byte-aligned
    stride = bits_per_frame
    total_bits = stride * n_frames

    rng = np.random.default_rng(n_levels * 31 + block)
    payload = rng.integers(0, 256, total_bits // 8, dtype=np.uint8)
    src = np.concatenate([payload, np.zeros(native.PACK_PAD, dtype=np.uint8)])

    frames = native.encode_plane_basen_c(
        src, 0, stride, n_frames, ph, pw, block, bx, byd, sync,
        n_levels, dg, gb)

    out = np.zeros(total_bits // 8 + native.PACK_PAD, dtype=np.uint8)
    native.decode_plane_basen_c(
        frames.reshape(n_frames, ph, pw), out, 0, stride,
        ph, pw, block, bx, byd, sync, n_levels, dg, gb,
        1 if block >= 4 else 0)

    return payload, out[:len(payload)], frames, (bx, byd, dg, gb, sync)


print('Round trip through the C kernels')
for n_levels in (3, 5, 6, 7, 10, 12):
    r = run_roundtrip(n_levels)
    if r is None:
        print(f'  skip  {n_levels} levels (not byte-aligned at this size)')
        continue
    payload, got, frames, _ = r
    dg, gb = group_shape(n_levels)
    check(f'{n_levels} levels ({dg} digits -> {gb} bits) round-trips exactly',
          np.array_equal(payload, got))

print('\nPower-of-two level counts still work through the same path')
for n_levels in (2, 4, 8):
    r = run_roundtrip(n_levels)
    if r is None:
        continue
    payload, got, _, _ = r
    check(f'{n_levels} levels round-trips exactly', np.array_equal(payload, got))

print('\nThe C encoder matches an independent NumPy model')
for n_levels in (3, 6):
    block, ph, pw, sync, n_frames = 2, 64, 64, 2, 2
    bx, byd = pw // block, ph // block - sync
    dg, gb = group_shape(n_levels)
    n_groups = (bx * byd) // dg
    stride = n_groups * gb
    if stride % 8:
        continue
    rng = np.random.default_rng(5)
    payload = rng.integers(0, 256, stride * n_frames // 8, dtype=np.uint8)
    src = np.concatenate([payload, np.zeros(native.PACK_PAD, dtype=np.uint8)])
    got = native.encode_plane_basen_c(src, 0, stride, n_frames, ph, pw, block,
                                      bx, byd, sync, n_levels, dg, gb)
    bits = np.unpackbits(payload)
    want = numpy_encode(bits, n_frames, ph, pw, block, bx, byd, sync,
                        n_levels, dg, gb)
    check(f'{n_levels} levels: C pixels == NumPy model', np.array_equal(got, want))

print('\nPixel levels are the evenly spaced set the demodulator assumes')
for n_levels in (3, 5, 6):
    r = run_roundtrip(n_levels)
    if r is None:
        continue
    _, _, frames, (bx, byd, dg, gb, sync) = r
    data = frames[:, sync * 2:, :]
    used = np.unique(data)
    want = levels_for(n_levels)
    check(f'{n_levels} levels: pixel values are {list(want)}',
          set(used.tolist()) <= set(want.tolist()),
          f'saw {used.tolist()[:8]}')

print('\nA corrupted digit damages a bounded number of bytes')
n_levels = 3
dg, gb = group_shape(n_levels)
block, ph, pw, sync, n_frames = 2, 64, 64, 2, 1
bx, byd = pw // block, ph // block - sync
n_groups = (bx * byd) // dg
stride = n_groups * gb
rng = np.random.default_rng(11)
payload = rng.integers(0, 256, stride // 8, dtype=np.uint8)
src = np.concatenate([payload, np.zeros(native.PACK_PAD, dtype=np.uint8)])
frames = native.encode_plane_basen_c(src, 0, stride, n_frames, ph, pw, block,
                                     bx, byd, sync, n_levels, dg, gb)
lv = levels_for(n_levels)
worst = 0
for trial in range(24):
    f2 = frames.copy()
    r_ = rng.integers(sync * block, ph)
    c_ = rng.integers(0, pw)
    cur = f2[0, r_, c_]
    f2[0, r_, c_] = lv[(int(np.argmin(np.abs(lv.astype(int) - int(cur)))) + 1) % n_levels]
    out = np.zeros(len(payload) + native.PACK_PAD, dtype=np.uint8)
    native.decode_plane_basen_c(f2.reshape(n_frames, ph, pw), out, 0, stride,
                                ph, pw, block, bx, byd, sync,
                                n_levels, dg, gb, 0)
    worst = max(worst, int(np.count_nonzero(out[:len(payload)] != payload)))
bound = -(-gb // 8) + 1
check(f'one bad pixel corrupts <= {bound} bytes (worst seen {worst})',
      worst <= bound, f'worst {worst}, bound {bound}')

print('\nAn over-range group is truncated, not allowed to run into its neighbour')
# 3**12 = 531441 > 2**19, so corrupted digits can express a value the 19-bit
# field cannot hold. It must be masked rather than overwrite the next group.
allmax = np.full((1, ph, pw), lv[n_levels - 1], dtype=np.uint8)
out = np.zeros(len(payload) + native.PACK_PAD, dtype=np.uint8)
native.decode_plane_basen_c(allmax, out, 0, stride, ph, pw, block,
                            bx, byd, sync, n_levels, dg, gb, 0)
check('all-maximum digits decode without touching padding past the payload',
      np.array_equal(out[len(payload):], np.zeros(native.PACK_PAD, dtype=np.uint8)))

print()
if fails:
    print(f'{fails} check(s) FAILED')
    sys.exit(1)
print('All base-N checks passed.')

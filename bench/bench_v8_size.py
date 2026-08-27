"""
Density is only half the answer: what does each format cost to upload?

A format that carries 3x the payload per frame but makes each frame 3x more
expensive to encode is worth nothing, because upload time tracks bytes on the
wire. This measures, for each candidate, the H.264 size of a frame at the
shipping quantiser and the resulting **uploaded bytes per payload byte** --
the number that actually decides end-to-end time.

Local encode only; no upload needed to answer this.

Usage: python bench/bench_v8_size.py [n_frames]
"""
import math
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np

import channel as ch

SCRATCH = os.environ.get('VIDCOMPILER_SCRATCH', 'E:/vid_compiler/benchwork/probe')
os.makedirs(SCRATCH, exist_ok=True)
tempfile.tempdir = SCRATCH

W, H, CW, CH_ = ch.W, ch.H, ch.CW, ch.CH
FRAME_BYTES = W * H + 2 * CW * CH_

# (name, luma block, luma levels, chroma block, chroma levels, measured SER)
CANDIDATES = [
    ('v7 default   Y4/4lv  C4/8lv', 4, 4, 4, 8, '1.1e-05 (probe2)'),
    ('v7 robust    Y4/4lv  C2/4lv', 4, 4, 2, 4, '4.3e-07 (probe3)'),
    ('dense        Y2/2lv  C2/4lv', 2, 2, 2, 4, '1.6e-07 (probe3)'),
    ('v8 candidate Y2/3lv  C2/4lv', 2, 3, 2, 4, '2.7e-06 (this branch)'),
]


def bits_per_frame(yb, yl, cb, cl):
    y = ch.SymSpec(W, H, yb, yl)
    c = ch.SymSpec(CW, CH_, cb, cl)
    return y.bits + 2 * c.bits


def payload_bytes_per_frame(yb, yl, cb, cl):
    """Usable bytes after base-N packing, which is not always 100% efficient."""
    y = ch.SymSpec(W, H, yb, yl)
    c = ch.SymSpec(CW, CH_, cb, cl)
    return (pack_bits(y.syms, yl) + 2 * pack_bits(c.syms, cl)) // 8


def best_group(n_levels, max_bits=62):
    """Digits-per-group and bits-per-group for base-N packing.

    Power-of-two level counts pack exactly. Otherwise find the (k, b) with
    n**k >= 2**b that wastes least, capped so the arithmetic stays in 64 bits.
    """
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


def pack_bits(n_syms, n_levels):
    k, b = best_group(n_levels)
    return (n_syms // k) * b


def build(yb, yl, cb, cl, n, rng):
    y = ch.SymSpec(W, H, yb, yl)
    c = ch.SymSpec(CW, CH_, cb, cl)
    Y = ch.modulate_sym(rng.integers(0, yl, n * y.syms, dtype=np.uint8), y, n)
    Cb = ch.modulate_sym(rng.integers(0, cl, n * c.syms, dtype=np.uint8), c, n)
    Cr = ch.modulate_sym(rng.integers(0, cl, n * c.syms, dtype=np.uint8), c, n)
    f = np.empty((n, FRAME_BYTES), dtype=np.uint8)
    f[:, :W * H] = Y.reshape(n, -1)
    f[:, W * H:W * H + CW * CH_] = Cb.reshape(n, -1)
    f[:, W * H + CW * CH_:] = Cr.reshape(n, -1)
    return f


def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 24
    rng = np.random.default_rng(7)
    path = os.path.join(SCRATCH, 'sizecheck.mp4')

    print(f'{n} frames per candidate, encoded at the shipping quantiser (qp44)\n')
    print(f'{"format":<30} {"payload/frame":>13} {"video/frame":>12} '
          f'{"expansion":>10} {"vs v7 payload":>13} {"vs v7 upload":>12}')

    base_payload = base_video = None
    rows = []
    for name, yb, yl, cb, cl, ser in CANDIDATES:
        pay = payload_bytes_per_frame(yb, yl, cb, cl)
        frames = build(yb, yl, cb, cl, n, rng)
        size = ch.encode_video(frames, path, preset='nvenc_qp44')
        vid = size / n
        if base_payload is None:
            base_payload, base_video = pay, vid
        exp = vid / pay
        rows.append((name, pay, vid, exp, ser))
        print(f'{name:<30} {pay:>13,} {vid:>12,.0f} {exp:>9.2f}x '
              f'{pay / base_payload:>12.2f}x '
              f'{(vid / pay) / (base_video / base_payload):>11.2f}x')

    print('\n"vs v7 upload" is bytes uploaded per payload byte, relative to the')
    print('v7 default. Below 1.00 means v8 uploads less for the same file.')
    print('\nmeasured symbol error rate on real YouTube:')
    for name, _p, _v, _e, ser in rows:
        print(f'  {name:<30} {ser}')

    print('\nbase-N packing efficiency:')
    for lv in (2, 3, 4, 5, 6, 8):
        k, b = best_group(lv)
        eff = b / (k * math.log2(lv))
        print(f'  {lv:>2} levels: {k:>2} digits -> {b:>2} bits '
              f'({eff * 100:.1f}% of theoretical, '
              f'1 digit error corrupts <= {math.ceil(b / 8)} byte(s))')


if __name__ == '__main__':
    main()

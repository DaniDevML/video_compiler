"""
Sweep 6: does an adaptive slicer recover bits a fixed slicer loses?

The decoder currently assumes received block levels sit exactly where the
encoder put them. A transcode applies its own gain/offset and pulls levels
toward the middle of the range, so the fixed decision thresholds drift off the
real level centres. If calibrating the slicer per frame cuts the error rate,
that is a density win: it buys the same reliability from a denser
constellation.

Three slicers are compared:
  fixed     - the current nearest-level rule
  minmax    - rescale using the observed extremes of the block means
  quantile  - rescale using the 1st/99th percentile, robust to outliers
"""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np

from channel import (Format, W, H, CW, CH, levels, encode_video,
                     youtube_transcode, decode_video, PlaneSpec)
import channel as ch
import os as _os
import tempfile


def block_means(planes, sp, margin=None):
    n = len(planes)
    if margin is None:
        margin = 1 if sp.block >= 4 else 0
    inner = sp.block - 2 * margin
    sh = sp.sync_rows * sp.block
    data = planes[:, sh:sh + sp.dr * sp.block, :]
    v = data.reshape(n, sp.dr, sp.block, sp.bx, sp.block)
    reg = v[:, :, margin:margin + inner, :, margin:margin + inner]
    return reg.mean(axis=(2, 4))


def slice_fixed(mean, sp):
    lv = sp.lv.astype(np.float64)
    edges = (lv[:-1] + lv[1:]) / 2.0
    return np.searchsorted(edges, mean).astype(np.uint16)


def slice_rescaled(mean, sp, lo, hi):
    """Map [lo,hi] back onto [0,255] per frame, then use the fixed rule."""
    lo = lo[:, None, None]
    hi = hi[:, None, None]
    span = np.maximum(hi - lo, 1e-6)
    norm = (mean - lo) / span * 255.0
    return slice_fixed(np.clip(norm, 0, 255), sp)


def bits_from_sym(sym, sp):
    n = sym.shape[0]
    bits = np.empty((n, sp.dr, sp.bx, sp.bpp), dtype=np.uint8)
    for k in range(sp.bpp):
        bits[:, :, :, k] = (sym >> (sp.bpp - 1 - k)) & 1
    return bits.reshape(-1)


def measure_slicers(fmt, n_frames, preset, yt, seed=7):
    rng = np.random.default_rng(seed)
    bits = rng.integers(0, 2, size=n_frames * fmt.bits, dtype=np.uint8)
    tmp1 = tempfile.mktemp(suffix='.mp4')
    tmp2 = tempfile.mktemp(suffix='.webm' if 'vp9' in yt else '.mp4')
    try:
        frames = fmt.encode(bits, n_frames)
        encode_video(frames, tmp1, preset)
        if yt != 'none':
            youtube_transcode(tmp1, tmp2, yt)
            got = decode_video(tmp2)
        else:
            got = decode_video(tmp1)
        if len(got) < n_frames:
            return None
        got = got[:n_frames]

        want = bits.reshape(n_frames, fmt.bits)
        yb = fmt.y.bits
        cb = fmt.c.bits
        planes = [
            (got[:, :W * H].reshape(n_frames, H, W), fmt.y, 0, yb),
            (got[:, W * H:W * H + CW * CH].reshape(n_frames, CH, CW),
             fmt.c, yb, yb + cb),
            (got[:, W * H + CW * CH:].reshape(n_frames, CH, CW),
             fmt.c, yb + cb, yb + 2 * cb),
        ]

        out = {}
        for name in ('fixed', 'minmax', 'quantile'):
            errs = 0
            for arr, sp, a, b in planes:
                mean = block_means(arr, sp)
                if name == 'fixed':
                    sym = slice_fixed(mean, sp)
                elif name == 'minmax':
                    sym = slice_rescaled(
                        mean, sp,
                        mean.min(axis=(1, 2)), mean.max(axis=(1, 2)))
                else:
                    sym = slice_rescaled(
                        mean, sp,
                        np.percentile(mean, 1, axis=(1, 2)),
                        np.percentile(mean, 99, axis=(1, 2)))
                gotb = bits_from_sym(sym, sp)
                wantb = want[:, a:b].reshape(-1)
                errs += int(np.count_nonzero(gotb != wantb))
            out[name] = errs / (n_frames * fmt.bits)
        return out
    finally:
        for t in (tmp1, tmp2):
            if _os.path.exists(t):
                _os.unlink(t)


CASES = [
    ('v4  Y b4/3 + C b4/2', Format(4, 3, 4, 2)),
    ('    Y b4/2 + C b4/2', Format(4, 2, 4, 2)),
    ('    Y b4/4 + C b4/3', Format(4, 4, 4, 3)),
    ('    Y b8/3 + C b8/2', Format(8, 3, 8, 2)),
]
YTS = ['none', 'yt_vp9_4m', 'yt_avc_8m']
N = 16

print(f'{N} frames per point, upload nvenc_qp30\n')
print(f'{"format":<22} {"channel":<12} {"fixed":>10} {"minmax":>10} {"quantile":>10}')
print('-' * 68)
for label, fmt in CASES:
    for yt in YTS:
        try:
            r = measure_slicers(fmt, N, 'nvenc_qp30', yt)
        except Exception as e:
            print(f'{label:<22} {yt:<12}  ERROR {type(e).__name__}')
            continue
        if r is None:
            print(f'{label:<22} {yt:<12}  n/a')
            continue
        print(f'{label:<22} {yt:<12} {r["fixed"]:>10.2e} '
              f'{r["minmax"]:>10.2e} {r["quantile"]:>10.2e}')

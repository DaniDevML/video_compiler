"""
Channel model + measurement harness.

Provides a configurable block-modulation codec (any block size, any bits per
block, any plane layout) and a YouTube-like transcode simulator, so that
capacity claims can be *measured* rather than guessed.

Nothing here is used at runtime by the app -- it exists to pick the format
constants and to produce the numbers in the README.
"""
import math
import os
import subprocess
import sys
import tempfile

import numpy as np
import imageio_ffmpeg

FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()

W, H = 1920, 1080
CW, CH = W // 2, H // 2
SYNC_ROWS = 2


def popen_kw(**extra):
    kw = dict(**extra)
    if sys.platform == 'win32':
        kw['creationflags'] = subprocess.CREATE_NO_WINDOW
    return kw


def levels(bpp: int) -> np.ndarray:
    """Evenly spaced gray levels spanning 0..255 for a given bits-per-block."""
    n = 1 << bpp
    return np.round(np.linspace(0, 255, n)).astype(np.uint8)


# ---------------------------------------------------------------------------
# Generic plane modulator (NumPy; clarity over speed -- sweep tool only)
# ---------------------------------------------------------------------------

class PlaneSpec:
    def __init__(self, pw, ph, block, bpp, sync_rows=SYNC_ROWS):
        self.pw, self.ph, self.block, self.bpp = pw, ph, block, bpp
        self.bx = pw // block
        self.by = ph // block
        self.sync_rows = sync_rows
        self.dr = self.by - sync_rows
        self.bits = self.bx * self.dr * bpp
        self.lv = levels(bpp)

    def __repr__(self):
        return f'Plane({self.pw}x{self.ph} b{self.block} {self.bpp}bpp -> {self.bits}b)'


def modulate(bits_flat: np.ndarray, sp: PlaneSpec, n: int) -> np.ndarray:
    """bits (n*sp.bits,) -> (n, ph, pw) uint8 pixel planes."""
    d = bits_flat[:n * sp.bits].reshape(n, sp.dr, sp.bx, sp.bpp)
    sym = np.zeros((n, sp.dr, sp.bx), dtype=np.uint16)
    for k in range(sp.bpp):                       # MSB first
        sym = (sym << 1) | d[:, :, :, k]
    px = sp.lv[sym]
    exp = np.repeat(np.repeat(px, sp.block, axis=1), sp.block, axis=2)

    out = np.zeros((n, sp.ph, sp.pw), dtype=np.uint8)
    sh = sp.sync_rows * sp.block
    r = np.repeat(np.arange(sp.sync_rows), sp.block)
    checker = ((r[:, None] + np.arange(sp.bx)[None, :]) % 2 == 0)
    out[:, :sh, :] = np.repeat(checker.astype(np.uint8) * 255, sp.block, axis=1)
    out[:, sh:sh + sp.dr * sp.block, :] = exp
    return out


def demodulate(planes: np.ndarray, sp: PlaneSpec, margin=None) -> np.ndarray:
    """(n, ph, pw) uint8 -> (n*sp.bits,) bits."""
    n = len(planes)
    if margin is None:
        margin = 1 if sp.block >= 4 else 0
    inner = sp.block - 2 * margin
    sh = sp.sync_rows * sp.block
    data = planes[:, sh:sh + sp.dr * sp.block, :]
    v = data.reshape(n, sp.dr, sp.block, sp.bx, sp.block)
    reg = v[:, :, margin:margin + inner, :, margin:margin + inner]
    mean = reg.mean(axis=(2, 4))

    # nearest-level slicing
    edges = (sp.lv[:-1].astype(np.int16) + sp.lv[1:]) / 2.0
    sym = np.searchsorted(edges, mean).astype(np.uint16)

    bits = np.empty((n, sp.dr, sp.bx, sp.bpp), dtype=np.uint8)
    for k in range(sp.bpp):
        bits[:, :, :, k] = (sym >> (sp.bpp - 1 - k)) & 1
    return bits.reshape(-1)


# ---------------------------------------------------------------------------
# Frame format = one Y plane + two chroma planes
# ---------------------------------------------------------------------------

class Format:
    def __init__(self, block_y, bpp_y, block_c, bpp_c, use_chroma=True):
        self.y = PlaneSpec(W, H, block_y, bpp_y)
        self.use_chroma = use_chroma
        self.c = PlaneSpec(CW, CH, block_c, bpp_c) if use_chroma else None
        self.bits = self.y.bits + (2 * self.c.bits if use_chroma else 0)
        self.bytes = self.bits // 8

    @property
    def label(self):
        if self.use_chroma:
            return (f'Y b{self.y.block}/{self.y.bpp}bpp + '
                    f'C b{self.c.block}/{self.c.bpp}bpp')
        return f'Y b{self.y.block}/{self.y.bpp}bpp (no chroma)'

    def encode(self, bits: np.ndarray, n: int) -> np.ndarray:
        """-> (n, frame_bytes) planar yuv420p"""
        yb = self.y.bits
        f = bits[:n * self.bits].reshape(n, self.bits)
        Y = modulate(f[:, :yb].reshape(-1), self.y, n)
        out = np.empty((n, W * H + 2 * CW * CH), dtype=np.uint8)
        out[:, :W * H] = Y.reshape(n, -1)
        if self.use_chroma:
            cb = self.c.bits
            Cb = modulate(f[:, yb:yb + cb].reshape(-1), self.c, n)
            Cr = modulate(f[:, yb + cb:].reshape(-1), self.c, n)
            out[:, W * H:W * H + CW * CH] = Cb.reshape(n, -1)
            out[:, W * H + CW * CH:] = Cr.reshape(n, -1)
        else:
            out[:, W * H:] = 128
        return out

    def decode(self, frames: np.ndarray) -> np.ndarray:
        n = len(frames)
        Y = frames[:, :W * H].reshape(n, H, W)
        yb = demodulate(Y, self.y)
        if not self.use_chroma:
            return yb.reshape(n, -1).reshape(-1)
        Cb = frames[:, W * H:W * H + CW * CH].reshape(n, CH, CW)
        Cr = frames[:, W * H + CW * CH:].reshape(n, CH, CW)
        cbb = demodulate(Cb, self.c)
        crb = demodulate(Cr, self.c)
        out = np.concatenate([
            yb.reshape(n, -1), cbb.reshape(n, -1), crb.reshape(n, -1)], axis=1)
        return out.reshape(-1)


FRAME_BYTES = W * H + 2 * CW * CH

# ---------------------------------------------------------------------------
# Encoder presets
# ---------------------------------------------------------------------------

def _nvenc(qp):
    return ('h264_nvenc', ['-rc', 'constqp', '-qp', str(qp), '-g', '1', '-bf', '0'])


# x264 with every psychovisual trick disabled. CRF mode's adaptive quantisation
# and psy-RDO are tuned for natural images and actively damage flat blocks;
# constant -qp with -aq-mode 0 -psy-rd 0 keeps the quantiser predictable.
def _x264_flat(qp, preset='veryfast'):
    return ('libx264', ['-qp', str(qp), '-preset', preset, '-g', '1',
                        '-aq-mode', '0', '-psy-rd', '0.0:0.0',
                        '-trellis', '0', '-bf', '0'])


ENCODERS = {
    'x264_lossless': ('libx264', ['-qp', '0', '-preset', 'ultrafast', '-g', '1']),
    'x264_crf18':  ('libx264', ['-crf', '18', '-preset', 'veryfast', '-g', '1']),
    'x264_crf23':  ('libx264', ['-crf', '23', '-preset', 'veryfast', '-g', '1']),
    'x264_crf28':  ('libx264', ['-crf', '28', '-preset', 'veryfast', '-g', '1']),
}
for _q in range(0, 52, 2):
    ENCODERS[f'nvenc_qp{_q}'] = _nvenc(_q)
    ENCODERS[f'x264flat_qp{_q}'] = _x264_flat(_q)
for _q in range(0, 52, 2):
    ENCODERS[f'x264flatfast_qp{_q}'] = _x264_flat(_q, 'ultrafast')


def encode_video(frames: np.ndarray, path: str, preset='nvenc_qp18', fps=30):
    codec, params = ENCODERS[preset]
    cmd = [FFMPEG, '-f', 'rawvideo', '-pix_fmt', 'yuv420p', '-s', f'{W}x{H}',
           '-r', str(fps), '-i', 'pipe:0', '-c:v', codec, *params,
           '-pix_fmt', 'yuv420p', '-an', '-y', path]
    p = subprocess.Popen(cmd, **popen_kw(stdin=subprocess.PIPE,
                                         stdout=subprocess.DEVNULL,
                                         stderr=subprocess.DEVNULL))
    p.stdin.write(frames.tobytes())
    p.stdin.close()
    p.wait()
    return os.path.getsize(path)


# ---------------------------------------------------------------------------
# YouTube transcode simulator
# ---------------------------------------------------------------------------
#
# YouTube re-encodes every upload. For 1080p30 it serves an AVC rendition at
# roughly 5-8 Mbps and a VP9 rendition at roughly 2-4 Mbps, with its own
# deblocking and rate control. These presets bracket that range; 'yt_vp9_2m'
# is deliberately harsher than what YouTube normally applies to 1080p30.

# YouTube is quality-targeted with a bitrate ceiling, not a hard CBR cap, and
# it uses slow, well-tuned encoder presets. A hard "-b:v 8M -preset veryfast"
# cap is far harsher than the real service and starves the first frames while
# rate control settles, so these presets use CRF-with-maxrate at -preset medium
# instead. Measurements still need enough frames for rate control to stabilise.

def _yt_avc(crf, cap):
    return ['-c:v', 'libx264', '-crf', str(crf), '-maxrate', cap,
            '-bufsize', str(int(cap[:-1]) * 2) + 'M', '-preset', 'medium',
            '-g', '60']


def _yt_vp9(crf, cap):
    return ['-c:v', 'libvpx-vp9', '-crf', str(crf), '-b:v', cap,
            '-deadline', 'good', '-cpu-used', '4', '-row-mt', '1', '-g', '60']


YT_PRESETS = {
    'none':       None,
    'yt_avc_12m': _yt_avc(20, '12M'),
    'yt_avc_8m':  _yt_avc(23, '8M'),
    'yt_avc_5m':  _yt_avc(26, '5M'),
    'yt_vp9_8m':  _yt_vp9(28, '8M'),
    'yt_vp9_4m':  _yt_vp9(32, '4M'),
    'yt_vp9_2m':  _yt_vp9(36, '2M'),
}


def youtube_transcode(src: str, dst: str, preset: str):
    params = YT_PRESETS[preset]
    if params is None:
        return os.path.getsize(src)
    cmd = [FFMPEG, '-i', src, *params, '-pix_fmt', 'yuv420p', '-an', '-y', dst]
    subprocess.run(cmd, **popen_kw(stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL))
    return os.path.getsize(dst)


def decode_video(path: str) -> np.ndarray:
    tmp = tempfile.mktemp(suffix='.yuv')
    cmd = [FFMPEG, '-i', path, '-f', 'rawvideo', '-pix_fmt', 'yuv420p',
           '-y', tmp]
    subprocess.run(cmd, **popen_kw(stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL))
    n = os.path.getsize(tmp) // FRAME_BYTES
    raw = np.fromfile(tmp, dtype=np.uint8, count=n * FRAME_BYTES)
    os.unlink(tmp)
    return raw.reshape(n, FRAME_BYTES)


# ---------------------------------------------------------------------------
# One full measurement
# ---------------------------------------------------------------------------

def measure(fmt: Format, n_frames=6, preset='nvenc_qp18', yt='none', seed=7):
    """Round-trip random bits through the channel; return BER and size stats."""
    rng = np.random.default_rng(seed)
    bits = rng.integers(0, 2, size=n_frames * fmt.bits, dtype=np.uint8)

    tmp1 = tempfile.mktemp(suffix='.mp4')
    tmp2 = tempfile.mktemp(suffix='.mp4' if 'vp9' not in yt else '.webm')
    try:
        frames = fmt.encode(bits, n_frames)
        up_size = encode_video(frames, tmp1, preset)
        if yt != 'none':
            dl_size = youtube_transcode(tmp1, tmp2, yt)
            got = decode_video(tmp2)
        else:
            dl_size = up_size
            got = decode_video(tmp1)

        if len(got) < n_frames:
            return None
        out = fmt.decode(got[:n_frames])
        ber = float(np.count_nonzero(out != bits)) / len(bits)
        payload = n_frames * fmt.bytes
        return dict(ber=ber, up_size=up_size, dl_size=dl_size,
                    payload=payload,
                    expansion=up_size / payload,
                    dl_expansion=dl_size / payload,
                    bytes_per_frame=fmt.bytes)
    finally:
        for t in (tmp1, tmp2):
            if os.path.exists(t):
                os.unlink(t)


# ---------------------------------------------------------------------------
# Arbitrary level counts (v8)
# ---------------------------------------------------------------------------
#
# Everything above is built on bits per block, so the only densities it can
# express are powers of two: 2, 4, 8, 16 levels. The real YouTube probes put
# the chroma cliff between 4 levels (1.6e-07, essentially clean) and 8 levels
# (3.5e-03, unusable) -- a 20,000x jump across a single rung. The usable
# maximum is somewhere inside that gap and the bit-based code cannot even
# describe it.
#
# These helpers modulate *symbols* rather than bits, so any level count works
# and the ladder can be swept one level at a time. Packing a byte stream into
# base-N digits is a separate, lossless concern; what decides the format is
# the symbol error rate, measured here.

def sym_levels(n: int) -> np.ndarray:
    """n evenly spaced levels spanning 0..255."""
    return np.round(np.linspace(0, 255, n)).astype(np.uint8)


class SymSpec:
    """A plane carrying one base-N symbol per block."""

    def __init__(self, pw, ph, block, n_levels, sync_rows=SYNC_ROWS):
        self.pw, self.ph, self.block = pw, ph, block
        self.n = n_levels
        self.bx = pw // block
        self.by = ph // block
        self.sync_rows = sync_rows
        self.dr = self.by - sync_rows
        self.syms = self.bx * self.dr
        self.lv = sym_levels(n_levels)
        self.spacing = 255.0 / (n_levels - 1) if n_levels > 1 else 255.0

    @property
    def bits(self) -> float:
        return self.syms * math.log2(self.n)

    def __repr__(self):
        return (f'SymSpec({self.pw}x{self.ph} b{self.block} {self.n}lv '
                f'spacing {self.spacing:.1f})')


def modulate_sym(syms: np.ndarray, sp: SymSpec, n: int) -> np.ndarray:
    """(n*sp.syms,) symbols in 0..N-1 -> (n, ph, pw) uint8."""
    s = syms[:n * sp.syms].reshape(n, sp.dr, sp.bx)
    px = sp.lv[s]
    exp = np.repeat(np.repeat(px, sp.block, axis=1), sp.block, axis=2)

    out = np.zeros((n, sp.ph, sp.pw), dtype=np.uint8)
    sh = sp.sync_rows * sp.block
    r = np.repeat(np.arange(sp.sync_rows), sp.block)
    checker = ((r[:, None] + np.arange(sp.bx)[None, :]) % 2 == 0)
    out[:, :sh, :] = np.repeat(checker.astype(np.uint8) * 255, sp.block, axis=1)
    out[:, sh:sh + sp.dr * sp.block, :] = exp
    return out


def demodulate_sym(planes: np.ndarray, sp: SymSpec, margin=None) -> np.ndarray:
    """(n, ph, pw) uint8 -> (n*sp.syms,) symbols, by nearest level."""
    n = len(planes)
    if margin is None:
        margin = 1 if sp.block >= 4 else 0
    inner = sp.block - 2 * margin
    sh = sp.sync_rows * sp.block
    data = planes[:, sh:sh + sp.dr * sp.block, :]
    v = data.reshape(n, sp.dr, sp.block, sp.bx, sp.block)
    reg = v[:, :, margin:margin + inner, :, margin:margin + inner]
    mean = reg.mean(axis=(2, 4))

    edges = (sp.lv[:-1].astype(np.float64) + sp.lv[1:]) / 2.0
    return np.searchsorted(edges, mean).astype(np.uint8).reshape(-1)

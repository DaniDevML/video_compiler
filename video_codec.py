# video_codec.py — Shared codec constants and batch-vectorised encode/decode primitives.
#
# v3 format improvements over v2:
#   • 2 bits per pixel on the Y (luma) plane  → 4 gray levels (0/85/170/255)
#   • 1 bit per pixel on Cb/Cr (chroma) planes → 2× more data for "free"
#   • YUV 4:2:0 frame layout used in full: Y + Cb + Cr
#   • Optional audio channel (FSK steganography via audio_codec.py)
#   • C native fast path via native/ (ctypes shared library)
#   • Backward-compatible: decoder still handles VIDCMPR2 grayscale videos

import struct
from collections import Counter

import numpy as np

# ---------------------------------------------------------------------------
# Codec constants
# ---------------------------------------------------------------------------
BLOCK_SIZE    = 4
FRAME_WIDTH   = 1920
FRAME_HEIGHT  = 1080
FPS           = 30
SAMPLE_MARGIN = 1          # pixels inset from each block edge when sampling
THRESHOLD     = 128        # v2 1-bpp threshold (kept for back-compat decode)

# ── Y (luma) plane ──────────────────────────────────────────────────────────
BLOCKS_X      = FRAME_WIDTH  // BLOCK_SIZE   # 480
BLOCKS_Y      = FRAME_HEIGHT // BLOCK_SIZE   # 270
SYNC_ROWS     = 2
DATA_ROWS     = BLOCKS_Y - SYNC_ROWS         # 268

BPP_Y         = 2          # bits per block on Y plane (4 gray levels)
BPP_C         = 1          # bits per block on Cb/Cr planes

# ── Cb/Cr (chroma) planes — each is half the Y size due to 4:2:0 ────────────
CHROMA_W      = FRAME_WIDTH  // 2            # 960
CHROMA_H      = FRAME_HEIGHT // 2            # 540
BLOCKS_X_C    = CHROMA_W // BLOCK_SIZE       # 240
BLOCKS_Y_C    = CHROMA_H // BLOCK_SIZE       # 135
DATA_ROWS_C   = BLOCKS_Y_C - SYNC_ROWS       # 133

# ── Data capacity per frame ─────────────────────────────────────────────────
BITS_PER_FRAME_Y  = BLOCKS_X   * DATA_ROWS   * BPP_Y   # 257 280
BITS_PER_FRAME_C  = BLOCKS_X_C * DATA_ROWS_C * BPP_C   # 31 920  (per chroma plane)
BITS_PER_FRAME    = BITS_PER_FRAME_Y + 2 * BITS_PER_FRAME_C  # 321 120
BYTES_PER_FRAME   = BITS_PER_FRAME // 8                 # 40 140
# vs v2: 16 080 bytes/frame  →  2.5× improvement

# ── Frame byte sizes (raw pixel counts) ─────────────────────────────────────
Y_PLANE_BYTES  = FRAME_WIDTH * FRAME_HEIGHT      # 2 073 600
CB_PLANE_BYTES = CHROMA_W * CHROMA_H             #   518 400
CR_PLANE_BYTES = CHROMA_W * CHROMA_H             #   518 400
YUV_FRAME_BYTES = Y_PLANE_BYTES + CB_PLANE_BYTES + CR_PLANE_BYTES  # 3 110 400

# ── Gray levels for 2-bpp encoding ──────────────────────────────────────────
# Gap between adjacent levels = 85 units > ±15 typical YouTube H.264 noise.
LEVELS_2BPP = np.array([0, 85, 170, 255], dtype=np.uint8)

# ── RS / header ─────────────────────────────────────────────────────────────
NROOTS        = 40
CHUNK_IN      = 255 - NROOTS               # 215

# v3 format
MAGIC         = b'VIDCMPR3'
HEADER_FORMAT = '<8sIIIIBBBBBIxxx'
# magic(8) archive_size(4) num_data_frames(4) encoded_size(4) crc32(4)
# nroots(1) block_size(1) fps(1) bpp_y(1) planes(1) audio_bytes(4) pad(3)
# = 36 bytes
HEADER_SIZE   = struct.calcsize(HEADER_FORMAT)   # 36
HEADER_REPEAT = 3

# v2 legacy magic (for backward-compatible decode)
MAGIC_V2      = b'VIDCMPR2'
HEADER_FORMAT_V2 = '<8sIIIIBBBx'
HEADER_SIZE_V2   = struct.calcsize(HEADER_FORMAT_V2)

# ---------------------------------------------------------------------------
# Per-plane parameter cache
# ---------------------------------------------------------------------------
_CACHE: dict = {}


def _build(bs: int, pw: int, ph: int, bpp: int) -> dict:
    """Compute all derived params for a plane of size (ph, pw) with given block_size and bpp."""
    bx    = pw // bs
    by    = ph // bs
    dr    = by - SYNC_ROWS
    bpf   = bx * dr * bpp        # bits per frame for this plane
    m     = max(1, bs // 4) if bs >= 8 else 1
    sh    = SYNC_ROWS * bs       # pixel height of sync area
    dh    = dr * bs              # pixel height of data area

    # Sync checkerboard pattern (pixel resolution)
    r_idx    = np.repeat(np.arange(SYNC_ROWS), bs)
    checker  = ((r_idx[:, None] + np.arange(bx)[None, :]) % 2 == 0)
    sync_pat = np.repeat(checker.astype(np.uint8) * 255, bs, axis=1)   # (sh, pw)

    # Expected per-block values for sync check
    r_blk    = np.arange(SYNC_ROWS)[:, None]
    c_blk    = np.arange(bx)[None, :]
    sync_exp = ((r_blk + c_blk) % 2 == 0).astype(np.uint8) * 255       # (SYNC_ROWS, bx)

    return dict(bs=bs, pw=pw, ph=ph, bx=bx, by=by, dr=dr, bpf=bpf, m=m,
                sh=sh, dh=dh, sync_pat=sync_pat, sync_exp=sync_exp, bpp=bpp)


def _params(bs: int,
            pw: int = FRAME_WIDTH,
            ph: int = FRAME_HEIGHT,
            bpp: int = BPP_Y) -> dict:
    key = (bs, pw, ph, bpp)
    if key not in _CACHE:
        _CACHE[key] = _build(bs, pw, ph, bpp)
    return _CACHE[key]


# Pre-build default params at import time
_PY = _params(BLOCK_SIZE, FRAME_WIDTH, FRAME_HEIGHT, BPP_Y)    # Y plane
_PC = _params(BLOCK_SIZE, CHROMA_W,    CHROMA_H,     BPP_C)    # Cb/Cr planes

# ---------------------------------------------------------------------------
# Header pack / unpack
# ---------------------------------------------------------------------------

def pack_header(archive_size, num_data_frames, encoded_size, crc32,
                audio_bytes: int = 0) -> bytes:
    raw = struct.pack(HEADER_FORMAT,
                      MAGIC, archive_size, num_data_frames, encoded_size, crc32,
                      NROOTS, BLOCK_SIZE, FPS,
                      BPP_Y, 3,           # bpp_y, planes (3 = YUV)
                      audio_bytes)
    assert len(raw) == HEADER_SIZE
    return raw


def unpack_header(raw: bytes) -> dict:
    """Parse a v2 or v3 header. Always returns a unified dict."""
    magic = raw[:8]
    if magic == MAGIC_V2:
        sz = HEADER_SIZE_V2
        magic, arch, ndf, enc, crc, nr, bs, fps = struct.unpack(HEADER_FORMAT_V2, raw[:sz])
        return dict(magic=magic, archive_size=arch, num_data_frames=ndf,
                    encoded_size=enc, crc32=crc, nroots=nr, block_size=bs,
                    fps=fps, bpp_y=1, planes=1, audio_bytes=0)
    if magic == MAGIC:
        sz = HEADER_SIZE
        magic, arch, ndf, enc, crc, nr, bs, fps, bpy, planes, abytes = \
            struct.unpack(HEADER_FORMAT, raw[:sz])
        return dict(magic=magic, archive_size=arch, num_data_frames=ndf,
                    encoded_size=enc, crc32=crc, nroots=nr, block_size=bs,
                    fps=fps, bpp_y=bpy, planes=planes, audio_bytes=abytes)
    raise ValueError(f'Unknown header magic: {magic!r}')

# ---------------------------------------------------------------------------
# Bit ↔ byte helpers
# ---------------------------------------------------------------------------

def bytes_to_bits(data: bytes) -> np.ndarray:
    return np.unpackbits(np.frombuffer(data, dtype=np.uint8))


def bits_to_bytes(bits) -> bytes:
    arr = np.asarray(bits, dtype=np.uint8)
    r = len(arr) % 8
    if r:
        arr = np.concatenate([arr, np.zeros(8 - r, dtype=np.uint8)])
    return np.packbits(arr).tobytes()

# ---------------------------------------------------------------------------
# BATCH encode — numpy fallback (used when C library is not built)
# ---------------------------------------------------------------------------

def _encode_plane_np(bits_flat: np.ndarray, p: dict) -> np.ndarray:
    """
    Encode bits into pixel frames for one plane.

    bits_flat : length = N * bpf (each byte is 0 or 1)
    Returns   : (N, ph, pw) uint8
    """
    bs, bx, dr, bpf, bpp = p['bs'], p['bx'], p['dr'], p['bpf'], p['bpp']
    pw, ph = p['pw'], p['ph']
    N = len(bits_flat) // bpf

    data = bits_flat[:N * bpf].reshape(N, dr, bx, bpp)

    if bpp == 1:
        pixels = data[:, :, :, 0].astype(np.uint8) * np.uint8(255)   # (N, dr, bx)
    else:  # bpp == 2 : MSB-first → symbol 0-3
        sym    = data[:, :, :, 0].astype(np.uint16) * 2 + data[:, :, :, 1]
        pixels = LEVELS_2BPP[sym.astype(np.uint8)]                    # (N, dr, bx)

    expanded = np.repeat(np.repeat(pixels, bs, axis=1), bs, axis=2)   # (N, dr*bs, bx*bs)

    frames = np.empty((N, ph, pw), dtype=np.uint8)
    frames[:, :p['sh'], :]              = p['sync_pat']
    frames[:, p['sh']:p['sh']+p['dh'], :] = expanded
    if ph > p['sh'] + p['dh']:
        frames[:, p['sh'] + p['dh']:, :] = 0
    return frames


def _decode_plane_np(frames: np.ndarray, p: dict) -> np.ndarray:
    """
    Decode pixel frames back to bits.

    frames : (N, ph, pw) uint8
    Returns: (N, bpf) uint8 (each byte is 0 or 1)
    """
    bs, bx, dr, m, bpf, bpp = p['bs'], p['bx'], p['dr'], p['m'], p['bpf'], p['bpp']
    inner     = bs - 2 * m
    n_pix     = inner * inner
    N         = len(frames)

    data  = frames[:, p['sh']:p['sh'] + p['dh'], :]      # (N, dr*bs, bx*bs)
    view  = data.reshape(N, dr, bs, bx, bs)
    inner_region = view[:, :, m:m+inner, :, m:m+inner]
    s     = inner_region.sum(axis=(2, 4), dtype=np.uint32)   # (N, dr, bx)
    mean  = (s // n_pix).astype(np.uint8)                    # integer mean

    if bpp == 1:
        bits = (mean >= 128).astype(np.uint8)                # (N, dr, bx)
        return bits.reshape(N, bpf)
    else:  # bpp == 2
        sym = np.zeros_like(mean, dtype=np.uint8)
        sym[mean >= 43]  = 1
        sym[mean >= 128] = 2
        sym[mean >= 213] = 3
        # Unpack to 2 bits each (MSB first): (N, dr, bx, 2)
        bit0 = (sym >> 1) & 1
        bit1 =  sym       & 1
        return np.stack([bit0, bit1], axis=-1).reshape(N, bpf)


def _check_sync_np(frames: np.ndarray, p: dict,
                   min_accuracy: float = 0.75) -> np.ndarray:
    """Return (N,) bool: True where frame has a valid sync checkerboard."""
    bs, bx, m = p['bs'], p['bx'], p['m']
    inner   = bs - 2 * m
    n_pix   = inner * inner
    N       = len(frames)

    sync  = frames[:, :p['sh'], :]
    view  = sync.reshape(N, SYNC_ROWS, bs, bx, bs)
    s     = view[:, :, m:m+inner, :, m:m+inner].sum(axis=(2, 4), dtype=np.uint32)
    got   = (s // n_pix >= 128).astype(np.uint8) * 255
    acc   = np.mean(got == p['sync_exp'][np.newaxis], axis=(1, 2))
    return acc >= min_accuracy

# ---------------------------------------------------------------------------
# Public encode/decode API — dispatches to C or NumPy
# ---------------------------------------------------------------------------

try:
    from native import (NATIVE_AVAILABLE,
                        encode_plane_c, decode_plane_c, check_sync_c)
except ImportError:
    NATIVE_AVAILABLE = False


def _encode_plane(bits_flat: np.ndarray, p: dict) -> np.ndarray:
    if NATIVE_AVAILABLE:
        return encode_plane_c(
            bits_flat, len(bits_flat) // p['bpf'],
            p['ph'], p['pw'],
            p['bs'], p['bx'], p['dr'], SYNC_ROWS, p['bpp'],
        )
    return _encode_plane_np(bits_flat, p)


def _decode_plane(frames: np.ndarray, p: dict) -> np.ndarray:
    if NATIVE_AVAILABLE:
        return decode_plane_c(
            frames,
            p['ph'], p['pw'],
            p['bs'], p['bx'], p['dr'], SYNC_ROWS, p['bpp'], p['m'],
        )
    return _decode_plane_np(frames, p)


def _check_sync(frames: np.ndarray, p: dict,
                min_accuracy: float = 0.75) -> np.ndarray:
    if NATIVE_AVAILABLE:
        N = len(frames)
        scores = np.array([
            check_sync_c(frames[i], p['ph'], p['pw'],
                         p['bs'], p['bx'], SYNC_ROWS, p['m'])
            for i in range(N)
        ])
        return scores >= int(min_accuracy * 100)
    return _check_sync_np(frames, p, min_accuracy)


# ---------------------------------------------------------------------------
# High-level encode: bits → YUV frames (packed as flat bytes per YUV frame)
# ---------------------------------------------------------------------------

def bits_to_yuv_frames(bits_flat: np.ndarray, block_size: int = BLOCK_SIZE) -> np.ndarray:
    """
    Encode a flat 0/1 bit array into N YUV 4:2:0 frames packed as a
    (N, YUV_FRAME_BYTES) uint8 array.

    bits_flat : length = N * BITS_PER_FRAME
    Returns   : (N, YUV_FRAME_BYTES) uint8
    """
    py = _params(block_size, FRAME_WIDTH, FRAME_HEIGHT, BPP_Y)
    pc = _params(block_size, CHROMA_W,    CHROMA_H,     BPP_C)

    N = len(bits_flat) // BITS_PER_FRAME

    bpf_y = py['bpf']   # 257 280
    bpf_c = pc['bpf']   # 31 920

    flat = bits_flat[:N * BITS_PER_FRAME]
    y_bits  = flat.reshape(N, BITS_PER_FRAME)[:, :bpf_y].reshape(-1)
    cb_bits = flat.reshape(N, BITS_PER_FRAME)[:, bpf_y:bpf_y+bpf_c].reshape(-1)
    cr_bits = flat.reshape(N, BITS_PER_FRAME)[:, bpf_y+bpf_c:].reshape(-1)

    Y_frames  = _encode_plane(y_bits,  py)    # (N, 1080, 1920)
    Cb_frames = _encode_plane(cb_bits, pc)    # (N, 540,  960)
    Cr_frames = _encode_plane(cr_bits, pc)    # (N, 540,  960)

    # Pack into planar YUV 4:2:0 — ffmpeg expects Y plane then Cb then Cr
    out = np.empty((N, YUV_FRAME_BYTES), dtype=np.uint8)
    out[:, :Y_PLANE_BYTES]                              = Y_frames.reshape(N, -1)
    out[:, Y_PLANE_BYTES:Y_PLANE_BYTES+CB_PLANE_BYTES]  = Cb_frames.reshape(N, -1)
    out[:, Y_PLANE_BYTES+CB_PLANE_BYTES:]               = Cr_frames.reshape(N, -1)
    return out


def yuv_frames_to_bits(yuv_frames: np.ndarray,
                       block_size: int = BLOCK_SIZE) -> np.ndarray:
    """
    Decode YUV frames back to a flat bit array.

    yuv_frames : (N, YUV_FRAME_BYTES) uint8
    Returns    : (N, BITS_PER_FRAME) uint8
    """
    py = _params(block_size, FRAME_WIDTH, FRAME_HEIGHT, BPP_Y)
    pc = _params(block_size, CHROMA_W,    CHROMA_H,     BPP_C)
    N  = len(yuv_frames)

    Y_frames  = yuv_frames[:, :Y_PLANE_BYTES].reshape(N, FRAME_HEIGHT, FRAME_WIDTH)
    Cb_frames = yuv_frames[:, Y_PLANE_BYTES:Y_PLANE_BYTES+CB_PLANE_BYTES].reshape(N, CHROMA_H, CHROMA_W)
    Cr_frames = yuv_frames[:, Y_PLANE_BYTES+CB_PLANE_BYTES:].reshape(N, CHROMA_H, CHROMA_W)

    y_bits  = _decode_plane(Y_frames,  py)   # (N, bpf_y)
    cb_bits = _decode_plane(Cb_frames, pc)   # (N, bpf_c)
    cr_bits = _decode_plane(Cr_frames, pc)   # (N, bpf_c)

    return np.concatenate([y_bits, cb_bits, cr_bits], axis=1)  # (N, BITS_PER_FRAME)


# ---------------------------------------------------------------------------
# Sync check for the combined YUV frame
# ---------------------------------------------------------------------------

def check_sync_yuv(yuv_frames: np.ndarray,
                   block_size: int = BLOCK_SIZE) -> np.ndarray:
    """
    Return (N,) bool: True where the Y plane of a YUV frame has a valid sync.
    Only the Y plane is checked (it is the most reliable after compression).
    """
    py = _params(block_size, FRAME_WIDTH, FRAME_HEIGHT, BPP_Y)
    N  = len(yuv_frames)
    Y  = yuv_frames[:, :Y_PLANE_BYTES].reshape(N, FRAME_HEIGHT, FRAME_WIDTH)
    return _check_sync(Y, py)


# ---------------------------------------------------------------------------
# Header frame encode/decode (single YUV frame, majority-vote over 3 copies)
# ---------------------------------------------------------------------------

def header_to_yuv_frame(header_raw: bytes,
                        block_size: int = BLOCK_SIZE) -> np.ndarray:
    """Return a single YUV frame (YUV_FRAME_BYTES,) encoding the header."""
    data     = header_raw * HEADER_REPEAT
    bits     = bytes_to_bits(data)
    # Pad to one full frame
    need     = BITS_PER_FRAME
    if len(bits) < need:
        bits = np.concatenate([bits, np.zeros(need - len(bits), dtype=np.uint8)])
    return bits_to_yuv_frames(bits[:need], block_size=block_size)[0]   # (YUV_FRAME_BYTES,)


def yuv_frame_to_header(yuv_frame: np.ndarray,
                        block_size: int = BLOCK_SIZE) -> dict:
    """Decode header from a single YUV frame, majority-voting across 3 copies."""
    row   = yuv_frame[np.newaxis]           # (1, YUV_FRAME_BYTES)
    bits  = yuv_frames_to_bits(row, block_size=block_size)[0]
    raw   = bits_to_bytes(bits)
    # Pick the right header size for majority voting
    hs    = HEADER_SIZE if raw[:8] == MAGIC else HEADER_SIZE_V2
    copies = [raw[k * hs:(k + 1) * hs] for k in range(HEADER_REPEAT)]
    hb     = bytearray(hs)
    for b in range(hs):
        votes = [c[b] if b < len(c) else 0 for c in copies]
        hb[b] = Counter(votes).most_common(1)[0][0]
    return unpack_header(bytes(hb))


# ---------------------------------------------------------------------------
# v2 back-compat shims (used by the decoder when it sees a VIDCMPR2 video)
# ---------------------------------------------------------------------------

# The v2 decoder used grayscale frames with 1-bpp.
# We expose the old API so video_decoder.py can import it for v2 videos.

def bits_to_frames_batch(bits_flat: np.ndarray,
                         block_size: int = BLOCK_SIZE) -> np.ndarray:
    """v2 compat: 1-bpp grayscale frames. Returns (N, H, W) uint8."""
    p = _params(block_size, FRAME_WIDTH, FRAME_HEIGHT, 1)
    return _encode_plane_np(bits_flat, p)


def frames_to_bits_batch(frames: np.ndarray,
                         block_size: int = BLOCK_SIZE) -> np.ndarray:
    """v2 compat: decode 1-bpp grayscale frames. Returns (N, bpf) uint8."""
    p = _params(block_size, FRAME_WIDTH, FRAME_HEIGHT, 1)
    return _decode_plane_np(frames, p)


def check_sync_batch(frames: np.ndarray,
                     block_size: int = BLOCK_SIZE,
                     min_accuracy: float = 0.75) -> np.ndarray:
    """v2 compat: sync check on grayscale frames."""
    p = _params(block_size, FRAME_WIDTH, FRAME_HEIGHT, 1)
    return _check_sync_np(frames, p, min_accuracy)


def decode_header_frame(frame: np.ndarray,
                        block_size: int = BLOCK_SIZE) -> dict:
    """v2 compat: decode a single grayscale header frame."""
    bits   = frames_to_bits_batch(frame[np.newaxis], block_size=block_size)[0]
    raw    = bits_to_bytes(bits)
    hs     = HEADER_SIZE_V2 if raw[:8] == MAGIC_V2 else HEADER_SIZE
    copies = [raw[k * hs:(k + 1) * hs] for k in range(HEADER_REPEAT)]
    hb     = bytearray(hs)
    for b in range(hs):
        votes = [c[b] if b < len(c) else 0 for c in copies]
        hb[b] = Counter(votes).most_common(1)[0][0]
    return unpack_header(bytes(hb))


def bits_to_frame(bits, block_size: int = BLOCK_SIZE) -> np.ndarray:
    """v2 compat: encode a single 1-bpp grayscale frame. Returns (H, W) uint8."""
    p   = _params(block_size, FRAME_WIDTH, FRAME_HEIGHT, 1)
    return _encode_plane_np(bits[:p['bpf']], p)[0]

# Shared codec constants and batch-vectorised encode/decode primitives.
# BLOCK_SIZE=4 gives 16.5× more data per frame than the old 16-pixel blocks,
# while still surviving YouTube's double-H.264 compression (verified empirically).

import struct
from collections import Counter

import numpy as np

# ---------------------------------------------------------------------------
# Current codec constants
# ---------------------------------------------------------------------------
BLOCK_SIZE    = 4
FRAME_WIDTH   = 1920
FRAME_HEIGHT  = 1080
BLOCKS_X      = FRAME_WIDTH  // BLOCK_SIZE   # 480
BLOCKS_Y      = FRAME_HEIGHT // BLOCK_SIZE   # 270
SYNC_ROWS     = 2
DATA_ROWS     = BLOCKS_Y - SYNC_ROWS         # 268
BITS_PER_FRAME  = BLOCKS_X * DATA_ROWS       # 128 640
BYTES_PER_FRAME = BITS_PER_FRAME // 8        # 16 080
FPS           = 30
SAMPLE_MARGIN = 1          # pixels inset from each block edge when sampling
THRESHOLD     = 128

# RS / header constants (unchanged across versions)
NROOTS        = 40
CHUNK_IN      = 255 - NROOTS               # 215 data bytes per RS chunk
MAGIC         = b'VIDCMPR2'
HEADER_SIZE   = 32
HEADER_REPEAT = 3
HEADER_FORMAT = '<8sIIIIBBBx'
# magic(8) archive_size(4) num_data_frames(4) encoded_size(4) crc32(4)
# nroots(1) block_size(1) fps(1) pad(1) = 32 bytes

# ---------------------------------------------------------------------------
# Per-block-size parameter cache  (supports old 16-px videos transparently)
# ---------------------------------------------------------------------------
_CACHE: dict = {}


def _build(bs: int) -> dict:
    bx   = FRAME_WIDTH  // bs
    by   = FRAME_HEIGHT // bs
    dr   = by - SYNC_ROWS
    bpf  = bx * dr
    m    = max(1, bs // 4) if bs >= 8 else 1   # sample margin
    sh   = SYNC_ROWS * bs
    dh   = dr * bs

    # Sync checkerboard pattern (full pixel resolution)
    r_idx = np.repeat(np.arange(SYNC_ROWS), bs)
    checker = ((r_idx[:, None] + np.arange(bx)[None, :]) % 2 == 0)
    sync_pat = np.repeat(checker.astype(np.uint8) * 255, bs, axis=1)   # (sh, W)

    # Expected per-block sync values for check_sync_batch
    r_blk = np.arange(SYNC_ROWS)[:, None]
    c_blk = np.arange(bx)[None, :]
    sync_exp = ((r_blk + c_blk) % 2 == 0).astype(np.uint8) * 255      # (SYNC_ROWS, bx)

    return dict(bs=bs, bx=bx, by=by, dr=dr, bpf=bpf, m=m,
                sh=sh, dh=dh, sync_pat=sync_pat, sync_exp=sync_exp)


def _params(bs: int) -> dict:
    if bs not in _CACHE:
        _CACHE[bs] = _build(bs)
    return _CACHE[bs]


# Pre-build default params at import time
_P = _params(BLOCK_SIZE)

# ---------------------------------------------------------------------------
# Header pack / unpack
# ---------------------------------------------------------------------------

def pack_header(archive_size, num_data_frames, encoded_size, crc32) -> bytes:
    raw = struct.pack(HEADER_FORMAT,
                      MAGIC, archive_size, num_data_frames, encoded_size, crc32,
                      NROOTS, BLOCK_SIZE, FPS)
    return raw + b'\x00' * (HEADER_SIZE - len(raw))


def unpack_header(raw: bytes) -> dict:
    sz = struct.calcsize(HEADER_FORMAT)
    magic, arch, ndf, enc, crc, nr, bs, fps = struct.unpack(HEADER_FORMAT, raw[:sz])
    return dict(magic=magic, archive_size=arch, num_data_frames=ndf,
                encoded_size=enc, crc32=crc, nroots=nr, block_size=bs, fps=fps)

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
# BATCH encode: bits → (N, H, W) uint8 grayscale frames
# ---------------------------------------------------------------------------

def bits_to_frames_batch(bits_flat: np.ndarray, block_size: int = BLOCK_SIZE) -> np.ndarray:
    """
    Convert a flat uint8 bit array (length = N * bits_per_frame) into an
    (N, FRAME_HEIGHT, FRAME_WIDTH) uint8 array of grayscale frames.

    All N frames are produced by two np.repeat calls — no Python loop.
    """
    p  = _params(block_size)
    bs, bx, dr, bpf = p['bs'], p['bx'], p['dr'], p['bpf']
    N  = len(bits_flat) // bpf

    # (N, dr, bx)  →  scale 0/1 to 0/255
    data = bits_flat[:N * bpf].reshape(N, dr, bx).astype(np.uint8) * np.uint8(255)

    # Tile each block cell into bs×bs pixels
    # (N, dr*bs, bx*bs)
    expanded = np.repeat(np.repeat(data, bs, axis=1), bs, axis=2)

    frames = np.empty((N, FRAME_HEIGHT, FRAME_WIDTH), dtype=np.uint8)
    frames[:, :p['sh'], :]                         = p['sync_pat']   # broadcast
    frames[:, p['sh']:p['sh'] + p['dh'], :]        = expanded
    tail = FRAME_HEIGHT - p['sh'] - p['dh']
    if tail > 0:
        frames[:, p['sh'] + p['dh']:, :]           = 0
    return frames

# ---------------------------------------------------------------------------
# BATCH decode: (N, H, W) uint8 frames → (N, bits_per_frame) uint8
# ---------------------------------------------------------------------------

def frames_to_bits_batch(frames: np.ndarray, block_size: int = BLOCK_SIZE) -> np.ndarray:
    """
    Extract data bits from an (N, FRAME_HEIGHT, FRAME_WIDTH) uint8 array.
    Returns (N, bits_per_frame) uint8.

    Averages the (bs-2m)×(bs-2m) inner region of each block using integer
    arithmetic (uint16 accumulator) — no float32 conversion, ~3× faster than
    the old approach, more robust than single-pixel sampling.
    """
    p   = _params(block_size)
    bs, bx, dr, m, bpf = p['bs'], p['bx'], p['dr'], p['m'], p['bpf']
    inner_size = bs - 2 * m   # 2 for block_size=4
    N  = len(frames)

    # Slice the data region (uint8, no copy until reshape)
    data = frames[:, p['sh']:p['sh'] + p['dh'], :]           # (N, dr*bs, bx*bs) uint8

    # Reshape to expose blocks: (N, dr, bs, bx, bs)
    view  = data.reshape(N, dr, bs, bx, bs)

    # Inner region: (N, dr, inner_size, bx, inner_size) — still uint8 view
    inner = view[:, :, m:m + inner_size, :, m:m + inner_size]

    # Integer sum → (N, dr, bx)  using uint16 to avoid overflow (max 4×255=1020)
    s = inner.sum(axis=(2, 4), dtype=np.uint16)

    # Threshold: pixel is white if mean >= 128, i.e. sum >= 128 * inner_size²
    n_pixels = inner_size * inner_size
    return (s >= THRESHOLD * n_pixels).astype(np.uint8).reshape(N, bpf)

# ---------------------------------------------------------------------------
# BATCH sync detection
# ---------------------------------------------------------------------------

def check_sync_batch(frames: np.ndarray,
                     block_size: int = BLOCK_SIZE,
                     min_accuracy: float = 0.75) -> np.ndarray:
    """
    Return a (N,) bool array: True where the frame contains our sync checkerboard.
    Accepts (N, H, W) uint8 grayscale frames.
    """
    p   = _params(block_size)
    bs, bx, m = p['bs'], p['bx'], p['m']
    inner_size = bs - 2 * m
    n_pixels   = inner_size * inner_size

    sync = frames[:, :p['sh'], :]                              # (N, sh, W) uint8
    view  = sync.reshape(len(frames), SYNC_ROWS, bs, bx, bs)
    inner = view[:, :, m:m + inner_size, :, m:m + inner_size]
    s     = inner.sum(axis=(2, 4), dtype=np.uint16)            # (N, SYNC_ROWS, bx)
    got   = (s >= THRESHOLD * n_pixels).astype(np.uint8) * 255
    acc   = np.mean(got == p['sync_exp'][np.newaxis], axis=(1, 2))
    return acc >= min_accuracy

# ---------------------------------------------------------------------------
# Header frame decode (single frame, with majority vote across 3 copies)
# ---------------------------------------------------------------------------

def decode_header_frame(frame: np.ndarray, block_size: int = BLOCK_SIZE) -> dict:
    bits = frames_to_bits_batch(frame[np.newaxis], block_size=block_size)[0]
    raw  = bits_to_bytes(bits)
    copies = [raw[k * HEADER_SIZE:(k + 1) * HEADER_SIZE] for k in range(HEADER_REPEAT)]
    hb = bytearray(HEADER_SIZE)
    for b in range(HEADER_SIZE):
        votes = [c[b] if b < len(c) else 0 for c in copies]
        hb[b] = Counter(votes).most_common(1)[0][0]
    return unpack_header(bytes(hb))

# ---------------------------------------------------------------------------
# Single-frame shims (used for the header frame in the encoder only)
# ---------------------------------------------------------------------------

def bits_to_frame(bits, block_size: int = BLOCK_SIZE) -> np.ndarray:
    """Encode a single frame (returns (H, W) uint8)."""
    p   = _params(block_size)
    bpf = p['bpf']
    arr = np.asarray(bits[:bpf], dtype=np.uint8).reshape(p['dr'], p['bx']) * np.uint8(255)
    expanded = np.repeat(np.repeat(arr, p['bs'], axis=0), p['bs'], axis=1)
    frame = np.empty((FRAME_HEIGHT, FRAME_WIDTH), dtype=np.uint8)
    frame[:p['sh'], :]                      = p['sync_pat']
    frame[p['sh']:p['sh'] + p['dh'], :]    = expanded
    tail = FRAME_HEIGHT - p['sh'] - p['dh']
    if tail > 0:
        frame[p['sh'] + p['dh']:, :]       = 0
    return frame

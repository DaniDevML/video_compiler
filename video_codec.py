# video_codec.py — Shared codec constants and batch-vectorised encode/decode primitives.
#
# v4 format improvements over v3:
#   • 3 bits per pixel on the Y (luma) plane  → 8 gray levels (gap ≈ 36, above ±15 noise)
#   • 2 bits per pixel on Cb/Cr (chroma) planes → 4 levels (gap = 85)
#   • 1.6× more data per frame (64,080 bytes vs 40,140)
#   • Backward-compatible: decoder handles VIDCMPR4, VIDCMPR3, and VIDCMPR2

import struct
from collections import Counter

import numpy as np

# ---------------------------------------------------------------------------
# Codec constants
# ---------------------------------------------------------------------------
FRAME_WIDTH   = 1920
FRAME_HEIGHT  = 1080
FPS           = 30
SYNC_ROWS     = 2
SAMPLE_MARGIN = 1          # pixels inset from each block edge when sampling
THRESHOLD     = 128        # v2 1-bpp threshold (kept for back-compat decode)

CHROMA_W      = FRAME_WIDTH  // 2            # 960
CHROMA_H      = FRAME_HEIGHT // 2            # 540


class Profile:
    """A frame format: block size and bits per block, per plane.

    Luma and chroma are separate sub-channels with very different survival
    characteristics through YouTube's transcode, so they are configured
    independently rather than sharing one block size.
    """

    def __init__(self, block_y, bpp_y, block_c, bpp_c, name=''):
        self.name = name
        self.block_y, self.bpp_y = block_y, bpp_y
        self.block_c, self.bpp_c = block_c, bpp_c

        self.blocks_x   = FRAME_WIDTH  // block_y
        self.data_rows  = FRAME_HEIGHT // block_y - SYNC_ROWS
        self.blocks_x_c = CHROMA_W // block_c
        self.data_rows_c = CHROMA_H // block_c - SYNC_ROWS

        self.bits_y = self.blocks_x   * self.data_rows   * bpp_y
        self.bits_c = self.blocks_x_c * self.data_rows_c * bpp_c
        self.bits   = self.bits_y + 2 * self.bits_c
        self.bytes  = self.bits // 8

        # The packed fast path requires each plane to start on a byte boundary
        # so parallel frames and planes never share an output byte.
        assert self.bits % 8 == 0 and self.bits_y % 8 == 0 \
            and self.bits_c % 8 == 0, \
            f'profile {name} is not byte-aligned'

        self.plane_bit_offsets = (0, self.bits_y, self.bits_y + self.bits_c)

    def __repr__(self):
        return (f'Profile({self.name}: Y {self.block_y}px/{self.bpp_y}bpp + '
                f'C {self.block_c}px/{self.bpp_c}bpp = {self.bytes:,} B/frame)')


# ── The v5 formats ──────────────────────────────────────────────────────────
#
# Chosen from measurements against real YouTube, not from theory. Three probe
# uploads (bench/diag_youtube_formats.py) measured per-plane error rates for a
# ladder of formats after YouTube's own transcode, and found:
#
#   * Chroma is far more robust than luma. It came back bit-perfect at every
#     density tried up to 3 bits per block, while luma broke between 2 and 3.
#     v4 failed for exactly one reason: 3 bpp on the luma plane.
#   * What luma needs is contrast, not area: 2x2 blocks at 1 bit per block
#     (pure black and white) survived with zero errors, while 4x4 blocks at
#     3 bpp did not.
#
# Density and upload size then pull in opposite directions. High-contrast 2x2
# blocks carry the most payload per frame but are the most expensive thing a
# video codec can be asked to represent, so the frames themselves get much
# bigger (bench/bench_profile_size.py):
#
#   Y4/2 + C4/3    56,100 B/frame   2.34x expansion   BER 1.1e-05
#   Y4/2 + C2/2    96,480 B/frame   3.15x expansion   BER 4.3e-07
#   Y2/1 + C2/2   128,880 B/frame   4.81x expansion   BER 1.6e-07
#
# End-to-end time is dominated by bytes on the wire, not by frame count: on a
# 12 Mbit/s uplink the extra frames of the sparse profile cost well under a
# second of CPU, while the extra bytes of the dense one cost about thirteen
# seconds of upload for the same 8 MB payload. So the default optimises for
# total bytes, and the dense profile is offered for the case where frame count
# genuinely matters -- fitting a very large archive inside YouTube's per-video
# duration limit.
# KNOWN MARGIN PROBLEM. This profile normally runs with about 14% of its
# Reed-Solomon blocks needing repair -- comfortable on average, but not a lot of
# headroom, and YouTube's per-video encoding is not consistent. In a four-shard
# 1 GB upload, three videos came back at 12.6-13.8% blocks repaired and decoded
# cleanly while the fourth was transcoded far harder (534 MB delivered against
# ~658 MB for its siblings, from identical input sizes) and lost the data
# outright: 533,893 of 1,248,917 blocks beyond repair. Re-downloading gave the
# same result, so it is the stored rendition that is damaged, not the transfer.
#
# The measured fix is PROFILE_ROBUST below, which probe 3 put at a 4.3e-07 bit
# error rate against this profile's 1.1e-05 -- 25x the margin -- while also
# carrying 1.7x more per frame. It was not the default because it uploads about
# 35% more bytes, a trade that made sense when upload was serial and looks very
# different now that shards upload concurrently.
PROFILE_V5 = Profile(block_y=4, bpp_y=2, block_c=4, bpp_c=3, name='v5')

# More error margin and more capacity, at a larger upload. Measured at a
# 4.3e-07 bit error rate through real YouTube against the default's 1.1e-05.
PROFILE_ROBUST = Profile(block_y=4, bpp_y=2, block_c=2, bpp_c=2, name='v5-robust')

# Maximum payload per frame. 2.3x the bytes per frame of the default, at
# roughly twice the uploaded size for the same payload.
PROFILE_DENSE = Profile(block_y=2, bpp_y=1, block_c=2, bpp_c=2, name='v5-dense')

# v4, kept so existing uploads still decode. Measured at a 3.6e-02 bit error
# rate through real YouTube, which RS cannot repair -- v4 videos that were
# already damaged on upload are not recoverable, but undamaged ones decode.
PROFILE_V4 = Profile(block_y=4, bpp_y=3, block_c=4, bpp_c=2, name='v4')
PROFILE_V3 = Profile(block_y=4, bpp_y=2, block_c=4, bpp_c=1, name='v3')

# The header frame is written in a deliberately sparse format: it has to be
# readable before the profile it describes is known, so it cannot depend on it.
# Y 4px/1bpp measured zero errors through YouTube.
PROFILE_HEADER = Profile(block_y=4, bpp_y=1, block_c=4, bpp_c=1, name='header')

DEFAULT_PROFILE = PROFILE_V5

# Module-level aliases for the active profile (many call sites read these).
BLOCK_SIZE        = DEFAULT_PROFILE.block_y
BPP_Y             = DEFAULT_PROFILE.bpp_y
BPP_C             = DEFAULT_PROFILE.bpp_c
BLOCKS_X          = DEFAULT_PROFILE.blocks_x
DATA_ROWS         = DEFAULT_PROFILE.data_rows
BLOCKS_X_C        = DEFAULT_PROFILE.blocks_x_c
DATA_ROWS_C       = DEFAULT_PROFILE.data_rows_c
BITS_PER_FRAME_Y  = DEFAULT_PROFILE.bits_y
BITS_PER_FRAME_C  = DEFAULT_PROFILE.bits_c
BITS_PER_FRAME    = DEFAULT_PROFILE.bits          # 1,031,040
BYTES_PER_FRAME   = DEFAULT_PROFILE.bytes         # 128,880

# ── Frame byte sizes (raw pixel counts) ─────────────────────────────────────
Y_PLANE_BYTES  = FRAME_WIDTH * FRAME_HEIGHT      # 2 073 600
CB_PLANE_BYTES = CHROMA_W * CHROMA_H             #   518 400
CR_PLANE_BYTES = CHROMA_W * CHROMA_H             #   518 400
YUV_FRAME_BYTES = Y_PLANE_BYTES + CB_PLANE_BYTES + CR_PLANE_BYTES  # 3 110 400

# ── Gray levels ────────────────────────────────────────────────────────────
# Gap between adjacent levels must exceed ±15 typical YouTube H.264 noise.
LEVELS_2BPP = np.array([0, 85, 170, 255], dtype=np.uint8)            # gap=85
LEVELS_3BPP = np.array([0, 36, 73, 109, 146, 182, 219, 255], dtype=np.uint8)  # gap≈36

# ── RS / header ─────────────────────────────────────────────────────────────
NROOTS        = 40
CHUNK_IN      = 255 - NROOTS               # 215

# v5 format — carries a block size per plane, since luma and chroma now differ
MAGIC         = b'VIDCMPR5'
HEADER_FORMAT = '<8sIIIIBBBBBBBIx'
# magic(8) archive_size(4) num_data_frames(4) encoded_size(4) crc32(4)
# nroots(1) block_y(1) block_c(1) fps(1) bpp_y(1) bpp_c(1) planes(1)
# audio_bytes(4) pad(1)  = 36 bytes
HEADER_SIZE   = struct.calcsize(HEADER_FORMAT)   # 36
HEADER_REPEAT = 5     # 5 copies × majority vote — tolerates 2 fully-corrupted copies

# v4 legacy magic
MAGIC_V4      = b'VIDCMPR4'
HEADER_FORMAT_V4 = '<8sIIIIBBBBBBIxx'
HEADER_SIZE_V4   = struct.calcsize(HEADER_FORMAT_V4)

# v3 legacy magic
MAGIC_V3      = b'VIDCMPR3'
HEADER_FORMAT_V3 = '<8sIIIIBBBBBIxxx'
HEADER_SIZE_V3   = struct.calcsize(HEADER_FORMAT_V3)

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
    # Sampling inset. Blocks smaller than 4px have no room to inset without
    # leaving zero pixels to average, so they are sampled whole.
    m     = 0 if bs < 4 else max(1, bs // 4)
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
                audio_bytes: int = 0, profile: 'Profile' = None) -> bytes:
    p = profile or DEFAULT_PROFILE
    raw = struct.pack(HEADER_FORMAT,
                      MAGIC, archive_size, num_data_frames, encoded_size, crc32,
                      NROOTS, p.block_y, p.block_c, FPS,
                      p.bpp_y, p.bpp_c, 3,   # planes (3 = YUV)
                      audio_bytes)
    assert len(raw) == HEADER_SIZE
    return raw


def unpack_header(raw: bytes) -> dict:
    """Parse a v2, v3, v4, or v5 header. Always returns a unified dict.

    `block_y` and `block_c` are always present; older versions used a single
    block size for both planes, so both fields carry it.
    """
    magic = raw[:8]
    if magic == MAGIC_V2:
        sz = HEADER_SIZE_V2
        magic, arch, ndf, enc, crc, nr, bs, fps = struct.unpack(HEADER_FORMAT_V2, raw[:sz])
        return dict(magic=magic, archive_size=arch, num_data_frames=ndf,
                    encoded_size=enc, crc32=crc, nroots=nr, block_size=bs,
                    block_y=bs, block_c=bs,
                    fps=fps, bpp_y=1, bpp_c=0, planes=1, audio_bytes=0)
    if magic == MAGIC_V3:
        sz = HEADER_SIZE_V3
        magic, arch, ndf, enc, crc, nr, bs, fps, bpy, planes, abytes = \
            struct.unpack(HEADER_FORMAT_V3, raw[:sz])
        return dict(magic=magic, archive_size=arch, num_data_frames=ndf,
                    encoded_size=enc, crc32=crc, nroots=nr, block_size=bs,
                    block_y=bs, block_c=bs,
                    fps=fps, bpp_y=bpy, bpp_c=1, planes=planes, audio_bytes=abytes)
    if magic == MAGIC_V4:
        sz = HEADER_SIZE_V4
        magic, arch, ndf, enc, crc, nr, bs, fps, bpy, bpc, planes, abytes = \
            struct.unpack(HEADER_FORMAT_V4, raw[:sz])
        return dict(magic=magic, archive_size=arch, num_data_frames=ndf,
                    encoded_size=enc, crc32=crc, nroots=nr, block_size=bs,
                    block_y=bs, block_c=bs,
                    fps=fps, bpp_y=bpy, bpp_c=bpc, planes=planes, audio_bytes=abytes)
    if magic == MAGIC:
        sz = HEADER_SIZE
        magic, arch, ndf, enc, crc, nr, by, bc, fps, bpy, bpc, planes, abytes = \
            struct.unpack(HEADER_FORMAT, raw[:sz])
        return dict(magic=magic, archive_size=arch, num_data_frames=ndf,
                    encoded_size=enc, crc32=crc, nroots=nr, block_size=by,
                    block_y=by, block_c=bc,
                    fps=fps, bpp_y=bpy, bpp_c=bpc, planes=planes, audio_bytes=abytes)
    raise ValueError(f'Unknown header magic: {magic!r}')


def profile_from_header(h: dict) -> Profile:
    """The frame format a decoded header describes."""
    return Profile(h['block_y'], h['bpp_y'], h['block_c'],
                   max(1, h['bpp_c']), name=h['magic'].decode(errors='replace'))

# ---------------------------------------------------------------------------
# Sidecar header (carried in the video description)
# ---------------------------------------------------------------------------
#
# The description survives YouTube untouched -- it is text metadata, not pixels
# -- so it is the one perfectly lossless channel available. Putting a copy of
# the header there means the decoder can learn the archive size, frame count
# and CRC without decoding and scanning frames for the header frame at all.
#
# As *capacity* the description is irrelevant: YouTube allows 5000 characters,
# about 3.7 KB of base64, against 1.9 MB/s carried by the pixels. Its value is
# robustness and skipping the header-frame scan, not payload.

SIDECAR_PREFIX = 'vidcompiler-header:'


def encode_sidecar(header_raw: bytes) -> str:
    """Render a header as a single text line for the video description."""
    import base64
    return SIDECAR_PREFIX + base64.b64encode(header_raw).decode('ascii')


def decode_sidecar(text: str) -> dict | None:
    """Recover a header from description text, or None if it isn't there.

    Never raises: the description is attacker-editable free text and a decode
    failure just means falling back to scanning the frames.
    """
    import base64
    if not text:
        return None
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith(SIDECAR_PREFIX):
            continue
        try:
            raw = base64.b64decode(line[len(SIDECAR_PREFIX):], validate=True)
            if raw[:8] not in (MAGIC, MAGIC_V3, MAGIC_V2):
                continue
            return unpack_header(raw)
        except Exception:
            continue
    return None


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
    elif bpp == 2:  # MSB-first → symbol 0-3
        sym    = data[:, :, :, 0].astype(np.uint16) * 2 + data[:, :, :, 1]
        pixels = LEVELS_2BPP[sym.astype(np.uint8)]                    # (N, dr, bx)
    else:  # bpp == 3 : MSB-first → symbol 0-7
        sym = (data[:, :, :, 0].astype(np.uint16) * 4
             + data[:, :, :, 1].astype(np.uint16) * 2
             + data[:, :, :, 2])
        pixels = LEVELS_3BPP[sym.astype(np.uint8)]                    # (N, dr, bx)

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
    elif bpp == 2:
        sym = np.zeros_like(mean, dtype=np.uint8)
        sym[mean >= 43]  = 1
        sym[mean >= 128] = 2
        sym[mean >= 213] = 3
        bit0 = (sym >> 1) & 1
        bit1 =  sym       & 1
        return np.stack([bit0, bit1], axis=-1).reshape(N, bpf)
    else:  # bpp == 3
        sym = np.zeros_like(mean, dtype=np.uint8)
        sym[mean >= 18]  = 1
        sym[mean >= 55]  = 2
        sym[mean >= 91]  = 3
        sym[mean >= 128] = 4
        sym[mean >= 164] = 5
        sym[mean >= 200] = 6
        sym[mean >= 237] = 7
        bit0 = (sym >> 2) & 1
        bit1 = (sym >> 1) & 1
        bit2 =  sym       & 1
        return np.stack([bit0, bit1, bit2], axis=-1).reshape(N, bpf)


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
    from native import (NATIVE_AVAILABLE, PACKED_AVAILABLE, NATIVE_THREADS,
                        PACK_PAD,
                        encode_plane_c, decode_plane_c,
                        encode_plane_packed_c, decode_plane_packed_c,
                        check_sync_c, check_sync_batch_c)
except ImportError:
    NATIVE_AVAILABLE = False
    PACKED_AVAILABLE = False
    NATIVE_THREADS = 1
    PACK_PAD = 4


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
        scores = check_sync_batch_c(
            frames.reshape(len(frames), -1),
            p['ph'], p['pw'], p['bs'], p['bx'], SYNC_ROWS, p['m'],
        )
        return scores >= int(min_accuracy * 100)
    return _check_sync_np(frames, p, min_accuracy)


# ---------------------------------------------------------------------------
# High-level encode: bits → YUV frames (packed as flat bytes per YUV frame)
# ---------------------------------------------------------------------------

def bits_to_yuv_frames(bits_flat: np.ndarray,
                       profile: Profile = None) -> np.ndarray:
    """
    Encode a flat 0/1 bit array into N YUV 4:2:0 frames packed as a
    (N, YUV_FRAME_BYTES) uint8 array.

    bits_flat : length = N * profile.bits
    Returns   : (N, YUV_FRAME_BYTES) uint8
    """
    prof = profile or DEFAULT_PROFILE
    py, pc, _ = _plane_params(prof)

    N = len(bits_flat) // prof.bits

    bpf_y = py['bpf']
    bpf_c = pc['bpf']

    flat = bits_flat[:N * prof.bits]
    y_bits  = flat.reshape(N, prof.bits)[:, :bpf_y].reshape(-1)
    cb_bits = flat.reshape(N, prof.bits)[:, bpf_y:bpf_y+bpf_c].reshape(-1)
    cr_bits = flat.reshape(N, prof.bits)[:, bpf_y+bpf_c:].reshape(-1)

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
                       profile: Profile = None) -> np.ndarray:
    """
    Decode YUV frames back to a flat bit array.

    yuv_frames : (N, YUV_FRAME_BYTES) uint8
    Returns    : (N, profile.bits) uint8
    """
    prof = profile or DEFAULT_PROFILE
    py, pc, _ = _plane_params(prof)
    N  = len(yuv_frames)

    Y_frames  = yuv_frames[:, :Y_PLANE_BYTES].reshape(N, FRAME_HEIGHT, FRAME_WIDTH)
    Cb_frames = yuv_frames[:, Y_PLANE_BYTES:Y_PLANE_BYTES+CB_PLANE_BYTES].reshape(N, CHROMA_H, CHROMA_W)
    Cr_frames = yuv_frames[:, Y_PLANE_BYTES+CB_PLANE_BYTES:].reshape(N, CHROMA_H, CHROMA_W)

    y_bits  = _decode_plane(Y_frames,  py)   # (N, bpf_y)
    cb_bits = _decode_plane(Cb_frames, pc)   # (N, bpf_c)
    cr_bits = _decode_plane(Cr_frames, pc)   # (N, bpf_c)

    return np.concatenate([y_bits, cb_bits, cr_bits], axis=1)  # (N, BITS_PER_FRAME)


# ---------------------------------------------------------------------------
# Packed-byte fast path
# ---------------------------------------------------------------------------
#
# The bit-per-byte representation costs an 8x intermediate array (75 MB for an
# 8 MB payload) on both sides of the pipeline. These entry points hand packed
# bytes straight to the C layer instead.
#
# Both rely on every plane's bit range being byte-aligned within a frame, which
# holds for the v4/v5 geometry and is asserted below. That alignment is also
# what makes the C layer's per-frame parallelism safe: no two frames, and no
# two planes, ever touch the same output byte.

def pad_for_packed(data: bytes | np.ndarray) -> np.ndarray:
    """Copy `data` into a uint8 array with the slack the C bit reader needs."""
    arr = np.frombuffer(data, dtype=np.uint8) if isinstance(data, (bytes, bytearray)) \
        else np.asarray(data, dtype=np.uint8)
    out = np.zeros(len(arr) + PACK_PAD, dtype=np.uint8)
    out[:len(arr)] = arr
    return out


def _plane_params(profile: Profile):
    """(Y, Cb, Cr) parameter dicts for a profile."""
    py = _params(profile.block_y, FRAME_WIDTH, FRAME_HEIGHT, profile.bpp_y)
    pc = _params(profile.block_c, CHROMA_W,    CHROMA_H,     profile.bpp_c)
    return py, pc, pc


def packed_to_yuv_frames(src: np.ndarray, n_frames: int,
                         first_frame: int = 0,
                         profile: Profile = None) -> np.ndarray:
    """Encode packed payload bytes directly into (n, YUV_FRAME_BYTES) frames.

    `src` must come from pad_for_packed(). `first_frame` selects where in the
    stream this batch starts, so batches can be produced without re-slicing.
    """
    p = profile or DEFAULT_PROFILE
    if not PACKED_AVAILABLE:
        start = first_frame * p.bits
        bits = np.unpackbits(src[:-PACK_PAD] if PACK_PAD else src)
        seg = bits[start:start + n_frames * p.bits]
        if len(seg) < n_frames * p.bits:
            seg = np.concatenate(
                [seg, np.zeros(n_frames * p.bits - len(seg), np.uint8)])
        return bits_to_yuv_frames(seg, profile=p)

    base = first_frame * p.bits
    planes = []
    for pp, off in zip(_plane_params(p), p.plane_bit_offsets):
        planes.append(encode_plane_packed_c(
            src, base + off, p.bits, n_frames,
            pp['ph'], pp['pw'], pp['bs'], pp['bx'], pp['dr'], SYNC_ROWS,
            pp['bpp']))

    out = np.empty((n_frames, YUV_FRAME_BYTES), dtype=np.uint8)
    out[:, :Y_PLANE_BYTES] = planes[0].reshape(n_frames, -1)
    out[:, Y_PLANE_BYTES:Y_PLANE_BYTES + CB_PLANE_BYTES] = planes[1].reshape(n_frames, -1)
    out[:, Y_PLANE_BYTES + CB_PLANE_BYTES:] = planes[2].reshape(n_frames, -1)
    return out


def yuv_frames_to_packed(yuv_frames: np.ndarray,
                         profile: Profile = None) -> np.ndarray:
    """Decode frames straight to packed bytes, (n * profile.bytes,) uint8."""
    p = profile or DEFAULT_PROFILE
    n = len(yuv_frames)
    if not PACKED_AVAILABLE:
        bits = yuv_frames_to_bits(yuv_frames, profile=p)
        return np.packbits(bits.reshape(-1))

    Y  = yuv_frames[:, :Y_PLANE_BYTES].reshape(n, FRAME_HEIGHT, FRAME_WIDTH)
    Cb = yuv_frames[:, Y_PLANE_BYTES:Y_PLANE_BYTES + CB_PLANE_BYTES] \
        .reshape(n, CHROMA_H, CHROMA_W)
    Cr = yuv_frames[:, Y_PLANE_BYTES + CB_PLANE_BYTES:] \
        .reshape(n, CHROMA_H, CHROMA_W)

    out = np.zeros(n * p.bytes + PACK_PAD, dtype=np.uint8)
    for frames, pp, off in zip((Y, Cb, Cr), _plane_params(p),
                               p.plane_bit_offsets):
        decode_plane_packed_c(
            frames, out, off, p.bits,
            pp['ph'], pp['pw'], pp['bs'], pp['bx'], pp['dr'], SYNC_ROWS,
            pp['bpp'], pp['m'])
    return out[:n * p.bytes]


# ---------------------------------------------------------------------------
# Sync check for the combined YUV frame
# ---------------------------------------------------------------------------

def check_sync_yuv(yuv_frames: np.ndarray,
                   profile: Profile = None) -> np.ndarray:
    """
    Return (N,) bool: True where the Y plane of a YUV frame has a valid sync.
    Only the Y plane is checked (it is the most reliable after compression).
    """
    prof = profile or DEFAULT_PROFILE
    py, _, _ = _plane_params(prof)
    N  = len(yuv_frames)
    Y  = yuv_frames[:, :Y_PLANE_BYTES].reshape(N, FRAME_HEIGHT, FRAME_WIDTH)
    return _check_sync(Y, py)


# ---------------------------------------------------------------------------
# Header frame encode/decode (single YUV frame, majority-vote over 3 copies)
# ---------------------------------------------------------------------------

def header_to_yuv_frame(header_raw: bytes,
                        profile: Profile = None) -> np.ndarray:
    """Return a single YUV frame (YUV_FRAME_BYTES,) encoding the header.

    Always written in PROFILE_HEADER, never the payload profile: the decoder
    has to read this frame *before* it knows what format the payload uses, so
    the header frame cannot be encoded in the format it describes.
    """
    prof     = profile or PROFILE_HEADER
    data     = header_raw * HEADER_REPEAT
    bits     = bytes_to_bits(data)
    need     = prof.bits
    if len(bits) < need:
        bits = np.concatenate([bits, np.zeros(need - len(bits), dtype=np.uint8)])
    return bits_to_yuv_frames(bits[:need], profile=prof)[0]


def yuv_frame_to_header(yuv_frame: np.ndarray,
                        profile: Profile = None) -> dict:
    """Decode a header from a single YUV frame, majority-voting across copies.

    Tries every known header size, since individual copies may have byte errors
    that corrupt the magic before the vote resolves it.
    """
    prof  = profile or PROFILE_HEADER
    row   = yuv_frame[np.newaxis]           # (1, YUV_FRAME_BYTES)
    bits  = yuv_frames_to_bits(row, profile=prof)[0]
    raw   = bits_to_bytes(bits)

    for hs, expected_magic in [(HEADER_SIZE, MAGIC), (HEADER_SIZE_V4, MAGIC_V4),
                               (HEADER_SIZE_V3, MAGIC_V3),
                               (HEADER_SIZE_V2, MAGIC_V2)]:
        copies = [raw[k * hs:(k + 1) * hs] for k in range(HEADER_REPEAT)]
        hb = bytearray(hs)
        for b in range(hs):
            votes = [c[b] if b < len(c) else 0 for c in copies]
            hb[b] = Counter(votes).most_common(1)[0][0]
        if bytes(hb[:len(expected_magic)]) == expected_magic:
            return unpack_header(bytes(hb))

    raise ValueError('Header magic not recognised after majority vote')


# ---------------------------------------------------------------------------
# v2 back-compat shims (used by the decoder when it sees a VIDCMPR2 video)
# ---------------------------------------------------------------------------

# The v2 decoder used grayscale frames with 1-bpp and 4x4 blocks. These keep
# their own block-size default rather than following the current profile,
# whose geometry has nothing to do with the legacy format.
V2_BLOCK_SIZE = 4

def bits_to_frames_batch(bits_flat: np.ndarray,
                         block_size: int = V2_BLOCK_SIZE) -> np.ndarray:
    """v2 compat: 1-bpp grayscale frames. Returns (N, H, W) uint8."""
    p = _params(block_size, FRAME_WIDTH, FRAME_HEIGHT, 1)
    return _encode_plane_np(bits_flat, p)


def frames_to_bits_batch(frames: np.ndarray,
                         block_size: int = V2_BLOCK_SIZE) -> np.ndarray:
    """v2 compat: decode 1-bpp grayscale frames. Returns (N, bpf) uint8."""
    p = _params(block_size, FRAME_WIDTH, FRAME_HEIGHT, 1)
    return _decode_plane_np(frames, p)


def check_sync_batch(frames: np.ndarray,
                     block_size: int = V2_BLOCK_SIZE,
                     min_accuracy: float = 0.75) -> np.ndarray:
    """v2 compat: sync check on grayscale frames."""
    p = _params(block_size, FRAME_WIDTH, FRAME_HEIGHT, 1)
    return _check_sync_np(frames, p, min_accuracy)


def decode_header_frame(frame: np.ndarray,
                        block_size: int = V2_BLOCK_SIZE) -> dict:
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


def bits_to_frame(bits, block_size: int = V2_BLOCK_SIZE) -> np.ndarray:
    """v2 compat: encode a single 1-bpp grayscale frame. Returns (H, W) uint8."""
    p   = _params(block_size, FRAME_WIDTH, FRAME_HEIGHT, 1)
    return _encode_plane_np(bits[:p['bpf']], p)[0]

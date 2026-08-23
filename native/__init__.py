"""
native/__init__.py — Loads the compiled C frame_ops library via ctypes and
exposes encode_plane / decode_plane / check_sync wrappers.

If the library hasn't been built yet (or compilation failed), every function
transparently falls back to the pure-NumPy implementation from video_codec.py.

Build the C library once with:   python native/build.py
"""

import ctypes
import os
import sys

import numpy as np

_HERE = os.path.dirname(os.path.abspath(__file__))
_LIB_NAME = 'frame_ops.dll' if sys.platform == 'win32' else 'frame_ops.so'
_LIB_PATH = os.path.join(_HERE, _LIB_NAME)

_lib = None


def _load():
    global _lib
    if not os.path.exists(_LIB_PATH):
        return
    try:
        lib = ctypes.CDLL(_LIB_PATH)

        _c_uint8_p = ctypes.POINTER(ctypes.c_uint8)

        lib.encode_plane.restype  = None
        lib.encode_plane.argtypes = [
            _c_uint8_p,     # bits
            _c_uint8_p,     # out
            ctypes.c_int,   # n_frames
            ctypes.c_int,   # plane_h
            ctypes.c_int,   # plane_w
            ctypes.c_int,   # block_size
            ctypes.c_int,   # blocks_x
            ctypes.c_int,   # blocks_y_data
            ctypes.c_int,   # sync_rows
            ctypes.c_int,   # bpp
        ]

        lib.decode_plane.restype  = None
        lib.decode_plane.argtypes = [
            _c_uint8_p,     # frames
            _c_uint8_p,     # out
            ctypes.c_int,   # n_frames
            ctypes.c_int,   # plane_h
            ctypes.c_int,   # plane_w
            ctypes.c_int,   # block_size
            ctypes.c_int,   # blocks_x
            ctypes.c_int,   # blocks_y_data
            ctypes.c_int,   # sync_rows
            ctypes.c_int,   # bpp
            ctypes.c_int,   # margin
        ]

        lib.check_sync.restype  = ctypes.c_int
        lib.check_sync.argtypes = [
            _c_uint8_p,
            ctypes.c_int, ctypes.c_int, ctypes.c_int,
            ctypes.c_int, ctypes.c_int, ctypes.c_int,
        ]

        _c_int_p = ctypes.POINTER(ctypes.c_int)
        lib.check_sync_batch.restype  = None
        lib.check_sync_batch.argtypes = [
            _c_uint8_p,     # frames
            _c_int_p,       # scores_out
            ctypes.c_int,   # n_frames
            ctypes.c_int,   # plane_h
            ctypes.c_int,   # plane_w
            ctypes.c_int,   # block_size
            ctypes.c_int,   # blocks_x
            ctypes.c_int,   # sync_rows
            ctypes.c_int,   # margin
        ]

        # v5 packed-bit entry points. A library built from an older
        # frame_ops.c will not export these; the packed fast path is then
        # simply reported as unavailable and callers use the v4 path.
        if hasattr(lib, 'encode_plane_packed'):
            lib.encode_plane_packed.restype  = None
            lib.encode_plane_packed.argtypes = [
                _c_uint8_p,        # src (packed bytes)
                ctypes.c_uint64,   # bit_offset
                ctypes.c_uint64,   # bit_stride
                _c_uint8_p,        # out
                ctypes.c_int,      # n_frames
                ctypes.c_int,      # plane_h
                ctypes.c_int,      # plane_w
                ctypes.c_int,      # block_size
                ctypes.c_int,      # blocks_x
                ctypes.c_int,      # blocks_y_data
                ctypes.c_int,      # sync_rows
                ctypes.c_int,      # bpp
                ctypes.c_int,      # n_threads
            ]
            lib.decode_plane_packed.restype  = None
            lib.decode_plane_packed.argtypes = [
                _c_uint8_p,        # frames
                _c_uint8_p,        # out (packed, must start zeroed)
                ctypes.c_uint64,   # bit_offset
                ctypes.c_uint64,   # bit_stride
                ctypes.c_int,      # n_frames
                ctypes.c_int,      # plane_h
                ctypes.c_int,      # plane_w
                ctypes.c_int,      # block_size
                ctypes.c_int,      # blocks_x
                ctypes.c_int,      # blocks_y_data
                ctypes.c_int,      # sync_rows
                ctypes.c_int,      # bpp
                ctypes.c_int,      # margin
                ctypes.c_int,      # n_threads
            ]

        if hasattr(lib, 'native_threads'):
            lib.native_threads.restype  = ctypes.c_int
            lib.native_threads.argtypes = []

        if hasattr(lib, 'rs_decode_batch'):
            lib.rs_decode_batch.restype  = ctypes.c_int
            lib.rs_decode_batch.argtypes = [
                _c_uint8_p,                      # data (corrected in place)
                ctypes.POINTER(ctypes.c_int),    # status_out
                ctypes.c_int,                    # n_chunks
                ctypes.c_int,                    # n (codeword length)
                ctypes.c_int,                    # nroots
                ctypes.c_int,                    # n_threads
            ]
            lib.rs_encode_batch.restype  = ctypes.c_int
            lib.rs_encode_batch.argtypes = [
                _c_uint8_p,                      # msg
                _c_uint8_p,                      # out (codewords)
                ctypes.c_int,                    # n_chunks
                ctypes.c_int,                    # n
                ctypes.c_int,                    # nroots
                ctypes.c_int,                    # n_threads
            ]
            lib.rs_max_roots.restype  = ctypes.c_int
            lib.rs_max_roots.argtypes = []

        _lib = lib
    except Exception as e:
        print(f'[native] Warning: could not load {_LIB_NAME}: {e}')


_load()
NATIVE_AVAILABLE = _lib is not None
PACKED_AVAILABLE = NATIVE_AVAILABLE and hasattr(_lib, 'encode_plane_packed')
RS_AVAILABLE     = NATIVE_AVAILABLE and hasattr(_lib, 'rs_decode_batch')

# Threads the C library will use internally. >1 means it was built with
# OpenMP, in which case callers must not add a second layer of parallelism.
NATIVE_THREADS = (_lib.native_threads()
                  if NATIVE_AVAILABLE and hasattr(_lib, 'native_threads')
                  else 1)

# get_bits/put_bits read and write three bytes at a time, so the packed
# buffers handed to the C layer need slack past the last real bit.
PACK_PAD = 4


def _ptr(arr: np.ndarray):
    return arr.ctypes.data_as(ctypes.POINTER(ctypes.c_uint8))


# ─── encode_plane ─────────────────────────────────────────────────────────────

def encode_plane_c(bits: np.ndarray,
                   n_frames: int,
                   plane_h: int, plane_w: int,
                   block_size: int, blocks_x: int,
                   blocks_y_data: int, sync_rows: int,
                   bpp: int) -> np.ndarray:
    """C-accelerated encode. Returns (n_frames, plane_h, plane_w) uint8."""
    out  = np.empty(n_frames * plane_h * plane_w, dtype=np.uint8)
    bits_c = np.ascontiguousarray(bits, dtype=np.uint8)
    _lib.encode_plane(
        _ptr(bits_c), _ptr(out),
        n_frames, plane_h, plane_w,
        block_size, blocks_x, blocks_y_data, sync_rows, bpp,
    )
    return out.reshape(n_frames, plane_h, plane_w)


# ─── decode_plane ─────────────────────────────────────────────────────────────

def decode_plane_c(frames: np.ndarray,
                   plane_h: int, plane_w: int,
                   block_size: int, blocks_x: int,
                   blocks_y_data: int, sync_rows: int,
                   bpp: int, margin: int) -> np.ndarray:
    """C-accelerated decode. Returns (n_frames, bits_per_frame) uint8."""
    n_frames = len(frames)
    bpf      = blocks_y_data * blocks_x * bpp
    out      = np.empty(n_frames * bpf, dtype=np.uint8)
    frames_c = np.ascontiguousarray(frames, dtype=np.uint8)
    _lib.decode_plane(
        _ptr(frames_c), _ptr(out),
        n_frames, plane_h, plane_w,
        block_size, blocks_x, blocks_y_data, sync_rows, bpp, margin,
    )
    return out.reshape(n_frames, bpf)


# ─── check_sync ───────────────────────────────────────────────────────────────

def check_sync_c(frame: np.ndarray,
                 plane_h: int, plane_w: int,
                 block_size: int, blocks_x: int,
                 sync_rows: int, margin: int) -> int:
    """Returns 0–100: percentage of sync blocks that match the checkerboard."""
    frame_c = np.ascontiguousarray(frame, dtype=np.uint8)
    return _lib.check_sync(
        _ptr(frame_c),
        plane_h, plane_w,
        block_size, blocks_x, sync_rows, margin,
    )


# ─── packed-bit fast path (v5) ────────────────────────────────────────────────

def encode_plane_packed_c(src: np.ndarray,
                          bit_offset: int, bit_stride: int,
                          n_frames: int,
                          plane_h: int, plane_w: int,
                          block_size: int, blocks_x: int,
                          blocks_y_data: int, sync_rows: int,
                          bpp: int) -> np.ndarray:
    """Encode straight from packed payload bytes.

    `src` must be a contiguous uint8 array with at least PACK_PAD bytes of
    readable slack past the last bit this call will touch.
    Returns (n_frames, plane_h, plane_w) uint8.
    """
    out = np.empty(n_frames * plane_h * plane_w, dtype=np.uint8)
    _lib.encode_plane_packed(
        _ptr(src), ctypes.c_uint64(bit_offset), ctypes.c_uint64(bit_stride),
        _ptr(out), n_frames, plane_h, plane_w,
        block_size, blocks_x, blocks_y_data, sync_rows, bpp, NATIVE_THREADS,
    )
    return out.reshape(n_frames, plane_h, plane_w)


def decode_plane_packed_c(frames: np.ndarray, out: np.ndarray,
                          bit_offset: int, bit_stride: int,
                          plane_h: int, plane_w: int,
                          block_size: int, blocks_x: int,
                          blocks_y_data: int, sync_rows: int,
                          bpp: int, margin: int) -> None:
    """Decode packed bits directly into `out`, which must start zeroed.

    Bits are OR-ed in, so `out` accumulates across the per-plane calls that
    make up one frame.
    """
    frames_c = np.ascontiguousarray(frames, dtype=np.uint8)
    _lib.decode_plane_packed(
        _ptr(frames_c), _ptr(out),
        ctypes.c_uint64(bit_offset), ctypes.c_uint64(bit_stride),
        len(frames_c), plane_h, plane_w,
        block_size, blocks_x, blocks_y_data, sync_rows, bpp, margin,
        NATIVE_THREADS,
    )


# ─── Reed-Solomon decode ──────────────────────────────────────────────────────

def rs_decode_batch_c(chunks: np.ndarray, nroots: int):
    """Correct a (n_chunks, n) uint8 array of codewords in place.

    Returns (corrected_array, status) where status[i] is the number of symbols
    repaired in chunk i, or -1 if that chunk was beyond the code's capability.
    The input is not modified; a corrected copy is returned.
    """
    arr = np.ascontiguousarray(chunks, dtype=np.uint8).copy()
    n_chunks, n = arr.shape
    status = np.empty(n_chunks, dtype=np.int32)
    _lib.rs_decode_batch(
        _ptr(arr), status.ctypes.data_as(ctypes.POINTER(ctypes.c_int)),
        n_chunks, n, nroots, NATIVE_THREADS,
    )
    return arr, status


def rs_encode_batch_c(msg: np.ndarray, nroots: int) -> np.ndarray:
    """Systematic encode of a (n_chunks, k) uint8 array.

    Returns (n_chunks, k + nroots) codewords: message first, then parity.
    """
    src = np.ascontiguousarray(msg, dtype=np.uint8)
    n_chunks, k = src.shape
    out = np.empty((n_chunks, k + nroots), dtype=np.uint8)
    _lib.rs_encode_batch(_ptr(src), _ptr(out), n_chunks, k + nroots,
                         nroots, NATIVE_THREADS)
    return out


def check_sync_batch_c(frames: np.ndarray,
                       plane_h: int, plane_w: int,
                       block_size: int, blocks_x: int,
                       sync_rows: int, margin: int) -> np.ndarray:
    """Batch check_sync: returns (n_frames,) int32 scores (0-100)."""
    n_frames = len(frames)
    scores   = np.empty(n_frames, dtype=np.int32)
    frames_c = np.ascontiguousarray(frames, dtype=np.uint8)
    _lib.check_sync_batch(
        _ptr(frames_c),
        scores.ctypes.data_as(ctypes.POINTER(ctypes.c_int)),
        n_frames, plane_h, plane_w,
        block_size, blocks_x, sync_rows, margin,
    )
    return scores

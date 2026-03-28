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

        _lib = lib
    except Exception as e:
        print(f'[native] Warning: could not load {_LIB_NAME}: {e}')


_load()
NATIVE_AVAILABLE = _lib is not None


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

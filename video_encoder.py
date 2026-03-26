"""
Video encoder: files → H.264 video

Key optimisations vs the original:
  • BLOCK_SIZE=4 → 16.5× more data per frame (128 640 bits vs 7 800)
  • Batch frame generation: 32 frames at a time via two np.repeat calls
  • Background write thread: numpy generation and ffmpeg pipe-write run in parallel
  • Grayscale pipe: 1 byte/pixel instead of 3 (3× less I/O)
  • Hardware-accelerated encoder (QSV → NVENC → AMF → libx264)
"""

import io
import math
import os
import queue
import subprocess
import sys
import tarfile
import threading
import zlib
from pathlib import Path

import numpy as np
import imageio_ffmpeg

from video_codec import (
    BLOCK_SIZE, FRAME_WIDTH, FRAME_HEIGHT,
    BITS_PER_FRAME, NROOTS, CHUNK_IN, MAGIC, FPS,
    HEADER_SIZE, HEADER_REPEAT,
    pack_header, bytes_to_bits,
    bits_to_frame, bits_to_frames_batch,
)

# ---------------------------------------------------------------------------
# Reed-Solomon encode  (galois – numba-JIT vectorised)
# ---------------------------------------------------------------------------
import galois as _galois

_GF = _galois.GF(2 ** 8)
_RS = _galois.ReedSolomon(255, CHUNK_IN)


def _rs_warmup():
    dummy = _GF(np.zeros((1, CHUNK_IN), dtype=int))
    _RS.encode(dummy)


threading.Thread(target=_rs_warmup, daemon=True).start()


def rs_encode(data: bytes) -> bytes:
    pad    = (-len(data)) % CHUNK_IN
    padded = data + b'\x00' * pad
    chunks = np.frombuffer(padded, dtype=np.uint8).reshape(-1, CHUNK_IN).astype(int)
    enc    = _RS.encode(_GF(chunks))
    return np.asarray(enc, dtype=np.uint8).tobytes()

# ---------------------------------------------------------------------------
# Hardware encoder detection
# ---------------------------------------------------------------------------

def _probe_hw_encoder() -> tuple:
    exe = imageio_ffmpeg.get_ffmpeg_exe()
    kw  = dict(stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.PIPE)
    if sys.platform == 'win32':
        kw['creationflags'] = subprocess.CREATE_NO_WINDOW
    # 8 tiny test frames (gray)
    payload = b'\x80' * (64 * 64 * 8)
    candidates = [
        ('h264_qsv',   ['-preset', 'veryfast', '-global_quality', '18', '-g', '1']),
        ('h264_nvenc', ['-rc', 'constqp', '-qp', '18', '-g', '1', '-bf', '0']),
        ('h264_amf',   ['-quality', 'speed', '-rc', 'cqp', '-qp_i', '18', '-g', '1']),
    ]
    for codec, params in candidates:
        cmd = [exe, '-f', 'rawvideo', '-pix_fmt', 'gray', '-s', '64x64', '-r', '24',
               '-i', 'pipe:0', '-c:v', codec, *params, '-f', 'null', '-']
        try:
            p = subprocess.Popen(cmd, **kw)
            p.stdin.write(payload)
            p.stdin.close()
            p.wait(timeout=10)
            if p.returncode == 0:
                return codec, params
        except Exception:
            pass
    # Software fallback
    return ('libx264',
            ['-crf', '20', '-preset', 'ultrafast', '-g', '1',
             '-movflags', '+faststart', '-threads', '0'])


_HW_CODEC: str | None = None
_HW_PARAMS: list | None = None


def _get_encoder():
    global _HW_CODEC, _HW_PARAMS
    if _HW_CODEC is None:
        _HW_CODEC, _HW_PARAMS = _probe_hw_encoder()
    return _HW_CODEC, _HW_PARAMS

# ---------------------------------------------------------------------------
# Archive
# ---------------------------------------------------------------------------

def create_archive(paths: list) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode='w:gz', compresslevel=1) as tar:
        for p in paths:
            p = Path(p)
            tar.add(str(p), arcname=p.name, recursive=True)
    return buf.getvalue()

# ---------------------------------------------------------------------------
# Background pipe-write thread
# ---------------------------------------------------------------------------

def _pipe_writer(pipe, q: queue.Queue):
    """Drain the queue and write each item to the ffmpeg stdin pipe."""
    while True:
        item = q.get()
        if item is None:
            pipe.close()
            return
        pipe.write(item)

# ---------------------------------------------------------------------------
# Main encode entry point
# ---------------------------------------------------------------------------

ENCODE_BATCH = 32   # frames generated + piped per iteration


def encode_files_to_video(file_paths: list, output_path: str, progress=None):
    def log(msg):
        if progress:
            progress(msg)

    log('Creating archive...')
    archive      = create_archive(file_paths)
    archive_size = len(archive)
    archive_crc  = zlib.crc32(archive) & 0xFFFFFFFF
    log(f'Archive: {archive_size:,} bytes. Applying error correction...')

    encoded      = rs_encode(archive)
    encoded_size = len(encoded)
    log(f'ECC encoded: {encoded_size:,} bytes.')

    data_bits      = bytes_to_bits(encoded)
    num_data_frames = math.ceil(len(data_bits) / BITS_PER_FRAME)
    pad = num_data_frames * BITS_PER_FRAME - len(data_bits)
    if pad:
        data_bits = np.concatenate([data_bits, np.zeros(pad, dtype=np.uint8)])

    header_raw  = pack_header(archive_size, num_data_frames, encoded_size, archive_crc)
    header_bits = bytes_to_bits(header_raw * HEADER_REPEAT)
    hpad = BITS_PER_FRAME - (len(header_bits) % BITS_PER_FRAME)
    if hpad != BITS_PER_FRAME:
        header_bits = np.concatenate([header_bits, np.zeros(hpad, dtype=np.uint8)])

    total_frames = 1 + num_data_frames
    codec, hw_params = _get_encoder()
    log(f'Generating {total_frames} frames using {codec}...')

    exe = imageio_ffmpeg.get_ffmpeg_exe()
    cmd = [
        exe,
        '-f', 'rawvideo', '-pix_fmt', 'gray',
        '-s', f'{FRAME_WIDTH}x{FRAME_HEIGHT}',
        '-r', str(FPS),
        '-i', 'pipe:0',
        '-c:v', codec, *hw_params,
        '-pix_fmt', 'yuv420p',
        '-y', output_path,
    ]
    kw = dict(stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if sys.platform == 'win32':
        kw['creationflags'] = subprocess.CREATE_NO_WINDOW

    proc = subprocess.Popen(cmd, **kw)

    # Background thread handles all pipe writes so numpy gen and I/O overlap
    write_q: queue.Queue = queue.Queue(maxsize=4)
    writer_t = threading.Thread(target=_pipe_writer, args=(proc.stdin, write_q), daemon=True)
    writer_t.start()

    try:
        # Header frame (single)
        write_q.put(bits_to_frame(header_bits[:BITS_PER_FRAME]).tobytes())

        # Data frames in batches
        for batch_start in range(0, num_data_frames, ENCODE_BATCH):
            n   = min(ENCODE_BATCH, num_data_frames - batch_start)
            s   = batch_start * BITS_PER_FRAME
            seg = data_bits[s:s + n * BITS_PER_FRAME]
            if len(seg) < n * BITS_PER_FRAME:
                seg = np.concatenate([seg,
                      np.zeros(n * BITS_PER_FRAME - len(seg), dtype=np.uint8)])
            frames = bits_to_frames_batch(seg)     # (n, H, W) uint8, vectorised
            write_q.put(frames.tobytes())           # one big write per batch

            done = batch_start + n
            if done % (ENCODE_BATCH * 10) == 0:
                log(f'  frame {done}/{num_data_frames}...')

    finally:
        write_q.put(None)   # signal writer thread to close pipe
        writer_t.join()
        proc.wait()

    log('Video encoding complete!')
    return output_path

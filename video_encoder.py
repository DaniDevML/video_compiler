"""
Video encoder (v4): files → H.264/AAC video

v4: 3-bpp Y plane (8 gray levels) + 2-bpp Cb/Cr planes (4 gray levels)
    → 64,200 bytes/frame (1.6× over v3's 40,140)
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
    BLOCK_SIZE, FRAME_WIDTH, FRAME_HEIGHT, YUV_FRAME_BYTES,
    BITS_PER_FRAME, BYTES_PER_FRAME,
    NROOTS, CHUNK_IN, FPS,
    HEADER_SIZE, HEADER_REPEAT,
    pack_header, bytes_to_bits,
    bits_to_yuv_frames, header_to_yuv_frame,
    NATIVE_AVAILABLE,
)
import audio_codec as _ac

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
    payload    = b'\x80' * (64 * 64 * 8)
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
    while True:
        item = q.get()
        if item is None:
            pipe.close()
            return
        pipe.write(item)

# ---------------------------------------------------------------------------
# Main encode entry point
# ---------------------------------------------------------------------------

ENCODE_BATCH = 32


def encode_files_to_video(file_paths: list, output_path: str, progress=None):
    def log(msg):
        if progress:
            progress(msg)

    if NATIVE_AVAILABLE:
        log('Pixel engine: C native library (maximum speed)')
    else:
        log('Pixel engine: NumPy fallback  —  run  python native/build.py  for faster encoding')

    log('Creating archive...')
    archive      = create_archive(file_paths)
    archive_size = len(archive)
    archive_crc  = zlib.crc32(archive) & 0xFFFFFFFF
    log(f'Archive: {archive_size:,} bytes. Applying error correction...')

    encoded      = rs_encode(archive)
    encoded_size = len(encoded)
    log(f'ECC encoded: {encoded_size:,} bytes  ({BYTES_PER_FRAME:,} bytes/frame).')

    data_bits       = bytes_to_bits(encoded)
    num_data_frames = math.ceil(len(data_bits) / BITS_PER_FRAME)
    pad             = num_data_frames * BITS_PER_FRAME - len(data_bits)
    if pad:
        data_bits = np.concatenate([data_bits, np.zeros(pad, dtype=np.uint8)])

    total_frames        = 1 + num_data_frames
    total_audio_samples = int(total_frames / FPS * _ac.SAMPLE_RATE)
    audio_capacity      = _ac.max_payload_bytes(total_audio_samples)
    log(f'Audio channel capacity: {audio_capacity:,} bytes '
        f'(header copy embedded for decode robustness)')

    # Header frame
    header_raw = pack_header(archive_size, num_data_frames, encoded_size, archive_crc)
    header_yuv = header_to_yuv_frame(header_raw)   # (YUV_FRAME_BYTES,) uint8

    codec, hw_params = _get_encoder()
    log(f'Generating {total_frames} YUV frames using {codec}...')

    # ── Pre-generate audio PCM while building the ffmpeg command ──
    sr = _ac.SAMPLE_RATE
    total_samples = int(total_frames / FPS * sr)
    has_audio = _ac.max_payload_bytes(total_samples) >= len(header_raw)

    exe = imageio_ffmpeg.get_ffmpeg_exe()

    if has_audio:
        log('Encoding video + audio in a single pass...')
        audio_pcm = _ac.float32_to_s16(_ac.encode_audio(header_raw, total_samples))
        # Write audio to a temp file (ffmpeg can't read two pipes simultaneously)
        import tempfile
        audio_tmp = tempfile.mktemp(suffix='.raw')
        with open(audio_tmp, 'wb') as af:
            af.write(audio_pcm)

        cmd = [
            exe,
            '-f', 'rawvideo', '-pix_fmt', 'yuv420p',
            '-s', f'{FRAME_WIDTH}x{FRAME_HEIGHT}',
            '-r', str(FPS),
            '-i', 'pipe:0',
            '-f', 's16le', '-ar', str(sr), '-ac', '1', '-i', audio_tmp,
            '-c:v', codec, *hw_params,
            '-c:a', 'aac', '-b:a', '128k',
            '-pix_fmt', 'yuv420p',
            '-shortest',
            '-y', output_path,
        ]
    else:
        audio_tmp = None
        cmd = [
            exe,
            '-f', 'rawvideo', '-pix_fmt', 'yuv420p',
            '-s', f'{FRAME_WIDTH}x{FRAME_HEIGHT}',
            '-r', str(FPS),
            '-i', 'pipe:0',
            '-c:v', codec, *hw_params,
            '-pix_fmt', 'yuv420p',
            '-an',
            '-y', output_path,
        ]

    kw = dict(stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if sys.platform == 'win32':
        kw['creationflags'] = subprocess.CREATE_NO_WINDOW

    proc = subprocess.Popen(cmd, **kw)

    write_q: queue.Queue = queue.Queue(maxsize=4)
    writer_t = threading.Thread(target=_pipe_writer, args=(proc.stdin, write_q), daemon=True)
    writer_t.start()

    try:
        # Header frame
        write_q.put(header_yuv.tobytes())

        # Data frames in batches
        for batch_start in range(0, num_data_frames, ENCODE_BATCH):
            n   = min(ENCODE_BATCH, num_data_frames - batch_start)
            s   = batch_start * BITS_PER_FRAME
            seg = data_bits[s:s + n * BITS_PER_FRAME]
            if len(seg) < n * BITS_PER_FRAME:
                seg = np.concatenate([seg,
                      np.zeros(n * BITS_PER_FRAME - len(seg), dtype=np.uint8)])
            yuv_batch = bits_to_yuv_frames(seg)    # (n, YUV_FRAME_BYTES) uint8
            write_q.put(yuv_batch.tobytes())

            done = batch_start + n
            if done % (ENCODE_BATCH * 10) == 0:
                log(f'  frame {done}/{num_data_frames}...')

    finally:
        write_q.put(None)
        writer_t.join()
        proc.wait()

    if audio_tmp and os.path.exists(audio_tmp):
        os.unlink(audio_tmp)

    log('Video encoding complete!')
    return output_path



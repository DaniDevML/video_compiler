"""
Video encoder (v5): files → H.264/AAC video

The frame format lives in video_codec.Profile; see DEFAULT_PROFILE there for
the geometry and the measurements behind it.
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

from concurrent.futures import ThreadPoolExecutor

import numpy as np
import imageio_ffmpeg

import paths

from video_codec import (
    BLOCK_SIZE, FRAME_WIDTH, FRAME_HEIGHT, YUV_FRAME_BYTES,
    BITS_PER_FRAME, BYTES_PER_FRAME,
    NROOTS, CHUNK_IN, FPS,
    HEADER_SIZE, HEADER_REPEAT,
    pack_header, bytes_to_bits, encode_sidecar,
    bits_to_yuv_frames, header_to_yuv_frame,
    pad_for_packed, packed_to_yuv_frames,
    DEFAULT_PROFILE, PROFILE_HEADER,
    NATIVE_AVAILABLE, PACKED_AVAILABLE, NATIVE_THREADS,
)
import audio_codec as _ac

try:
    from native import RS_AVAILABLE, rs_encode_batch_c
except ImportError:
    RS_AVAILABLE = False

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


_RS_WORKERS = min(12, (os.cpu_count() or 4))
# Below this many chunks the thread hand-off costs more than it saves.
_RS_THREAD_MIN_CHUNKS = 64


def rs_encode(data: bytes) -> bytes:
    """Reed-Solomon encode.

    Prefers the native encoder. The galois path below is the fallback for
    builds without the C library: it hands the payload over as uint8 rather
    than widening it to int64 first, and splits the chunk matrix across
    threads (galois runs numba ufuncs that release the GIL). All three paths
    are verified to produce byte-identical parity.
    """
    pad    = (-len(data)) % CHUNK_IN
    padded = data + b'\x00' * pad
    chunks = np.frombuffer(padded, dtype=np.uint8).reshape(-1, CHUNK_IN)

    if RS_AVAILABLE:
        # Native encoder: verified byte-identical to galois in
        # bench/test_rs_native.py, and about 3x faster.
        return rs_encode_batch_c(chunks, NROOTS).tobytes()

    if len(chunks) < _RS_THREAD_MIN_CHUNKS or _RS_WORKERS < 2:
        return np.asarray(_RS.encode(chunks.view(_GF)), dtype=np.uint8).tobytes()

    bounds = np.linspace(0, len(chunks), _RS_WORKERS + 1).astype(int)
    parts  = [chunks[a:b] for a, b in zip(bounds[:-1], bounds[1:]) if b > a]
    with ThreadPoolExecutor(max_workers=len(parts)) as ex:
        outs = list(ex.map(
            lambda c: np.asarray(_RS.encode(c.view(_GF)), dtype=np.uint8),
            parts))
    return np.concatenate(outs).tobytes()

# ---------------------------------------------------------------------------
# Hardware encoder detection
# ---------------------------------------------------------------------------

# Quantiser used for the upload. The blocks are flat by construction, so a
# coarse quantiser reproduces them exactly through our own decoder, and raising
# it from the v4 default of qp18 to qp44 cuts the uploaded bytes substantially
# at no cost to recovery.
#
# It does NOT keep paying: measured against real YouTube, qp51 -- which is
# still clean through our own decoder at 2.75x expansion -- loses the data,
# with 22 of 39,031 blocks beyond repair (video PWEOo6HEmDY). Uploading a
# coarser file degrades the source YouTube re-encodes from, and past roughly
# qp47 that starts to matter. qp44 is verified end to end through the real
# service at 8, 64, 256 and 1024 MB.
# See bench/sweep_upload_qp_v5.py and bench/bench_youtube.py.
UPLOAD_QP = 44


def _probe_hw_encoder() -> tuple:
    exe = paths.ffmpeg_exe()
    kw  = dict(stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.PIPE)
    if sys.platform == 'win32':
        kw['creationflags'] = subprocess.CREATE_NO_WINDOW
    payload = b'\x80' * (64 * 64 * 8)
    q = str(UPLOAD_QP)
    # The low-latency NVENC presets encode this content markedly faster than
    # the default at identical output size; the newer p1..p7 preset names are
    # not implemented on older (Pascal-era) drivers, so 'llhp' is used.
    candidates = [
        ('h264_nvenc', ['-preset', 'llhp', '-rc', 'constqp', '-qp', q,
                        '-g', '1', '-bf', '0']),
        ('h264_nvenc', ['-rc', 'constqp', '-qp', q, '-g', '1', '-bf', '0']),
        ('h264_qsv',   ['-preset', 'veryfast', '-global_quality', q, '-g', '1']),
        ('h264_amf',   ['-quality', 'speed', '-rc', 'cqp', '-qp_i', q, '-g', '1']),
    ]
    for codec, params in candidates:
        cmd = [exe, '-f', 'rawvideo', '-pix_fmt', 'gray', '-s', '64x64', '-r', '24',
               '-i', 'pipe:0', '-c:v', codec, *params, '-f', 'null', '-']
        try:
            p = subprocess.Popen(cmd, **kw)
            try:
                p.stdin.write(payload)
                p.stdin.close()
            except (BrokenPipeError, OSError):
                p.wait(timeout=10)
                continue
            p.wait(timeout=10)
            if p.returncode == 0:
                return codec, params
        except Exception:
            pass
    return ('libx264',
            ['-qp', str(UPLOAD_QP), '-preset', 'ultrafast', '-g', '1',
             '-aq-mode', '0', '-movflags', '+faststart', '-threads', '0'])


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

def _looks_compressible(file_paths: list, sample_bytes: int = 2 << 20) -> bool:
    """Cheap guess at whether gzip will earn its keep.

    Compressing incompressible input is pure loss: measured on 256 MB of random
    bytes, gzip level 1 cost 8.4 s and produced a file no smaller -- about
    34 seconds thrown away per gigabyte. Sampling a couple of megabytes answers
    the question for a fraction of a second.
    """
    import zlib

    sample = bytearray()
    for p in file_paths:
        for root, _dirs, files in os.walk(p) if os.path.isdir(p) else [(None, None, None)]:
            candidates = ([os.path.join(root, f) for f in files]
                          if root is not None else [p])
            for c in candidates:
                try:
                    with open(c, 'rb') as f:
                        sample += f.read(sample_bytes - len(sample))
                except OSError:
                    continue
                if len(sample) >= sample_bytes:
                    break
            if len(sample) >= sample_bytes:
                break
        if len(sample) >= sample_bytes:
            break

    if len(sample) < 65536:
        return True          # too little to judge; compressing is cheap anyway
    ratio = len(zlib.compress(bytes(sample), 1)) / len(sample)
    return ratio < 0.95


def create_archive(file_paths: list, progress=None) -> bytes:
    """Pack the inputs into a tar, compressed only when that helps."""
    compress = _looks_compressible(file_paths)
    if progress:
        progress('Creating archive'
                 + (' (compressing)...' if compress else
                    ' (already incompressible, skipping gzip)...'))

    buf = io.BytesIO()
    mode = 'w:gz' if compress else 'w'
    kw = {'compresslevel': 1} if compress else {}
    with tarfile.open(fileobj=buf, mode=mode, **kw) as tar:
        for p in file_paths:
            p = Path(p)
            tar.add(str(p), arcname=p.name, recursive=True)
    return buf.getvalue()

# ---------------------------------------------------------------------------
# Background pipe-write thread
# ---------------------------------------------------------------------------

def _pipe_writer(pipe, q: queue.Queue):
    """Drain the queue into ffmpeg's stdin.

    Items are NumPy arrays rather than bytes: pipe.write takes any object
    supporting the buffer protocol, so queueing the arrays directly avoids
    copying every batch a second time on its way out.
    """
    while True:
        item = q.get()
        if item is None:
            pipe.close()
            return
        pipe.write(memoryview(item).cast('B'))

# ---------------------------------------------------------------------------
# Main encode entry point
# ---------------------------------------------------------------------------

# Frames generated per batch before handing off to the writer thread. Keeping
# the batch small bounds how long ffmpeg waits on a block being generated and
# how much sits queued (8 frames is ~25 MB). Measured best of 2/4/8/16/32/64.
ENCODE_BATCH = 8


def encode_files_to_video(file_paths: list, output_path: str, progress=None):
    """Archive `file_paths` and write the whole thing to one video."""
    def log(msg):
        if progress:
            progress(msg)
    log('Creating archive...')
    archive = create_archive(file_paths)
    log(f'Archive: {len(archive):,} bytes.')
    return encode_bytes_to_video(archive, output_path, progress=progress)


def encode_bytes_to_video(archive: bytes, output_path: str, progress=None,
                          extra_sidecar: str = ''):
    """Write an arbitrary byte string to one video.

    Split out from encode_files_to_video so a shard -- a slice of a larger
    archive, not a valid archive on its own -- can be encoded by the same path.
    `extra_sidecar` is appended to the .sidecar file, which is how the shard
    manifest travels alongside the header.
    """
    def log(msg):
        if progress:
            progress(msg)

    if NATIVE_AVAILABLE:
        mode = 'packed' if PACKED_AVAILABLE else 'byte-per-bit'
        log(f'Pixel engine: C native library ({mode}, {NATIVE_THREADS} thread'
            f'{"s" if NATIVE_THREADS != 1 else ""})')
    else:
        log('Pixel engine: NumPy fallback  —  run  python native/build.py  for faster encoding')

    archive_size = len(archive)
    archive_crc  = zlib.crc32(archive) & 0xFFFFFFFF
    log('Applying error correction...')

    encoded      = rs_encode(archive)
    encoded_size = len(encoded)
    log(f'ECC encoded: {encoded_size:,} bytes  ({BYTES_PER_FRAME:,} bytes/frame).')

    num_data_frames = math.ceil(len(encoded) / BYTES_PER_FRAME)
    # One zero-padded buffer covering every frame, with the slack the packed
    # bit reader needs past the end. Frames are cut from this without copying.
    frame_src = pad_for_packed(
        encoded.ljust(num_data_frames * BYTES_PER_FRAME, b'\x00'))

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

    exe = paths.ffmpeg_exe()

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
        write_q.put(header_yuv)

        # Data frames in batches. The arrays are queued directly rather than
        # via .tobytes(); pipe.write accepts the buffer protocol, so this
        # avoids copying every batch (~100 MB each) a second time.
        for batch_start in range(0, num_data_frames, ENCODE_BATCH):
            n = min(ENCODE_BATCH, num_data_frames - batch_start)
            write_q.put(packed_to_yuv_frames(frame_src, n, batch_start))

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
    # Written alongside the video so the uploader can copy it into the
    # description, giving the decoder a lossless copy of the header.
    with open(output_path + '.sidecar', 'w', encoding='utf-8') as f:
        f.write(encode_sidecar(header_raw))
        if extra_sidecar:
            f.write(chr(10) + extra_sidecar)
    return output_path



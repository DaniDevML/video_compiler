"""
Video decoder (v3): H.264/AAC video → original files

Handles both v3 (YUV 4:2:0, 2-bpp Y + 1-bpp Cb/Cr) and v2 (grayscale 1-bpp)
by auto-detecting the magic bytes in the header frame.

v3 decode path:
  1. ffmpeg outputs raw yuv420p frames (3 planes per frame)
  2. Y plane decoded with 2-bpp quantisation (4 gray levels)
  3. Cb/Cr planes decoded with 1-bpp quantisation
  4. Bits concatenated → RS decode → un-tar → output files

v2 decode path (backward compat):
  Exactly as before — grayscale frames, 1-bpp, VIDCMPR2 magic.
"""

import io
import os
import subprocess
import sys
import tarfile
import zlib

import numpy as np
import imageio_ffmpeg

from video_codec import (
    BLOCK_SIZE, FRAME_WIDTH, FRAME_HEIGHT,
    BITS_PER_FRAME, BYTES_PER_FRAME,
    YUV_FRAME_BYTES, Y_PLANE_BYTES, CB_PLANE_BYTES,
    NROOTS, CHUNK_IN, FPS,
    HEADER_SIZE, HEADER_REPEAT,
    MAGIC, MAGIC_V2,
    unpack_header, bits_to_bytes,
    yuv_frames_to_bits, check_sync_yuv, yuv_frame_to_header,
    # v2 compat
    check_sync_batch, frames_to_bits_batch, decode_header_frame,
    NATIVE_AVAILABLE,
)

_CANDIDATE_BLOCK_SIZES = [4, 16]

# ---------------------------------------------------------------------------
# Reed-Solomon decode  (galois – numba-JIT vectorised)
# ---------------------------------------------------------------------------
import galois as _galois
import threading as _t

_GF = _galois.GF(2 ** 8)
_RS = _galois.ReedSolomon(255, CHUNK_IN)


def _rs_warmup():
    _RS.decode(_GF(np.zeros((1, 255), dtype=int)))


_t.Thread(target=_rs_warmup, daemon=True).start()


def _rs_strip_parity(data: bytes, nroots: int = NROOTS) -> bytes:
    """Fast path: galois RS is systematic, data bytes come first — just slice."""
    chunk_size = 255
    chunk_in   = chunk_size - nroots
    n          = len(data) // chunk_size
    if n == 0:
        return b''
    arr = np.frombuffer(data[:n * chunk_size], dtype=np.uint8).reshape(n, chunk_size)
    return arr[:, :chunk_in].tobytes()


def rs_decode(data: bytes, original_size: int, nroots: int = NROOTS) -> bytes:
    chunk_size = 255
    chunk_in   = chunk_size - nroots
    n_full     = len(data) // chunk_size
    remainder  = len(data) % chunk_size
    parts: list[bytes] = []

    if n_full > 0:
        arr = np.frombuffer(data[:n_full * chunk_size], dtype=np.uint8) \
                .reshape(-1, chunk_size).astype(int)
        try:
            rs = _galois.ReedSolomon(255, chunk_in) if nroots != NROOTS else _RS
            dec = rs.decode(_GF(arr))
            parts.append(np.asarray(dec, dtype=np.uint8).tobytes())
        except Exception:
            raise RuntimeError(
                "Couldn't repair the data errors in this video. "
                'YouTube may have re-compressed it too aggressively, '
                'or this might be the wrong video URL.'
            )

    if remainder > 0:
        last = data[n_full * chunk_size:]
        try:
            from reedsolo import RSCodec
            dec, _, _ = RSCodec(nroots).decode(last)
            parts.append(bytes(dec))
        except Exception:
            raise RuntimeError(
                "Couldn't repair errors in the final data chunk. "
                'The video may have been cut short or re-encoded by YouTube.'
            )

    return b''.join(parts)[:original_size]

# ---------------------------------------------------------------------------
# ffmpeg helpers
# ---------------------------------------------------------------------------

def _popen_kw(**extra):
    kw = dict(stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, **extra)
    if sys.platform == 'win32':
        kw['creationflags'] = subprocess.CREATE_NO_WINDOW
    return kw


def _build_decode_cmd(video_path: str, pix_fmt: str, output: str = 'pipe:1') -> list:
    """Build ffmpeg decode command for either yuv420p (v3) or gray (v2)."""
    exe   = imageio_ffmpeg.get_ffmpeg_exe()
    w, h  = FRAME_WIDTH, FRAME_HEIGHT
    scale = f'scale={w}:{h}:flags=neighbor'
    tail  = ['-vf', scale, '-f', 'rawvideo', '-pix_fmt', pix_fmt]
    tail += ['pipe:1'] if output == 'pipe:1' else ['-y', output]

    if sys.platform == 'win32':
        probe_kw = dict(stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                        creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            r = subprocess.run(
                [exe, '-hwaccel', 'cuda', '-i', video_path,
                 '-frames:v', '1', '-f', 'null', '-'],
                timeout=8, **probe_kw)
            if r.returncode == 0:
                return [exe, '-hwaccel', 'cuda', '-i', video_path] + tail
        except Exception:
            pass

    return [exe, '-threads', '0', '-i', video_path] + tail


_FILE_IO_LIMIT_BYTES = 5 * 1024 ** 3   # 5 GB


def probe_video(video_path: str) -> tuple:
    import re
    exe = imageio_ffmpeg.get_ffmpeg_exe()
    kw  = dict(stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if sys.platform == 'win32':
        kw['creationflags'] = subprocess.CREATE_NO_WINDOW
    _, stderr = subprocess.Popen([exe, '-i', video_path], **kw).communicate()
    m = re.search(r'Video:.*?(\d{3,5})x(\d{3,5})', stderr.decode(errors='replace'))
    return (int(m.group(1)), int(m.group(2))) if m else (None, None)

# ---------------------------------------------------------------------------
# Raw-frame iterators
# ---------------------------------------------------------------------------

DECODE_BATCH = 32


def _iter_batches_pipe(proc, frame_bytes: int, batch_size: int):
    buf        = bytearray()
    frame_idx  = 0
    batch_bytes = frame_bytes * batch_size

    while True:
        need  = batch_bytes - len(buf)
        chunk = proc.stdout.read(need)
        if chunk:
            buf += chunk
        if len(buf) < frame_bytes:
            break
        n      = len(buf) // frame_bytes
        usable = n * frame_bytes
        arr    = np.frombuffer(bytes(buf[:usable]), dtype=np.uint8).copy()
        yield frame_idx, arr.reshape(n, frame_bytes)
        buf        = bytearray(buf[usable:])
        frame_idx += n
        if not chunk:
            break

    proc.wait()


def _read_all_frames_file(video_path: str, pix_fmt: str, frame_bytes: int):
    """
    Faster than piping for most videos: ffmpeg writes to a temp .raw file,
    Python reads it in one numpy call.
    """
    import tempfile
    tmp = tempfile.mktemp(suffix='.raw')
    kw  = dict(stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if sys.platform == 'win32':
        kw['creationflags'] = subprocess.CREATE_NO_WINDOW
    subprocess.run(_build_decode_cmd(video_path, pix_fmt, output=tmp), **kw)

    if not os.path.exists(tmp):
        return None

    raw      = np.fromfile(tmp, dtype=np.uint8)
    os.unlink(tmp)
    n_frames = len(raw) // frame_bytes
    if n_frames == 0:
        return None
    return raw[:n_frames * frame_bytes].reshape(n_frames, frame_bytes)

# ---------------------------------------------------------------------------
# Main decode entry point
# ---------------------------------------------------------------------------

def decode_video_to_files(video_path: str, output_dir: str, progress=None) -> list:
    def log(msg):
        if progress:
            progress(msg)

    if NATIVE_AVAILABLE:
        log('Pixel engine: C native library')
    else:
        log('Pixel engine: NumPy fallback')

    w, h = probe_video(video_path)
    log(f'Downloaded video resolution: {w}x{h}')
    if w and h and (w < FRAME_WIDTH or h < FRAME_HEIGHT):
        raise RuntimeError(
            f'The video is only {w}x{h} — full 1080p is required for decoding. '
            'YouTube usually takes 2–5 minutes to finish processing the high-resolution '
            'version after upload. Please wait a moment and try again.'
        )

    # ── Probe: detect v2 (gray) vs v3 (yuv420p) by scanning the first few frames ──
    log('Detecting video format...')
    version, header, block_size = _detect_version(video_path, log)
    log(f'Format: {"v3 YUV 2-bpp" if version == 3 else "v2 grayscale 1-bpp"}  '
        f'(block_size={block_size})')

    if version == 3:
        return _decode_v3(video_path, header, block_size, output_dir, log)
    else:
        return _decode_v2(video_path, header, block_size, output_dir, log)


# ---------------------------------------------------------------------------
# Version detection
# ---------------------------------------------------------------------------

def _detect_version(video_path: str, log):
    """
    Read just enough frames to find and parse the header.
    Returns (version: int, header: dict, block_size: int).
    """
    # Try yuv420p first (v3), fall back to gray (v2)
    for pix_fmt, frame_bytes, v_candidate in [
        ('yuv420p', YUV_FRAME_BYTES, 3),
        ('gray',    FRAME_WIDTH * FRAME_HEIGHT, 2),
    ]:
        raw_size_est = os.path.getsize(video_path) * 20
        if raw_size_est < _FILE_IO_LIMIT_BYTES:
            all_frames = _read_all_frames_file(video_path, pix_fmt, frame_bytes)
            if all_frames is None:
                continue
            source = [(i, all_frames[i:i+DECODE_BATCH])
                      for i in range(0, len(all_frames), DECODE_BATCH)]
        else:
            proc   = subprocess.Popen(_build_decode_cmd(video_path, pix_fmt),
                                      **_popen_kw())
            source = _iter_batches_pipe(proc, frame_bytes, DECODE_BATCH)

        for batch_start, batch in source:
            if batch_start > 60:
                break   # header should be in first 60 frames

            if v_candidate == 3:
                header, bs = _try_find_header_v3(batch)
            else:
                # Gray frames are (N, W*H) — reshape to (N, H, W) for v2 API
                batch_3d = batch.reshape(-1, FRAME_HEIGHT, FRAME_WIDTH)
                header, bs = _try_find_header_v2(batch_3d)

            if header is not None:
                return v_candidate, header, bs

    raise RuntimeError(
        "This doesn't appear to be a VidCompiler video — no data marker was found. "
        'Double-check the URL. If you just uploaded it, '
        'YouTube may still be processing; try again in a few minutes.'
    )


def _try_find_header_v3(batch: np.ndarray):
    """Try to find and decode a v3 header in a batch of YUV frames."""
    for bs in _CANDIDATE_BLOCK_SIZES:
        sync_mask = check_sync_yuv(batch, block_size=bs)
        for i in np.where(sync_mask)[0]:
            try:
                frame_row = batch[i:i+1]   # (1, YUV_FRAME_BYTES)
                h = yuv_frame_to_header(frame_row[0], block_size=bs)
                if h['magic'] == MAGIC:
                    return h, bs
            except Exception:
                continue
    return None, None


def _try_find_header_v2(batch: np.ndarray):
    """Try to find and decode a v2 header in a batch of grayscale frames."""
    for bs in _CANDIDATE_BLOCK_SIZES:
        sync_mask = check_sync_batch(batch, block_size=bs)
        for i in np.where(sync_mask)[0]:
            try:
                h = decode_header_frame(batch[i], block_size=bs)
                if h['magic'] == MAGIC_V2:
                    return h, bs
            except Exception:
                continue
    return None, None


# ---------------------------------------------------------------------------
# v3 decode
# ---------------------------------------------------------------------------

def _decode_v3(video_path: str, header: dict, block_size: int,
               output_dir: str, log) -> list:
    frame_bytes     = YUV_FRAME_BYTES
    num_data_frames = header['num_data_frames']
    log(f'Header found. Archive: {header["archive_size"]:,} bytes, '
        f'{num_data_frames} data frames.')

    raw_size_est = os.path.getsize(video_path) * 20
    if raw_size_est < _FILE_IO_LIMIT_BYTES:
        all_frames = _read_all_frames_file(video_path, 'yuv420p', frame_bytes)
        if all_frames is None:
            raise RuntimeError('Failed to decode video frames.')
        source = [(i, all_frames[i:i+DECODE_BATCH])
                  for i in range(0, len(all_frames), DECODE_BATCH)]
        proc = None
    else:
        proc   = subprocess.Popen(_build_decode_cmd(video_path, 'yuv420p'),
                                  **_popen_kw())
        source = _iter_batches_pipe(proc, frame_bytes, DECODE_BATCH)

    found_header     = False
    bit_chunks: list = []
    frames_read      = 0

    for batch_start, batch in source:
        if not found_header:
            sync_mask = check_sync_yuv(batch, block_size=block_size)
            for i in np.where(sync_mask)[0]:
                try:
                    h = yuv_frame_to_header(batch[i], block_size=block_size)
                    if h['magic'] == MAGIC:
                        found_header = True
                        tail = batch[i + 1:]
                        if len(tail):
                            bit_chunks.append(yuv_frames_to_bits(tail, block_size))
                            frames_read += len(tail)
                        break
                except Exception:
                    continue
            continue

        bit_chunks.append(yuv_frames_to_bits(batch, block_size))
        frames_read += len(batch)

        if frames_read % (DECODE_BATCH * 10) == 0:
            log(f'  frame {frames_read}/{num_data_frames}...')

        if frames_read >= num_data_frames:
            if proc is not None:
                proc.stdout.close()
            break

    return _finish_decode(header, bit_chunks, frames_read, BITS_PER_FRAME, output_dir, log)


# ---------------------------------------------------------------------------
# v2 decode  (backward compat — unchanged logic from original)
# ---------------------------------------------------------------------------

def _decode_v2(video_path: str, header: dict, block_size: int,
               output_dir: str, log) -> list:
    from video_codec import _params
    p               = _params(block_size, FRAME_WIDTH, FRAME_HEIGHT, 1)
    bpf             = p['bpf']
    frame_bytes     = FRAME_WIDTH * FRAME_HEIGHT
    num_data_frames = header['num_data_frames']
    log(f'Header found (v2). Archive: {header["archive_size"]:,} bytes, '
        f'{num_data_frames} data frames.')

    raw_size_est = os.path.getsize(video_path) * 20
    if raw_size_est < _FILE_IO_LIMIT_BYTES:
        all_frames = _read_all_frames_file(video_path, 'gray', frame_bytes)
        if all_frames is None:
            raise RuntimeError('Failed to decode video frames.')
        source = [(i, all_frames[i:i+DECODE_BATCH].reshape(-1, FRAME_HEIGHT, FRAME_WIDTH))
                  for i in range(0, len(all_frames), DECODE_BATCH)]
        proc = None
    else:
        proc   = subprocess.Popen(_build_decode_cmd(video_path, 'gray'), **_popen_kw())
        source = [(bi, arr.reshape(-1, FRAME_HEIGHT, FRAME_WIDTH))
                  for bi, arr in _iter_batches_pipe(proc, frame_bytes, DECODE_BATCH)]

    found_header     = False
    bit_chunks: list = []
    frames_read      = 0

    for batch_start, batch in source:
        if not found_header:
            sync_mask = check_sync_batch(batch, block_size=block_size)
            for i in np.where(sync_mask)[0]:
                try:
                    h = decode_header_frame(batch[i], block_size=block_size)
                    if h['magic'] == MAGIC_V2:
                        found_header = True
                        tail = batch[i + 1:]
                        if len(tail):
                            bit_chunks.append(frames_to_bits_batch(tail, block_size))
                            frames_read += len(tail)
                        break
                except Exception:
                    continue
            continue

        bit_chunks.append(frames_to_bits_batch(batch, block_size))
        frames_read += len(batch)

        if frames_read >= num_data_frames:
            if proc is not None:
                proc.stdout.close()
            break

    return _finish_decode(header, bit_chunks, frames_read, bpf, output_dir, log)


# ---------------------------------------------------------------------------
# Common finish: RS decode → untar → output
# ---------------------------------------------------------------------------

def _finish_decode(header: dict, bit_chunks: list, frames_read: int,
                   bpf: int, output_dir: str, log) -> list:
    num_data_frames = header['num_data_frames']
    nroots          = header['nroots']

    if frames_read < num_data_frames:
        raise RuntimeError(
            f'The video ended earlier than expected '
            f'({frames_read} of {num_data_frames} data frames recovered). '
            'It may have been clipped or only partially downloaded — please try again.'
        )

    log('Reconstructing bytes...')
    all_bits      = np.concatenate(bit_chunks).ravel()[:num_data_frames * bpf]
    encoded_bytes = bits_to_bytes(all_bits)[:header['encoded_size']]

    log('Verifying data integrity...')
    archive_bytes = _rs_strip_parity(encoded_bytes, nroots)[:header['archive_size']]
    actual_crc    = zlib.crc32(archive_bytes) & 0xFFFFFFFF

    if actual_crc != header['crc32']:
        log('Errors detected, applying Reed-Solomon correction...')
        archive_bytes = rs_decode(encoded_bytes, header['archive_size'], nroots=nroots)
        actual_crc    = zlib.crc32(archive_bytes) & 0xFFFFFFFF
        if actual_crc != header['crc32']:
            raise RuntimeError(
                'The recovered data failed its integrity check even after error correction. '
                'YouTube may have re-compressed this video too aggressively. '
                'Try waiting a few more minutes for it to finish processing, then decode again.'
            )
    else:
        log('CRC32 OK — no errors.')

    log('Extracting files...')
    os.makedirs(output_dir, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode='r:gz') as tar:
        tar.extractall(output_dir)
        names = tar.getnames()

    log(f'Done! Extracted {len(names)} item(s).')
    return names

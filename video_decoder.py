"""
Video decoder: H.264 video → original files

Key optimisations vs the original:
  • Batch frame reading: 32 frames piped from ffmpeg per read() call
  • Batch sync detection + bit extraction via vectorised numpy
  • Backward-compatible: auto-detects block_size (4 px or 16 px) from header
  • Hardware-accelerated decode (CUDA → CPU fallback)
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
    BLOCK_SIZE, FRAME_WIDTH, FRAME_HEIGHT, BITS_PER_FRAME,
    NROOTS, CHUNK_IN, MAGIC, FPS,
    HEADER_SIZE, HEADER_REPEAT,
    unpack_header, bits_to_bytes,
    check_sync_batch, frames_to_bits_batch, decode_header_frame,
)

# Block sizes to try when scanning for the header (new first, then legacy)
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


def _rs_strip_parity(data: bytes, nroots: int = NROOTS, original_size: int = 0) -> bytes:
    """
    Fast path: galois uses *systematic* RS codes, so the first k=(255-nroots)
    bytes of every 255-byte codeword ARE the original data bytes.
    Simply slicing them off is zero-error decoding with negligible CPU cost.
    """
    chunk_size = 255
    chunk_in   = chunk_size - nroots
    n          = len(data) // chunk_size
    if n == 0:
        return b''
    arr = np.frombuffer(data[:n * chunk_size], dtype=np.uint8).reshape(n, chunk_size)
    return arr[:, :chunk_in].tobytes()


def rs_decode(data: bytes, original_size: int, nroots: int = NROOTS) -> bytes:
    """
    Full RS error correction.  Only called when the fast-path CRC check fails.
    """
    chunk_size = 255
    chunk_in   = chunk_size - nroots
    n_full     = len(data) // chunk_size
    remainder  = len(data) % chunk_size
    parts: list[bytes] = []

    if n_full > 0:
        arr = np.frombuffer(data[:n_full * chunk_size], dtype=np.uint8) \
                .reshape(-1, chunk_size).astype(int)
        try:
            # Build a RS object matching the actual nroots stored in the header
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
# ffmpeg decode subprocess
# ---------------------------------------------------------------------------

def _popen_kw(**extra):
    kw = dict(stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, **extra)
    if sys.platform == 'win32':
        kw['creationflags'] = subprocess.CREATE_NO_WINDOW
    return kw


def _build_decode_cmd(video_path: str, output: str = 'pipe:1') -> list:
    exe    = imageio_ffmpeg.get_ffmpeg_exe()
    scale  = f'scale={FRAME_WIDTH}:{FRAME_HEIGHT}:flags=neighbor'
    tail   = ['-vf', scale, '-f', 'rawvideo', '-pix_fmt', 'gray']
    if output == 'pipe:1':
        tail += ['pipe:1']
    else:
        tail += ['-y', output]

    # Try CUDA hardware decode first
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


# Max raw frame data to decode to a temp file (file I/O is faster than pipe).
# For larger videos we fall back to streaming pipe.
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
# Batch frame iterator
# ---------------------------------------------------------------------------

DECODE_BATCH = 32   # frames read from ffmpeg per call


def _iter_batches(proc, batch_size: int):
    """
    Yield (start_frame_idx, ndarray(N, H, W)) uint8 from a running ffmpeg process.
    N may be < batch_size at the end of the video.
    """
    frame_bytes  = FRAME_WIDTH * FRAME_HEIGHT
    batch_bytes  = frame_bytes * batch_size
    buf          = bytearray()
    frame_idx    = 0

    while True:
        need  = batch_bytes - len(buf)
        chunk = proc.stdout.read(need)
        if chunk:
            buf += chunk
        if len(buf) < frame_bytes:
            break
        n      = len(buf) // frame_bytes
        usable = n * frame_bytes
        arr    = np.frombuffer(bytes(buf[:usable]), dtype=np.uint8) \
                   .reshape(n, FRAME_HEIGHT, FRAME_WIDTH).copy()
        yield frame_idx, arr
        buf        = bytearray(buf[usable:])
        frame_idx += n
        if not chunk:
            break

    proc.wait()

# ---------------------------------------------------------------------------
# Main decode entry point
# ---------------------------------------------------------------------------

def decode_video_to_files(video_path: str, output_dir: str, progress=None) -> list:
    def log(msg):
        if progress:
            progress(msg)

    w, h = probe_video(video_path)
    log(f'Downloaded video resolution: {w}x{h}')
    if w and h and (w < FRAME_WIDTH or h < FRAME_HEIGHT):
        raise RuntimeError(
            f'The video is only {w}x{h} — full 1080p is required for decoding. '
            'YouTube usually takes 2–5 minutes to finish processing the high-resolution '
            'version after upload. Please wait a moment and try again.'
        )

    log('Scanning frames for sync pattern...')

    # Prefer file I/O over pipe: ffmpeg writes at full disk speed, Python
    # reads everything in one numpy call. Faster than piping for videos that
    # fit within the disk-space budget.
    _tmp_raw = None
    raw_size_est = os.path.getsize(video_path) * 20   # rough upper-bound
    if raw_size_est < _FILE_IO_LIMIT_BYTES:
        import tempfile
        _tmp_raw = tempfile.mktemp(suffix='.raw')
        kw_run = dict(stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if sys.platform == 'win32':
            kw_run['creationflags'] = subprocess.CREATE_NO_WINDOW
        subprocess.run(_build_decode_cmd(video_path, output=_tmp_raw), **kw_run)

    if _tmp_raw and os.path.exists(_tmp_raw):
        # Read entire raw file in one shot → fast numpy parse
        frame_bytes = FRAME_WIDTH * FRAME_HEIGHT
        raw = np.fromfile(_tmp_raw, dtype=np.uint8)
        os.unlink(_tmp_raw)
        n_frames = len(raw) // frame_bytes
        all_frames_arr = raw[:n_frames * frame_bytes].reshape(n_frames, FRAME_HEIGHT, FRAME_WIDTH)
        frame_batches = [(i, all_frames_arr[i:i+DECODE_BATCH])
                         for i in range(0, n_frames, DECODE_BATCH)]
        proc = None
    else:
        if _tmp_raw and os.path.exists(_tmp_raw):
            os.unlink(_tmp_raw)
        proc = subprocess.Popen(_build_decode_cmd(video_path), **_popen_kw())
        frame_batches = None

    header           = None
    block_size       = None   # determined from header
    bit_chunks: list = []
    data_frames_read = 0
    num_data_frames  = None

    source = frame_batches if frame_batches is not None else _iter_batches(proc, DECODE_BATCH)
    for batch_start, frames in source:

        # ---- Phase 1: find header ----------------------------------------
        if header is None:
            for bs in _CANDIDATE_BLOCK_SIZES:
                sync_mask = check_sync_batch(frames, block_size=bs)
                hits = np.where(sync_mask)[0]
                for i in hits:
                    try:
                        h = decode_header_frame(frames[i], block_size=bs)
                        if h['magic'] != MAGIC:
                            continue
                        header          = h
                        block_size      = bs
                        num_data_frames = h['num_data_frames']
                        log(f'Header found at frame {batch_start + i}! '
                            f'Archive: {h["archive_size"]:,} bytes, '
                            f'{num_data_frames} data frames '
                            f'(block_size={bs}).')
                        # Data frames that follow the header in this same batch
                        tail = frames[i + 1:]
                        if len(tail):
                            bits = frames_to_bits_batch(tail, block_size=block_size)
                            bit_chunks.append(bits)
                            data_frames_read += len(tail)
                        break
                    except Exception:
                        continue
                if header is not None:
                    break
            # Still no header — first few frames might lack sync, keep scanning
            if header is None and batch_start < 60:
                if batch_start == 0:
                    # Report sync accuracy for the very first frame
                    for bs in _CANDIDATE_BLOCK_SIZES:
                        from video_codec import _params, THRESHOLD, SYNC_ROWS
                        p = _params(bs)
                        sr = frames[:1, :p['sh'], :].astype(np.float32)
                        view = sr.reshape(1, SYNC_ROWS, p['bs'], p['bx'], p['bs'])
                        inner = view[:, :, p['m']:p['bs']-p['m'], :, p['m']:p['bs']-p['m']]
                        means = inner.mean(axis=(2, 4))
                        got   = (means >= THRESHOLD).astype(np.uint8) * 255
                        acc   = float(np.mean(got == p['sync_exp'][np.newaxis]))
                        log(f'Frame 0: sync accuracy {acc:.1%} (block_size={bs}, need 75%)')
            continue

        # ---- Phase 2: accumulate data frames ------------------------------
        bits = frames_to_bits_batch(frames, block_size=block_size)
        bit_chunks.append(bits)
        data_frames_read += len(frames)

        if data_frames_read % (DECODE_BATCH * 10) == 0:
            log(f'  frame {data_frames_read}/{num_data_frames}...')

        if num_data_frames and data_frames_read >= num_data_frames:
            if proc is not None:
                proc.stdout.close()   # no need to read further
            break

    if header is None:
        raise RuntimeError(
            "This doesn't appear to be a VidCompiler video — no data marker was found. "
            'Double-check the URL. If you just uploaded it, '
            'YouTube may still be processing; try again in a few minutes.'
        )
    if data_frames_read < num_data_frames:
        raise RuntimeError(
            f'The video ended earlier than expected '
            f'({data_frames_read} of {num_data_frames} data frames recovered). '
            'It may have been clipped or only partially downloaded — please try again.'
        )

    from video_codec import _params
    bpf = _params(block_size)['bpf']

    log('Reconstructing bytes...')
    all_bits      = np.concatenate(bit_chunks).ravel()[:num_data_frames * bpf]
    encoded_bytes = bits_to_bytes(all_bits)[:header['encoded_size']]
    nroots        = header['nroots']

    # Fast path: strip parity bytes without error correction (microseconds).
    # Valid because galois uses systematic RS — data bytes come first.
    log('Verifying data integrity...')
    archive_bytes = _rs_strip_parity(encoded_bytes, nroots)[:header['archive_size']]
    actual_crc    = zlib.crc32(archive_bytes) & 0xFFFFFFFF

    if actual_crc != header['crc32']:
        # Bit errors detected — run full RS error correction
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

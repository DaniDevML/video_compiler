"""
Video decoder (v4): H.264/AAC video → original files

Handles v4 (3-bpp Y + 2-bpp Cb/Cr), v3 (2-bpp Y + 1-bpp Cb/Cr), and
v2 (grayscale 1-bpp) by auto-detecting the magic bytes in the header frame.
"""

import io
import os
import subprocess
import sys
import tarfile
import tempfile
import zlib

from concurrent.futures import ThreadPoolExecutor as _ThreadPoolExecutor

import numpy as np
import imageio_ffmpeg

from video_codec import (
    BLOCK_SIZE, FRAME_WIDTH, FRAME_HEIGHT,
    BITS_PER_FRAME, BYTES_PER_FRAME,
    YUV_FRAME_BYTES, Y_PLANE_BYTES, CB_PLANE_BYTES,
    NROOTS, CHUNK_IN, FPS,
    HEADER_SIZE, HEADER_REPEAT,
    MAGIC, MAGIC_V3, MAGIC_V2,
    MAGIC_V4,
    unpack_header, bits_to_bytes, decode_sidecar,
    yuv_frames_to_bits, yuv_frames_to_packed,
    check_sync_yuv, yuv_frame_to_header, profile_from_header,
    Profile, PROFILE_HEADER, PROFILE_V4, PROFILE_V3, DEFAULT_PROFILE,
    # v2 compat
    check_sync_batch, frames_to_bits_batch, decode_header_frame,
    NATIVE_AVAILABLE, PACKED_AVAILABLE, NATIVE_THREADS,
)

try:
    from native import RS_AVAILABLE, rs_decode_batch_c
except ImportError:
    RS_AVAILABLE = False

_CANDIDATE_BLOCK_SIZES = [4, 16]

# Profiles the header frame might be written in. v5 always uses PROFILE_HEADER;
# v4 and v3 wrote the header frame in their own payload format.
_HEADER_PROFILES = [PROFILE_HEADER, PROFILE_V4, PROFILE_V3]

_V5_MAGICS = (MAGIC, MAGIC_V4, MAGIC_V3)

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


_RS_WORKERS = min(12, (os.cpu_count() or 4))
_RS_THREAD_MIN_CHUNKS = 64

# Filled in by the last rs_decode() call: chunks seen, chunks repaired, total
# symbols repaired, chunks beyond repair.
LAST_CORRECTION_STATS: dict = {}


def _rs_decode_parallel(rs, arr: np.ndarray) -> bytes:
    """Reed-Solomon decode split across threads.

    Same rationale as the encoder: galois runs numba-compiled ufuncs that
    release the GIL, so splitting the chunk matrix genuinely parallelises.
    Correction is per-chunk, so splitting cannot change the result.
    """
    if len(arr) < _RS_THREAD_MIN_CHUNKS or _RS_WORKERS < 2:
        return np.asarray(rs.decode(arr.view(_GF)), dtype=np.uint8).tobytes()

    bounds = np.linspace(0, len(arr), _RS_WORKERS + 1).astype(int)
    parts  = [arr[a:b] for a, b in zip(bounds[:-1], bounds[1:]) if b > a]
    with _ThreadPoolExecutor(max_workers=len(parts)) as ex:
        outs = list(ex.map(
            lambda c: np.asarray(rs.decode(c.view(_GF)), dtype=np.uint8),
            parts))
    return np.concatenate(outs).tobytes()


# A screening pass was tried here and removed: re-encode each chunk's data half,
# compare parity, and correct only the chunks that differ. At the ~1e-05 error
# rate YouTube produces only about 2% of chunks are damaged, so it looked like a
# large win. Measured, it was not one -- galois already short-circuits clean
# chunks, so decode cost tracks the number of *erroneous* chunks rather than the
# total. Correcting the 444 damaged chunks of a 20,000-chunk archive took 3.2 s
# against 4.3 s to run the whole array through. Not worth the extra code path.


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
                .reshape(-1, chunk_size)
        if RS_AVAILABLE:
            # The native decoder implements the same code as galois (verified
            # bit-for-bit in bench/test_rs_native.py) around 55x faster, which
            # matters because correction runs over the whole archive whenever
            # even one error is present -- which is always, off YouTube.
            fixed, status = rs_decode_batch_c(arr, nroots)
            # Telemetry: how hard the error correction had to work. A useful
            # health signal for a channel we do not control -- a video that
            # decodes fine but needed 30% of its blocks repaired is close to
            # the edge, and that is invisible from success alone.
            LAST_CORRECTION_STATS.update(
                chunks=int(len(status)),
                repaired=int(np.count_nonzero(status > 0)),
                symbols=int(status[status > 0].sum()) if len(status) else 0,
                failed=int(np.count_nonzero(status < 0)),
            )
            n_bad = int(np.count_nonzero(status < 0))
            if n_bad:
                raise RuntimeError(
                    f"Couldn't repair the data errors in this video "
                    f'({n_bad:,} of {n_full:,} blocks were too damaged to '
                    'correct). YouTube may have re-compressed it too '
                    'aggressively, or this might be the wrong video URL.'
                )
            parts.append(fixed[:, :chunk_in].tobytes())
        else:
            try:
                rs = _galois.ReedSolomon(255, chunk_in) if nroots != NROOTS else _RS
                parts.append(_rs_decode_parallel(rs, arr))
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


_SIZE_CACHE: dict = {}


def _needs_scale(video_path: str) -> bool:
    """Whether this video has to be rescaled to the codec's frame geometry.

    Almost never true: YouTube returns 1080p and the encoder wrote 1080p. When
    it is false the scale filter is a full-frame no-op worth skipping.
    """
    if video_path not in _SIZE_CACHE:
        _SIZE_CACHE[video_path] = probe_video(video_path)
    w, h = _SIZE_CACHE[video_path]
    return not (w == FRAME_WIDTH and h == FRAME_HEIGHT)


def _build_decode_cmd(video_path: str, pix_fmt: str, output: str = 'pipe:1',
                      max_frames: int | None = None) -> list:
    """Build ffmpeg decode command for either yuv420p (v4/v3) or gray (v2).

    `max_frames` limits how much of the video is decoded, which is what makes
    format detection cheap: it only needs the opening frames, not the whole
    file.

    Decoding runs on the CPU. CUDA hwaccel was measured to be slower here, not
    faster: the frames have to come back over PCIe to reach system memory, and
    that transfer costs more than the decode it saves (measured 142 fps with
    hwaccel against 250 fps on the CPU, and 12 fps for hwaccel without a filter
    to force the download path). See bench/bench_decode_cmd.py.
    """
    exe  = imageio_ffmpeg.get_ffmpeg_exe()
    tail = []
    if max_frames:
        tail += ['-frames:v', str(max_frames)]
    if _needs_scale(video_path):
        tail += ['-vf', f'scale={FRAME_WIDTH}:{FRAME_HEIGHT}:flags=neighbor']
    tail += ['-f', 'rawvideo', '-pix_fmt', pix_fmt]
    tail += ['pipe:1'] if output == 'pipe:1' else ['-y', output]
    return [exe, '-threads', '0', '-i', video_path] + tail


def probe_video(video_path: str) -> tuple:
    if video_path in _SIZE_CACHE:
        return _SIZE_CACHE[video_path]
    import re
    exe = imageio_ffmpeg.get_ffmpeg_exe()
    kw  = dict(stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if sys.platform == 'win32':
        kw['creationflags'] = subprocess.CREATE_NO_WINDOW
    _, stderr = subprocess.Popen([exe, '-i', video_path], **kw).communicate()
    m = re.search(r'Video:.*?(\d{3,5})x(\d{3,5})', stderr.decode(errors='replace'))
    size = (int(m.group(1)), int(m.group(2))) if m else (None, None)
    _SIZE_CACHE[video_path] = size
    return size

# ---------------------------------------------------------------------------
# Raw-frame iterators
# ---------------------------------------------------------------------------

# Small batches decode markedly faster than large ones. A batch is read in
# full before any pixel work starts, so the batch size sets how long ffmpeg
# sits blocked on a full pipe while we demodulate. Measured on 1080p: 158 fps
# at 4 frames per batch against 62 at 32 and 34 at 64. See
# bench/diag_decode_pipe.py.
DECODE_BATCH = 4


def _iter_batches_pipe(proc, frame_bytes: int, batch_size: int):
    """Yield (first_frame_index, (n, frame_bytes) uint8) batches from ffmpeg.

    Reads straight into a NumPy buffer with readinto, so a batch makes no
    intermediate copies on its way from the pipe to the pixel decoder.

    The buffer is allocated once and reused. Allocating a fresh multi-hundred-
    megabyte array per batch cost more than the decode itself: at batch=64 the
    pipeline ran at 34 fps against 131 fps at batch=8, purely from allocation
    and page-fault overhead.

    Consequently the yielded array is only valid until the next iteration.
    Every caller here consumes it immediately (demodulating it into packed
    bytes, or reading a header out of it) before advancing.
    """
    frame_idx   = 0
    batch_bytes = frame_bytes * batch_size
    arr = np.empty(batch_bytes, dtype=np.uint8)
    mv  = memoryview(arr.data).cast('B')

    while True:
        filled = 0
        while filled < batch_bytes:
            n = proc.stdout.readinto(mv[filled:])
            if not n:
                break
            filled += n

        n_frames = filled // frame_bytes
        if n_frames:
            yield frame_idx, arr[:n_frames * frame_bytes].reshape(n_frames, -1)
            frame_idx += n_frames
        if filled < batch_bytes:
            break

    try:
        proc.stdout.close()
    except Exception:
        pass
    proc.wait()


def _open_stream(video_path: str, pix_fmt: str, max_frames: int | None = None):
    return subprocess.Popen(
        _build_decode_cmd(video_path, pix_fmt, max_frames=max_frames),
        **_popen_kw(bufsize=1 << 22))


# ---------------------------------------------------------------------------
# Main decode entry point
# ---------------------------------------------------------------------------

def decode_video_to_files(video_path: str, output_dir: str, progress=None,
                          description: str = '') -> list:
    """Decode a VidCompiler video back into files.

    `description` is the video's YouTube description. If it carries the header
    sidecar the format-detection pass is skipped entirely, since detection
    exists only to recover the parameters the sidecar already states.
    """
    def log(msg):
        if progress:
            progress(msg)

    if NATIVE_AVAILABLE:
        mode = 'packed' if PACKED_AVAILABLE else 'byte-per-bit'
        log(f'Pixel engine: C native library ({mode}, {NATIVE_THREADS} thread'
            f'{"s" if NATIVE_THREADS != 1 else ""})')
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

    header = decode_sidecar(description)
    if header is not None:
        version = 2 if header['magic'] == MAGIC_V2 else 3
        log('Header recovered from the video description.')
    else:
        # ── Detect v2 (gray) vs v5/v4/v3 (yuv420p) from the opening frames ──
        log('Detecting video format...')
        try:
            version, header, _hdr_profile = _detect_version(video_path, log)
        except RuntimeError:
            # Last resort: the header backup carried in the audio track.
            header = header_from_audio(video_path)
            if header is None:
                raise
            log('Header recovered from the audio channel.')
            version = 2 if header['magic'] == MAGIC_V2 else 3

    if version == 3:
        profile = profile_from_header(header)
        log(f'Format: {header["magic"].decode()}  '
            f'Y {profile.block_y}px/{profile.bpp_y}bpp + '
            f'C {profile.block_c}px/{profile.bpp_c}bpp  '
            f'({profile.bytes:,} bytes/frame)')
        return _decode_v3(video_path, header, profile, output_dir, log)
    else:
        log(f'Format: v2 grayscale 1-bpp (block_size={header["block_size"]})')
        return _decode_v2(video_path, header, header['block_size'],
                          output_dir, log)


# ---------------------------------------------------------------------------
# Version detection
# ---------------------------------------------------------------------------

# The header is written as the very first frame, so detection only needs a
# short prefix. A few spare frames cover a leading frame being dropped.
_DETECT_FRAMES = 16


def header_from_audio(media_path: str) -> dict | None:
    """Recover the header from the FSK audio track, if the file has one.

    The encoder has always written this backup copy, but nothing read it.
    It is the last fallback: used only when the description sidecar is absent
    and no header frame could be found in the pixels.
    """
    import audio_codec as _ac

    exe = imageio_ffmpeg.get_ffmpeg_exe()
    tmp = tempfile.mktemp(suffix='.raw')
    try:
        kw = dict(stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if sys.platform == 'win32':
            kw['creationflags'] = subprocess.CREATE_NO_WINDOW
        r = subprocess.run(
            [exe, '-i', media_path, '-vn', '-f', 's16le',
             '-ar', str(_ac.SAMPLE_RATE), '-ac', '1', '-y', tmp], **kw)
        if r.returncode != 0 or not os.path.exists(tmp) \
                or os.path.getsize(tmp) == 0:
            return None
        with open(tmp, 'rb') as f:
            pcm = f.read()
        raw = _ac.decode_audio(pcm)
        if not raw or raw[:8] not in (MAGIC, MAGIC_V3, MAGIC_V2):
            return None
        return unpack_header(raw)
    except Exception:
        return None
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _detect_version(video_path: str, log):
    """
    Read just enough frames to find and parse the header.
    Returns (version: int, header: dict, block_size: int).

    Only the opening frames are decoded. Earlier versions decoded the entire
    video here and then decoded it a second time to read the payload, which
    made detection as expensive as the decode itself.
    """
    # Try yuv420p first (v3/v4), fall back to gray (v2)
    for pix_fmt, frame_bytes, v_candidate in [
        ('yuv420p', YUV_FRAME_BYTES, 3),
        ('gray',    FRAME_WIDTH * FRAME_HEIGHT, 2),
    ]:
        proc = _open_stream(video_path, pix_fmt, max_frames=_DETECT_FRAMES)
        try:
            for batch_start, batch in _iter_batches_pipe(
                    proc, frame_bytes, DECODE_BATCH):
                if v_candidate == 3:
                    header, bs = _try_find_header_v3(batch)
                else:
                    # Gray frames are (N, W*H) — reshape to (N, H, W) for v2
                    header, bs = _try_find_header_v2(
                        batch.reshape(-1, FRAME_HEIGHT, FRAME_WIDTH))
                if header is not None:
                    return v_candidate, header, bs
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()

    raise RuntimeError(
        "This doesn't appear to be a VidCompiler video — no data marker was found. "
        'Double-check the URL. If you just uploaded it, '
        'YouTube may still be processing; try again in a few minutes.'
    )


def _find_header_frame(batch: np.ndarray):
    """Find and decode a v5/v4/v3 header frame in a batch of YUV frames.

    Returns (header dict, header profile, frame index within the batch), or
    (None, None, None). The header frame's own format is not the payload
    format -- v5 writes it in a deliberately sparse profile so it can be read
    before the payload format is known.
    """
    for prof in _HEADER_PROFILES:
        sync_mask = check_sync_yuv(batch, profile=prof)
        for i in np.where(sync_mask)[0]:
            try:
                h = yuv_frame_to_header(batch[i], profile=prof)
                if h['magic'] in _V5_MAGICS:
                    return h, prof, int(i)
            except Exception:
                continue
    return None, None, None


def _try_find_header_v3(batch: np.ndarray):
    """Back-compat wrapper returning just (header, profile)."""
    h, prof, _ = _find_header_frame(batch)
    return h, prof


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

def _decode_v3(video_path: str, header: dict, profile,
               output_dir: str, log) -> list:
    frame_bytes     = YUV_FRAME_BYTES
    num_data_frames = header['num_data_frames']
    log(f'Header found. Archive: {header["archive_size"]:,} bytes, '
        f'{num_data_frames} data frames.')

    # Single streaming pass. Piping avoids staging ~20x the video size as raw
    # YUV on disk, and lets pixel decoding overlap with ffmpeg.
    proc   = _open_stream(video_path, 'yuv420p')
    source = _iter_batches_pipe(proc, frame_bytes, DECODE_BATCH)

    found_header      = False
    byte_chunks: list = []
    frames_read       = 0

    for batch_start, batch in source:
        if not found_header:
            h, _prof, idx = _find_header_frame(batch)
            if h is not None:
                found_header = True
                tail = batch[idx + 1:]
                if len(tail):
                    byte_chunks.append(yuv_frames_to_packed(tail, profile))
                    frames_read += len(tail)
            continue

        byte_chunks.append(yuv_frames_to_packed(batch, profile))
        frames_read += len(batch)

        if frames_read % (DECODE_BATCH * 10) == 0:
            log(f'  frame {frames_read}/{num_data_frames}...')

        if frames_read >= num_data_frames:
            break

    if proc.poll() is None:
        proc.kill()
        proc.wait()

    _require_all_frames(frames_read, num_data_frames)
    log('Reconstructing bytes...')
    encoded_bytes = (np.concatenate(byte_chunks).tobytes()[:header['encoded_size']]
                     if byte_chunks else b'')
    return _finish_decode(header, encoded_bytes, output_dir, log)


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

    proc   = _open_stream(video_path, 'gray')
    source = ((bi, arr.reshape(-1, FRAME_HEIGHT, FRAME_WIDTH))
              for bi, arr in _iter_batches_pipe(proc, frame_bytes, DECODE_BATCH))

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
            break

    if proc.poll() is None:
        proc.kill()
        proc.wait()

    _require_all_frames(frames_read, num_data_frames)
    log('Reconstructing bytes...')
    all_bits      = np.concatenate(bit_chunks).ravel()[:num_data_frames * bpf]
    encoded_bytes = bits_to_bytes(all_bits)[:header['encoded_size']]
    return _finish_decode(header, encoded_bytes, output_dir, log)


# ---------------------------------------------------------------------------
# Common finish: RS decode → untar → output
# ---------------------------------------------------------------------------

def _require_all_frames(frames_read: int, num_data_frames: int) -> None:
    if frames_read < num_data_frames:
        raise RuntimeError(
            f'The video ended earlier than expected '
            f'({frames_read} of {num_data_frames} data frames recovered). '
            'It may have been clipped or only partially downloaded — please try again.'
        )


def _finish_decode(header: dict, encoded_bytes: bytes,
                   output_dir: str, log) -> list:
    nroots = header['nroots']

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

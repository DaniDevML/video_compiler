"""
audio_codec.py — Store extra bytes in the video's audio channel using binary FSK.

How it works:
  The audio track carries a continuous FSK (Frequency-Shift Keying) signal:
    bit 0 → sine wave at FREQ_0 (1 500 Hz)
    bit 1 → sine wave at FREQ_1 (3 000 Hz)

  Symbol duration = SAMPLES_PER_BIT / SAMPLE_RATE = 441 / 44 100 = 10 ms
  Raw throughput  = 100 bits/sec = 12.5 bytes/sec

  Why such a conservative rate?
    YouTube re-encodes audio to AAC 128 kbps.  AAC uses psychoacoustic masking
    and quantisation noise shaping.  At 10 ms per symbol the decoder can average
    many samples before deciding, making it robust to AAC-introduced distortion.
    A 1 500 Hz / 3 000 Hz frequency pair sits well within AAC's flat-response
    band (20 Hz – 16 kHz), so both tones survive with negligible distortion.

  Reed-Solomon error correction is applied to the audio payload too (same
  RS(255, 215) as the video codec) so the audio channel can correct up to 20
  byte-errors per 255-byte chunk.

Capacity example:
  A 216 MB file takes ≈ 90 minutes of video at the v3 pixel data rate.
  Audio adds 12.5 bytes/sec × 5 400 sec = 67 500 bytes ≈ 67 KB bonus.
  That is small relative to the main payload, but it is essentially free
  because the audio track has to exist anyway and YouTube keeps it.

  For small files (≤ a few KB) the audio channel alone could carry the data —
  no video frames needed — which could be a future optimisation.

Reed-Solomon import note:
  Uses the same galois-based RS as the rest of the codec (loaded lazily so this
  module can be imported without triggering the galois/numba JIT warmup).
"""

import math
import struct

import numpy as np

# ─── Audio parameters ────────────────────────────────────────────────────────
SAMPLE_RATE       = 44_100   # Hz
FREQ_0            = 1_500    # Hz  (represents bit 0)
FREQ_1            = 3_000    # Hz  (represents bit 1)
BITS_PER_SEC      = 100      # symbol rate
SAMPLES_PER_BIT   = SAMPLE_RATE // BITS_PER_SEC   # 441 samples / bit
AMPLITUDE         = 0.25     # fraction of full scale (keeps headroom for AAC)

# RS params (same as video codec to reuse the pre-warmed galois RS object)
AUDIO_NROOTS  = 40
AUDIO_CHUNK_IN = 255 - AUDIO_NROOTS  # 215

# Preamble: 2-second tone burst at FREQ_0, followed by 0.5s silence.
# The decoder looks for this pattern to find the start of encoded data.
PREAMBLE_SECS  = 0.5
SILENCE_SECS   = 0.25
PREAMBLE_BITS  = int(PREAMBLE_SECS  * BITS_PER_SEC)
SILENCE_BITS   = int(SILENCE_SECS   * BITS_PER_SEC)

# Sync word placed right after the preamble (8 bytes = 64 bits)
SYNC_WORD = b'\xAA\x55\xAA\x55\xDE\xAD\xBE\xEF'


# ─── RS helpers (lazy import so galois JIT is not triggered until needed) ─────

_rs_enc = None
_rs_dec = None


def _get_rs():
    global _rs_enc, _rs_dec
    if _rs_enc is None:
        import galois as _g
        _GF  = _g.GF(2 ** 8)
        _RS  = _g.ReedSolomon(255, AUDIO_CHUNK_IN)
        _rs_enc = (_GF, _RS)
        _rs_dec = (_GF, _RS)
    return _rs_enc


def _rs_encode_audio(data: bytes) -> bytes:
    GF, RS = _get_rs()
    import numpy as np
    pad    = (-len(data)) % AUDIO_CHUNK_IN
    padded = data + b'\x00' * pad
    chunks = np.frombuffer(padded, dtype=np.uint8).reshape(-1, AUDIO_CHUNK_IN).astype(int)
    enc    = RS.encode(GF(chunks))
    return np.asarray(enc, dtype=np.uint8).tobytes()


def _rs_decode_audio(data: bytes) -> bytes:
    GF, RS = _get_rs()
    import numpy as np
    chunk_size = 255
    n = len(data) // chunk_size
    if n == 0:
        return b''
    arr = np.frombuffer(data[:n * chunk_size], dtype=np.uint8).reshape(n, chunk_size).astype(int)
    dec = RS.decode(GF(arr))
    return np.asarray(dec, dtype=np.uint8).tobytes()


# ─── Phase-continuous FSK signal generation ──────────────────────────────────

def _bits_to_signal(bits: np.ndarray) -> np.ndarray:
    """Convert 0/1 bit array to a phase-continuous FSK float32 signal."""
    freqs = np.where(bits == 1, float(FREQ_1), float(FREQ_0))
    # Repeat each frequency for SAMPLES_PER_BIT samples
    freq_samples = np.repeat(freqs, SAMPLES_PER_BIT)
    # Accumulate phase for phase-continuity (no clicks between symbols)
    phase = np.cumsum(2.0 * math.pi * freq_samples / SAMPLE_RATE)
    return (np.sin(phase) * AMPLITUDE).astype(np.float32)


def _make_preamble_signal() -> np.ndarray:
    """Tone at FREQ_0 for PREAMBLE_SECS, then silence for SILENCE_SECS."""
    t_pre = np.arange(int(PREAMBLE_SECS * SAMPLE_RATE)) / SAMPLE_RATE
    tone  = (np.sin(2.0 * math.pi * FREQ_0 * t_pre) * AMPLITUDE).astype(np.float32)
    silence = np.zeros(int(SILENCE_SECS * SAMPLE_RATE), dtype=np.float32)
    return np.concatenate([tone, silence])


# ─── Public API ───────────────────────────────────────────────────────────────

def encode_audio(payload: bytes, total_samples: int) -> np.ndarray:
    """
    Encode payload bytes as an FSK audio signal.

    Parameters
    ----------
    payload       : bytes to embed
    total_samples : total number of PCM samples needed (= video_frames * samples_per_frame)

    Returns
    -------
    float32 array of length total_samples, range [-1, 1], suitable for feeding
    to ffmpeg as s16le after scaling (or directly as f32le).
    """
    rs_payload = _rs_encode_audio(payload)
    data_bits  = np.unpackbits(
        np.frombuffer(struct.pack('<I', len(rs_payload)) + rs_payload, dtype=np.uint8)
    )
    sync_bits  = np.unpackbits(np.frombuffer(SYNC_WORD, dtype=np.uint8))

    preamble_sig = _make_preamble_signal()
    sync_sig     = _bits_to_signal(sync_bits)
    data_sig     = _bits_to_signal(data_bits)

    content = np.concatenate([preamble_sig, sync_sig, data_sig])

    # Fit into total_samples (truncate or zero-pad)
    out = np.zeros(total_samples, dtype=np.float32)
    n   = min(len(content), total_samples)
    out[:n] = content[:n]
    return out


def max_payload_bytes(total_samples: int) -> int:
    """
    How many raw (pre-RS) bytes fit in an audio track of total_samples samples.
    """
    overhead_samples = (
        int(PREAMBLE_SECS * SAMPLE_RATE) +
        int(SILENCE_SECS  * SAMPLE_RATE) +
        len(SYNC_WORD) * 8 * SAMPLES_PER_BIT +
        4 * 8 * SAMPLES_PER_BIT          # 4-byte length prefix
    )
    available_samples = total_samples - overhead_samples
    if available_samples <= 0:
        return 0
    rs_bits   = available_samples // SAMPLES_PER_BIT
    rs_bytes  = rs_bits // 8
    # RS chunks: each 255-byte chunk encodes 215 raw bytes
    n_chunks  = rs_bytes // 255
    return n_chunks * AUDIO_CHUNK_IN


def float32_to_s16(sig: np.ndarray) -> bytes:
    """Convert float32 [-1,1] to signed 16-bit PCM bytes (little-endian)."""
    s16 = np.clip(sig * 32767, -32768, 32767).astype(np.int16)
    return s16.tobytes()


# ─── Decoder ─────────────────────────────────────────────────────────────────

def decode_audio(pcm_s16: bytes, sample_rate: int = SAMPLE_RATE) -> bytes:
    """
    Attempt to recover the payload from raw s16le mono PCM bytes.

    Returns the decoded payload bytes, or b'' if decoding fails / no data found.
    """
    if sample_rate != SAMPLE_RATE:
        # Resample (simple nearest-neighbour — good enough for robustness testing)
        sig_in  = np.frombuffer(pcm_s16, dtype=np.int16).astype(np.float32) / 32768.0
        ratio   = SAMPLE_RATE / sample_rate
        new_len = int(len(sig_in) * ratio)
        idx     = (np.arange(new_len) / ratio).astype(int)
        idx     = np.clip(idx, 0, len(sig_in) - 1)
        sig     = sig_in[idx]
    else:
        sig = np.frombuffer(pcm_s16, dtype=np.int16).astype(np.float32) / 32768.0

    spb = SAMPLES_PER_BIT

    # ── Find preamble: look for a long run of FREQ_0 ───────────────────────
    # Use energy in FREQ_0 band via per-symbol Goertzel
    preamble_start = _find_preamble(sig, spb)
    if preamble_start < 0:
        return b''

    # Skip preamble + silence
    data_start = preamble_start + int((PREAMBLE_SECS + SILENCE_SECS) * SAMPLE_RATE)

    # ── Read sync word ─────────────────────────────────────────────────────
    sync_n    = len(SYNC_WORD) * 8
    sync_bits = _demod_bits(sig[data_start:], sync_n, spb)
    got_sync  = np.packbits(sync_bits).tobytes()
    if got_sync != SYNC_WORD:
        return b''

    data_start += sync_n * spb

    # ── Read 4-byte RS-payload length ──────────────────────────────────────
    len_bits   = _demod_bits(sig[data_start:], 32, spb)
    rs_length  = struct.unpack('<I', np.packbits(len_bits).tobytes())[0]
    data_start += 32 * spb

    if rs_length == 0 or rs_length > len(sig) // spb // 8:
        return b''

    # ── Read RS-encoded payload ────────────────────────────────────────────
    total_bits  = rs_length * 8
    rs_bits_arr = _demod_bits(sig[data_start:], total_bits, spb)
    rs_bytes    = np.packbits(rs_bits_arr).tobytes()[:rs_length]

    try:
        return _rs_decode_audio(rs_bytes)
    except Exception:
        return b''


def _demod_bits(sig: np.ndarray, n_bits: int, spb: int) -> np.ndarray:
    """Demodulate n_bits from the FSK signal using per-symbol energy comparison."""
    bits = np.empty(n_bits, dtype=np.uint8)
    for i in range(n_bits):
        start = i * spb
        end   = start + spb
        if end > len(sig):
            bits[i:] = 0
            break
        chunk = sig[start:end]
        e0 = _goertzel_energy(chunk, FREQ_0, SAMPLE_RATE)
        e1 = _goertzel_energy(chunk, FREQ_1, SAMPLE_RATE)
        bits[i] = 1 if e1 > e0 else 0
    return bits


def _goertzel_energy(x: np.ndarray, freq: float, fs: float) -> float:
    """Goertzel algorithm: energy at a single frequency in array x."""
    N   = len(x)
    k   = freq * N / fs
    w   = 2.0 * math.pi * k / N
    cos_w = math.cos(w)
    coeff = 2.0 * cos_w
    s0 = s1 = s2 = 0.0
    for sample in x:
        s0 = float(sample) + coeff * s1 - s2
        s2 = s1
        s1 = s0
    return s1 * s1 + s2 * s2 - coeff * s1 * s2


def _find_preamble(sig: np.ndarray, spb: int) -> int:
    """
    Scan for a run of at least PREAMBLE_BITS consecutive FREQ_0 symbols.
    Returns the sample index of the first preamble symbol, or -1 if not found.
    """
    required = PREAMBLE_BITS
    run       = 0
    run_start = 0
    step      = spb
    i         = 0
    while i + spb <= len(sig):
        chunk = sig[i:i + spb]
        e0 = _goertzel_energy(chunk, FREQ_0, SAMPLE_RATE)
        e1 = _goertzel_energy(chunk, FREQ_1, SAMPLE_RATE)
        if e0 > e1 * 1.5:   # clearly FREQ_0
            if run == 0:
                run_start = i
            run += 1
            if run >= required:
                return run_start
        else:
            run = 0
        i += step
    return -1

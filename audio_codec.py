"""
audio_codec.py — Store extra bytes in the video's audio channel using M-ary FSK.

Each symbol is one of M tones, carrying log2(M) bits. The tone set, symbol rate
and spacing were chosen by measurement against a real AAC round trip rather
than by argument; see the parameter block below and bench/sweep_audio_mary.py.

The payload is Reed-Solomon protected with the same RS(255, 215) as the video
codec, so the audio channel can correct up to 20 byte errors per 255-byte chunk.

Scale, honestly: at 609 payload bytes/s against the pixel channel's 1.68 MB/s,
audio contributes about 0.04% of total capacity. Its value is redundancy -- it
carries a backup copy of the header -- not throughput.
"""

import math
import struct

import numpy as np

# ─── Audio parameters ────────────────────────────────────────────────────────
#
# M-ary FSK: each symbol is one of M = 2^BITS_PER_SYMBOL tones, carrying
# BITS_PER_SYMBOL bits instead of one.
#
# The tones must stay orthogonal over a symbol, which requires a spacing of at
# least the symbol rate (1/T). That couples the three knobs: with usable
# bandwidth B, M tones at spacing Rs need M*Rs <= B, so the throughput
# log2(M)*Rs is bounded by log2(M)/M * B -- which is maximised near M = 3.
# Pure bandwidth arguments therefore say M-ary FSK barely beats binary.
#
# What tips it is that the channel is AAC, not white noise. AAC's transform
# smears very short symbols, so there is a floor on symbol duration that has
# nothing to do with bandwidth; below roughly 8 samples per symbol binary FSK
# stops working however much bandwidth is free. M-ary buys more bits per symbol
# at a symbol duration AAC can actually preserve.
#
# Measured, the theory holds: sweeping every viable combination through a real
# AAC 128k round trip, 4-FSK at 3150 symbols/s wins at 6300 raw bits/s. That is
# the bandwidth-limited optimum -- the next symbol rate up would push the top
# tone past AAC's cutoff. 8-FSK and 16-FSK both work but carry less.
# 609 payload bytes/s, 2.83x the previous binary setting.
# See bench/sweep_audio_mary.py.
SAMPLE_RATE       = 44_100   # Hz
BITS_PER_SYMBOL   = 2        # M = 4 tones
SYMBOL_RATE       = 3_150    # symbols/sec (must divide SAMPLE_RATE)
SAMPLES_PER_SYMBOL = SAMPLE_RATE // SYMBOL_RATE   # 14 samples/symbol
FREQ_BASE         = 3_150    # Hz, lowest tone
FREQ_SPACING      = 3_150    # Hz between tones; top tone lands at 12.6 kHz
AMPLITUDE         = 0.25     # fraction of full scale (keeps headroom for AAC)

# Derived
N_TONES           = 1 << BITS_PER_SYMBOL
BITS_PER_SEC      = SYMBOL_RATE * BITS_PER_SYMBOL

# Legacy aliases: binary-FSK names kept so older callers still resolve.
SAMPLES_PER_BIT   = SAMPLES_PER_SYMBOL
FREQ_0            = FREQ_BASE
FREQ_1            = FREQ_BASE + FREQ_SPACING


def tone_freqs():
    """The M tone frequencies, one per symbol value."""
    return [FREQ_BASE + i * FREQ_SPACING for i in range(N_TONES)]


def configure(bits_per_symbol=None, symbol_rate=None,
              freq_base=None, freq_spacing=None):
    """Reconfigure the modem. Used by the sweep benchmarks."""
    global BITS_PER_SYMBOL, SYMBOL_RATE, SAMPLES_PER_SYMBOL, FREQ_BASE
    global FREQ_SPACING, N_TONES, BITS_PER_SEC, SAMPLES_PER_BIT
    global FREQ_0, FREQ_1, PREAMBLE_SYMS, SILENCE_SYMS
    if bits_per_symbol is not None: BITS_PER_SYMBOL = bits_per_symbol
    if symbol_rate     is not None: SYMBOL_RATE = symbol_rate
    if freq_base       is not None: FREQ_BASE = freq_base
    if freq_spacing    is not None: FREQ_SPACING = freq_spacing
    SAMPLES_PER_SYMBOL = SAMPLE_RATE // SYMBOL_RATE
    N_TONES = 1 << BITS_PER_SYMBOL
    BITS_PER_SEC = SYMBOL_RATE * BITS_PER_SYMBOL
    SAMPLES_PER_BIT = SAMPLES_PER_SYMBOL
    FREQ_0 = FREQ_BASE
    FREQ_1 = FREQ_BASE + FREQ_SPACING
    PREAMBLE_SYMS = int(PREAMBLE_SECS * SYMBOL_RATE)
    SILENCE_SYMS = int(SILENCE_SECS * SYMBOL_RATE)


# RS params (same as video codec to reuse the pre-warmed galois RS object)
AUDIO_NROOTS  = 40
AUDIO_CHUNK_IN = 255 - AUDIO_NROOTS  # 215

# Preamble: 2-second tone burst at FREQ_0, followed by 0.5s silence.
# The decoder looks for this pattern to find the start of encoded data.
PREAMBLE_SECS  = 0.5
SILENCE_SECS   = 0.25
PREAMBLE_SYMS  = int(PREAMBLE_SECS * SYMBOL_RATE)
SILENCE_SYMS   = int(SILENCE_SECS  * SYMBOL_RATE)
PREAMBLE_BITS  = PREAMBLE_SYMS      # legacy alias
SILENCE_BITS   = SILENCE_SYMS

# Sync word placed right after the preamble (8 bytes = 64 bits)
SYNC_WORD = b'\xAA\x55\xAA\x55\xDE\xAD\xBE\xEF'


# ─── RS helpers — reuse the already-warmed galois objects from video_codec ────

def _get_rs():
    """Import the pre-warmed RS objects from video_encoder/decoder (same params)."""
    import galois as _g
    _GF = _g.GF(2 ** 8)
    # Reuse the module-level RS instance if available (already JIT-compiled)
    try:
        from video_codec import CHUNK_IN as _VCI
        if _VCI == AUDIO_CHUNK_IN:
            # Same RS params — import the pre-warmed encoder/decoder
            import video_encoder as _ve
            return _ve._GF, _ve._RS
    except ImportError:
        pass
    return _GF, _g.ReedSolomon(255, AUDIO_CHUNK_IN)


def _rs_encode_audio(data: bytes) -> bytes:
    GF, RS = _get_rs()
    pad    = (-len(data)) % AUDIO_CHUNK_IN
    padded = data + b'\x00' * pad
    chunks = np.frombuffer(padded, dtype=np.uint8).reshape(-1, AUDIO_CHUNK_IN).astype(int)
    enc    = RS.encode(GF(chunks))
    return np.asarray(enc, dtype=np.uint8).tobytes()


def _rs_decode_audio(data: bytes) -> bytes:
    GF, RS = _get_rs()
    chunk_size = 255
    n = len(data) // chunk_size
    if n == 0:
        return b''
    arr = np.frombuffer(data[:n * chunk_size], dtype=np.uint8).reshape(n, chunk_size).astype(int)
    dec = RS.decode(GF(arr))
    return np.asarray(dec, dtype=np.uint8).tobytes()


# --- M-ary FSK modulation ---------------------------------------------------

def _bits_to_symbols(bits: np.ndarray) -> np.ndarray:
    """Pack a 0/1 bit array into symbol values, MSB first, zero-padded."""
    k = BITS_PER_SYMBOL
    pad = (-len(bits)) % k
    if pad:
        bits = np.concatenate([bits, np.zeros(pad, dtype=np.uint8)])
    g = bits.reshape(-1, k).astype(np.uint16)
    weights = (1 << np.arange(k - 1, -1, -1)).astype(np.uint16)
    return (g * weights).sum(axis=1).astype(np.uint16)


def _symbols_to_bits(syms: np.ndarray, n_bits: int) -> np.ndarray:
    k = BITS_PER_SYMBOL
    out = np.empty((len(syms), k), dtype=np.uint8)
    for i in range(k):
        out[:, i] = (syms >> (k - 1 - i)) & 1
    return out.reshape(-1)[:n_bits]


def _symbols_to_signal(syms: np.ndarray) -> np.ndarray:
    """Phase-continuous M-ary FSK.

    Phase carries across symbol boundaries: a discontinuity there spreads
    energy across the whole band, which wastes power and hands AAC a transient
    to smear.
    """
    sps = SAMPLES_PER_SYMBOL
    t = np.arange(sps, dtype=np.float64) / SAMPLE_RATE
    w = 2.0 * math.pi * np.asarray(tone_freqs(), dtype=np.float64)

    out = np.empty(len(syms) * sps, dtype=np.float32)
    phase = 0.0
    for i, sym in enumerate(syms):
        wi = w[int(sym)]
        out[i * sps:(i + 1) * sps] = np.sin(phase + wi * t) * AMPLITUDE
        phase = (phase + wi * sps / SAMPLE_RATE) % (2.0 * math.pi)
    return out


def _bits_to_signal(bits: np.ndarray) -> np.ndarray:
    """Legacy name: bits in, modulated signal out."""
    return _symbols_to_signal(_bits_to_symbols(np.asarray(bits, dtype=np.uint8)))


def _make_preamble_signal() -> np.ndarray:
    """A steady tone at the lowest constellation tone, then silence.

    The decoder locks on by finding a long run of symbols whose strongest tone
    is tone 0, so the preamble has to use a frequency the demodulator measures.
    """
    n = int(PREAMBLE_SECS * SAMPLE_RATE)
    t = np.arange(n) / SAMPLE_RATE
    tone = (np.sin(2.0 * math.pi * FREQ_BASE * t) * AMPLITUDE).astype(np.float32)
    silence = np.zeros(int(SILENCE_SECS * SAMPLE_RATE), dtype=np.float32)
    return np.concatenate([tone, silence])


# --- Public API --------------------------------------------------------------

def encode_audio(payload: bytes, total_samples: int) -> np.ndarray:
    """Encode payload bytes as an M-ary FSK signal, float32 in [-1, 1]."""
    rs_payload = _rs_encode_audio(payload)
    # Both lengths are needed: the RS length says how many bytes to read off
    # the wire, the payload length says where the zero padding added to fill
    # the last RS chunk begins.
    prefix = struct.pack('<II', len(payload), len(rs_payload))

    # Each section is modulated separately so it starts on a symbol boundary.
    # Packing them as one bit stream breaks whenever BITS_PER_SYMBOL does not
    # divide the section length -- with 3 bits/symbol the 64-bit prefix ends
    # two thirds of the way through a symbol, and the decoder, which reads each
    # section at a symbol offset, then starts the payload in the wrong place.
    content = np.concatenate([
        _make_preamble_signal(),
        _bits_to_signal(np.unpackbits(np.frombuffer(SYNC_WORD, dtype=np.uint8))),
        _bits_to_signal(np.unpackbits(np.frombuffer(prefix, dtype=np.uint8))),
        _bits_to_signal(np.unpackbits(np.frombuffer(rs_payload, dtype=np.uint8))),
    ])

    out = np.zeros(total_samples, dtype=np.float32)
    n = min(len(content), total_samples)
    out[:n] = content[:n]
    return out


def _syms_for_bits(n_bits: int) -> int:
    return -(-n_bits // BITS_PER_SYMBOL)


def max_payload_bytes(total_samples: int) -> int:
    """How many raw (pre-RS) bytes fit in an audio track of this length."""
    overhead = (
        int(PREAMBLE_SECS * SAMPLE_RATE) +
        int(SILENCE_SECS * SAMPLE_RATE) +
        _syms_for_bits(len(SYNC_WORD) * 8) * SAMPLES_PER_SYMBOL +
        _syms_for_bits(8 * 8) * SAMPLES_PER_SYMBOL
    )
    available = total_samples - overhead
    if available <= 0:
        return 0
    rs_bytes = (available // SAMPLES_PER_SYMBOL) * BITS_PER_SYMBOL // 8
    return (rs_bytes // 255) * AUDIO_CHUNK_IN


def float32_to_s16(sig: np.ndarray) -> bytes:
    """Convert float32 [-1,1] to signed 16-bit PCM bytes (little-endian)."""
    s16 = np.clip(sig * 32767, -32768, 32767).astype(np.int16)
    return s16.tobytes()


# --- Demodulator -------------------------------------------------------------

def _tone_energies(chunks: np.ndarray) -> np.ndarray:
    """(n_chunks, sps) -> (n_chunks, M) energy at each tone.

    One complex DFT bin per tone, as a single matrix product rather than a
    Goertzel recurrence per tone.
    """
    n = chunks.shape[1]
    t = np.arange(n, dtype=np.float64)
    freqs = np.asarray(tone_freqs(), dtype=np.float64)
    basis = np.exp(-2j * math.pi * np.outer(freqs, t) / SAMPLE_RATE)
    coeff = chunks.astype(np.float64) @ basis.T
    return np.abs(coeff) ** 2


def _demod_symbols(sig: np.ndarray, n_syms: int) -> np.ndarray:
    sps = SAMPLES_PER_SYMBOL
    avail = len(sig) // sps
    n = min(n_syms, avail)
    if n <= 0:
        return np.zeros(n_syms, dtype=np.uint16)
    e = _tone_energies(sig[:n * sps].reshape(n, sps))
    syms = np.argmax(e, axis=1).astype(np.uint16)
    if n < n_syms:
        syms = np.concatenate([syms, np.zeros(n_syms - n, dtype=np.uint16)])
    return syms


def _demod_bits(sig: np.ndarray, n_bits: int, spb: int = None) -> np.ndarray:
    return _symbols_to_bits(_demod_symbols(sig, _syms_for_bits(n_bits)), n_bits)


def _find_preamble(sig: np.ndarray, spb: int = None) -> int:
    """Sample index where the preamble tone starts, or -1."""
    sps = SAMPLES_PER_SYMBOL
    n_syms = len(sig) // sps
    if n_syms == 0:
        return -1
    e = _tone_energies(sig[:n_syms * sps].reshape(n_syms, sps))
    strongest = np.argmax(e, axis=1)
    total = e.sum(axis=1) + 1e-12
    # Tone 0 must dominate, not merely win: silence has an argmax too.
    is_pre = (strongest == 0) & (e[:, 0] / total > 0.5)

    required = max(1, int(PREAMBLE_SECS * SYMBOL_RATE * 0.6))
    run = 0
    start = 0
    for i in range(n_syms):
        if is_pre[i]:
            if run == 0:
                start = i
            run += 1
            if run >= required:
                return start * sps
        else:
            run = 0
    return -1


def decode_audio(pcm_s16: bytes, sample_rate: int = SAMPLE_RATE) -> bytes:
    """Recover the payload from raw s16le mono PCM, or b"" if not found."""
    sig = np.frombuffer(pcm_s16, dtype=np.int16).astype(np.float32) / 32768.0
    if sample_rate != SAMPLE_RATE:
        ratio = SAMPLE_RATE / sample_rate
        idx = np.clip((np.arange(int(len(sig) * ratio)) / ratio).astype(int),
                      0, len(sig) - 1)
        sig = sig[idx]

    sps = SAMPLES_PER_SYMBOL
    pre = _find_preamble(sig)
    if pre < 0:
        return b""

    pos = pre + int((PREAMBLE_SECS + SILENCE_SECS) * SAMPLE_RATE)

    sync_bits = _demod_bits(sig[pos:], len(SYNC_WORD) * 8)
    if np.packbits(sync_bits).tobytes() != SYNC_WORD:
        return b""
    pos += _syms_for_bits(len(SYNC_WORD) * 8) * sps

    len_bits = _demod_bits(sig[pos:], 64)
    payload_length, rs_length = struct.unpack(
        '<II', np.packbits(len_bits).tobytes())
    pos += _syms_for_bits(64) * sps

    if rs_length == 0 or rs_length > len(sig) // sps * BITS_PER_SYMBOL // 8:
        return b""
    if payload_length > rs_length:
        return b""

    rs_bits = _demod_bits(sig[pos:], rs_length * 8)
    rs_bytes = np.packbits(rs_bits).tobytes()[:rs_length]
    try:
        return _rs_decode_audio(rs_bytes)[:payload_length]
    except Exception:
        return b""

"""
Benchmark for v3 codec (BPP_Y=2, BPP_C=1, 40,140 bytes/frame).

Usage:
    python benchmark_v3.py                  # default 10 MB
    python benchmark_v3.py 50               # 50 MB test
    python benchmark_v3.py 1 5 10 25 50     # multiple sizes
"""
import sys, time, os, math, numpy as np

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from video_codec import (
    BLOCK_SIZE, FRAME_WIDTH, FRAME_HEIGHT, CHROMA_W, CHROMA_H,
    SYNC_ROWS, _params, _encode_plane_np, _decode_plane_np,
    bytes_to_bits, NATIVE_AVAILABLE,
)
if NATIVE_AVAILABLE:
    from native import encode_plane_c, decode_plane_c

# ---------------------------------------------------------------------------
# v3 parameters
# ---------------------------------------------------------------------------
BPP_Y_V3 = 2
BPP_C_V3 = 1

PY = _params(BLOCK_SIZE, FRAME_WIDTH, FRAME_HEIGHT, BPP_Y_V3)
PC = _params(BLOCK_SIZE, CHROMA_W, CHROMA_H, BPP_C_V3)
BPF_Y = PY["bpf"]
BPF_C = PC["bpf"]
BPF = BPF_Y + 2 * BPF_C
BYTES_PER_FRAME = BPF // 8  # 40,140

# ---------------------------------------------------------------------------
# Adjustable file sizes (MB) -- change this or pass via CLI
# ---------------------------------------------------------------------------
DEFAULT_SIZES_MB = [10]

BATCH = 32
TRIALS = 3
WARMUP_FRAMES = 30


def _encode_batch(bits_seg, n, use_c):
    per = bits_seg.reshape(n, BPF)
    yb = per[:, :BPF_Y].reshape(-1)
    cbb = per[:, BPF_Y:BPF_Y + BPF_C].reshape(-1)
    crb = per[:, BPF_Y + BPF_C:].reshape(-1)
    if use_c:
        Y = encode_plane_c(yb, n, PY["ph"], PY["pw"], PY["bs"], PY["bx"], PY["dr"], SYNC_ROWS, PY["bpp"])
        Cb = encode_plane_c(cbb, n, PC["ph"], PC["pw"], PC["bs"], PC["bx"], PC["dr"], SYNC_ROWS, PC["bpp"])
        Cr = encode_plane_c(crb, n, PC["ph"], PC["pw"], PC["bs"], PC["bx"], PC["dr"], SYNC_ROWS, PC["bpp"])
    else:
        Y = _encode_plane_np(yb, PY)
        Cb = _encode_plane_np(cbb, PC)
        Cr = _encode_plane_np(crb, PC)
    return Y, Cb, Cr


def _decode_batch(Y, Cb, Cr, use_c):
    if use_c:
        decode_plane_c(Y, PY["ph"], PY["pw"], PY["bs"], PY["bx"], PY["dr"], SYNC_ROWS, PY["bpp"], PY["m"])
        decode_plane_c(Cb, PC["ph"], PC["pw"], PC["bs"], PC["bx"], PC["dr"], SYNC_ROWS, PC["bpp"], PC["m"])
        decode_plane_c(Cr, PC["ph"], PC["pw"], PC["bs"], PC["bx"], PC["dr"], SYNC_ROWS, PC["bpp"], PC["m"])
    else:
        _decode_plane_np(Y, PY)
        _decode_plane_np(Cb, PC)
        _decode_plane_np(Cr, PC)


def run_benchmark(size_mb, use_c, engine_label):
    size_bytes = int(size_mb * 1_000_000)
    encoded_bytes = size_bytes * 255 // 215  # RS overhead
    total_bits = encoded_bytes * 8
    n_frames = math.ceil(total_bits / BPF)

    rng = np.random.default_rng(42)
    bits = rng.integers(0, 2, size=n_frames * BPF, dtype=np.uint8)

    # Warmup
    wn = min(WARMUP_FRAMES, n_frames)
    Y, Cb, Cr = _encode_batch(bits[:wn * BPF], wn, use_c)
    _decode_batch(Y, Cb, Cr, use_c)

    enc_times = []
    dec_times = []

    for trial in range(TRIALS):
        # Encode
        t0 = time.perf_counter()
        frames_list = []
        for f in range(0, n_frames, BATCH):
            n = min(BATCH, n_frames - f)
            Y, Cb, Cr = _encode_batch(bits[f * BPF:(f + n) * BPF], n, use_c)
            frames_list.append((Y.copy(), Cb.copy(), Cr.copy()))
        t_enc = time.perf_counter() - t0
        enc_times.append(t_enc)

        # Decode
        t0 = time.perf_counter()
        for Y, Cb, Cr in frames_list:
            _decode_batch(Y, Cb, Cr, use_c)
        t_dec = time.perf_counter() - t0
        dec_times.append(t_dec)

        del frames_list

    enc_times.sort()
    dec_times.sort()
    # Use median trial
    t_enc = enc_times[TRIALS // 2]
    t_dec = dec_times[TRIALS // 2]

    enc_mbps = size_mb / t_enc
    dec_mbps = size_mb / t_dec

    print(f"    {engine_label:>12}:  {n_frames:>5} frames  |  "
          f"encode {t_enc:>6.3f}s ({enc_mbps:>6.1f} MB/s)  |  "
          f"decode {t_dec:>6.3f}s ({dec_mbps:>6.1f} MB/s)")

    return dict(size_mb=size_mb, engine=engine_label, n_frames=n_frames,
                enc_time=t_enc, dec_time=t_dec, enc_mbps=enc_mbps, dec_mbps=dec_mbps)


def main():
    sizes = DEFAULT_SIZES_MB
    if len(sys.argv) > 1:
        sizes = [float(x) for x in sys.argv[1:]]

    print("=" * 75)
    print("  v3 Benchmark  (BPP_Y=2, BPP_C=1, 40,140 bytes/frame)")
    print(f"  Native C library: {'YES' if NATIVE_AVAILABLE else 'NO'}")
    print(f"  Trials: {TRIALS} (median reported), batch size: {BATCH}")
    print("=" * 75)

    for sz in sizes:
        print(f"\n  --- {sz} MB ---")
        if NATIVE_AVAILABLE:
            run_benchmark(sz, True, "C native")
        run_benchmark(sz, False, "NumPy")

    print(f"\n{'=' * 75}")
    print("  DONE")
    print("=" * 75)


if __name__ == "__main__":
    main()

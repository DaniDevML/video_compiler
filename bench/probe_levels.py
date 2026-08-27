"""
How many levels per block survive the channel?  (v8)

The format has always been described in bits per block, so the only densities
it could express were powers of two: 2, 4, 8, 16 levels. The real YouTube
probes put the chroma cliff between 4 levels (BER 1.6e-07, essentially clean)
and 8 levels (3.5e-03, unusable). A 20,000x jump across one rung means the
usable maximum lies *inside* that gap, where the bit-based format cannot go.

This sweeps level counts one at a time and reports the symbol error rate, so
the cliff can be located rather than bracketed. Packing bytes into base-N
digits is a separate and lossless concern; what decides the format is whether
a symbol comes back as the symbol that was sent.

    python bench/probe_levels.py local            # transcode simulator
    python bench/probe_levels.py local yt_avc_6m  # a specific simulated rung
    python bench/probe_levels.py upload           # one real YouTube round trip
    python bench/probe_levels.py measure <video_id>

The simulator is a screening tool only. This project's whole history says so:
the v4 format passed every local test and lost 3.6e-02 of its luma bits on the
real service. Nothing here becomes a format decision until `upload` confirms
it.
"""
import json
import math
import os
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import imageio_ffmpeg

import channel as ch

HERE = os.path.dirname(os.path.abspath(__file__))
SCRATCH = os.environ.get(
    'VIDCOMPILER_SCRATCH', 'E:/vid_compiler/benchwork/probe')
os.makedirs(SCRATCH, exist_ok=True)
tempfile.tempdir = SCRATCH

FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
W, H, CW, CH_ = ch.W, ch.H, ch.CW, ch.CH
FRAME_BYTES = W * H + 2 * CW * CH_

SEG_FRAMES = 16
SEED = 20260827

# Each rung varies ONE plane's level count with the other pinned to a setting
# already measured clean on real YouTube, so a failure is attributable.
#
#   luma pin:   2px blocks, 2 levels  -- probe3 measured BER 0.0
#   chroma pin: 2px blocks, 4 levels  -- probe3 measured BER 1.6e-07
LADDER = (
    [('chroma', 2, n, 2, 2) for n in (4, 5, 6, 7, 8)] +
    [('chroma', 4, n, 2, 2) for n in (8, 9, 10, 12)] +
    [('luma', 4, n, 2, 4) for n in (4, 5, 6, 7)] +
    [('luma', 2, n, 2, 4) for n in (2, 3, 4)]
)


def build_segment(which, block, n_levels, pin_block, pin_levels, rng):
    """One segment of SEG_FRAMES frames. Returns (frames, sent_symbols, spec)."""
    if which == 'chroma':
        c = ch.SymSpec(CW, CH_, block, n_levels)
        y = ch.SymSpec(W, H, pin_block, pin_levels)
        csym = rng.integers(0, n_levels, SEG_FRAMES * c.syms * 2, dtype=np.uint8)
        ysym = rng.integers(0, pin_levels, SEG_FRAMES * y.syms, dtype=np.uint8)
        spec, sent = c, csym
    else:
        y = ch.SymSpec(W, H, block, n_levels)
        c = ch.SymSpec(CW, CH_, pin_block, pin_levels)
        ysym = rng.integers(0, n_levels, SEG_FRAMES * y.syms, dtype=np.uint8)
        csym = rng.integers(0, pin_levels, SEG_FRAMES * c.syms * 2, dtype=np.uint8)
        spec, sent = y, ysym

    Y = ch.modulate_sym(ysym, y, SEG_FRAMES)
    half = SEG_FRAMES * c.syms
    Cb = ch.modulate_sym(csym[:half], c, SEG_FRAMES)
    Cr = ch.modulate_sym(csym[half:], c, SEG_FRAMES)

    frames = np.empty((SEG_FRAMES, FRAME_BYTES), dtype=np.uint8)
    frames[:, :W * H] = Y.reshape(SEG_FRAMES, -1)
    frames[:, W * H:W * H + CW * CH_] = Cb.reshape(SEG_FRAMES, -1)
    frames[:, W * H + CW * CH_:] = Cr.reshape(SEG_FRAMES, -1)
    return frames, sent, spec


def read_segment(frames, which, block, n_levels, pin_block, pin_levels):
    n = len(frames)
    if which == 'chroma':
        c = ch.SymSpec(CW, CH_, block, n_levels)
        Cb = frames[:, W * H:W * H + CW * CH_].reshape(n, CH_, CW)
        Cr = frames[:, W * H + CW * CH_:].reshape(n, CH_, CW)
        return np.concatenate([ch.demodulate_sym(Cb, c),
                               ch.demodulate_sym(Cr, c)])
    y = ch.SymSpec(W, H, block, n_levels)
    Y = frames[:, :W * H].reshape(n, H, W)
    return ch.demodulate_sym(Y, y)


def build_ladder():
    rng = np.random.default_rng(SEED)
    all_frames, sent, specs = [], [], []
    for which, block, n, pb, pl in LADDER:
        f, s, sp = build_segment(which, block, n, pb, pl, rng)
        all_frames.append(f)
        sent.append(s)
        specs.append(sp)
    return np.concatenate(all_frames), sent, specs


def measure(frames, sent):
    """Per-rung symbol error rate."""
    rows = []
    for i, (which, block, n, pb, pl) in enumerate(LADDER):
        seg = frames[i * SEG_FRAMES:(i + 1) * SEG_FRAMES]
        if len(seg) < SEG_FRAMES:
            rows.append({'plane': which, 'block': block, 'levels': n,
                         'error': 'segment missing from download'})
            continue
        got = read_segment(seg, which, block, n, pb, pl)
        exp = sent[i]
        k = min(len(got), len(exp))
        bad = int(np.count_nonzero(got[:k] != exp[:k]))
        ser = bad / k if k else 1.0
        rows.append({
            'plane': which, 'block': block, 'levels': n,
            'spacing': round(255.0 / (n - 1), 2) if n > 1 else 255.0,
            'bits_per_sym': round(math.log2(n), 4),
            'symbols': k, 'errors': bad, 'ser': ser,
        })
    return rows


def frame_bytes_for(plane, block, levels):
    """Bytes/frame if this rung were adopted with the other plane pinned."""
    if plane == 'chroma':
        y = ch.SymSpec(W, H, 2, 2)
        c = ch.SymSpec(CW, CH_, block, levels)
    else:
        y = ch.SymSpec(W, H, block, levels)
        c = ch.SymSpec(CW, CH_, 2, 4)
    return int((y.bits + 2 * c.bits) // 8)


def report(rows, title):
    print(f'\n{title}')
    print(f'{"plane":>7} {"block":>5} {"levels":>6} {"spacing":>7} '
          f'{"bits/sym":>8} {"symbols":>10} {"errors":>8} {"SER":>10}  verdict')
    for r in rows:
        if 'error' in r:
            print(f'{r["plane"]:>7} {r["block"]:>5} {r["levels"]:>6}  {r["error"]}')
            continue
        ser = r['ser']
        # RS(255,215) corrects 20 symbols per 255. It runs out of margin long
        # before that on average, so anything past ~1e-4 is treated as failed
        # and anything under 1e-5 as comfortable.
        verdict = 'CLEAN' if ser < 1e-5 else ('marginal' if ser < 1e-4 else 'FAILS')
        print(f'{r["plane"]:>7} {r["block"]:>5} {r["levels"]:>6} '
              f'{r["spacing"]:>7.2f} {r["bits_per_sym"]:>8.3f} '
              f'{r["symbols"]:>10,} {r["errors"]:>8,} {ser:>10.2e}  {verdict}')


def encode_and_simulate(frames, preset):
    src = os.path.join(SCRATCH, 'probe_src.mp4')
    ch.encode_video(frames, src, preset='nvenc_qp44')
    out = os.path.join(SCRATCH, 'probe_yt.mp4')
    params = ch.YT_PRESETS[preset] if hasattr(ch, 'YT_PRESETS') else None
    if params is None:
        raise SystemExit(f'no simulator preset {preset!r}')
    cmd = [FFMPEG, '-y', '-i', src, *params, '-an', out]
    subprocess.run(cmd, **ch.popen_kw(stdout=subprocess.DEVNULL,
                                      stderr=subprocess.DEVNULL), check=True)
    return decode_frames(out, len(frames))


def decode_frames(path, n):
    cmd = [FFMPEG, '-v', 'error', '-i', path, '-f', 'rawvideo',
           '-pix_fmt', 'yuv420p', 'pipe:1']
    p = subprocess.Popen(cmd, **ch.popen_kw(stdout=subprocess.PIPE,
                                            stderr=subprocess.DEVNULL))
    buf = p.stdout.read(n * FRAME_BYTES)
    p.stdout.close()
    p.wait()
    got = len(buf) // FRAME_BYTES
    return np.frombuffer(buf[:got * FRAME_BYTES],
                         dtype=np.uint8).reshape(got, FRAME_BYTES)


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else 'local'
    print(f'Ladder: {len(LADDER)} rungs x {SEG_FRAMES} frames '
          f'= {len(LADDER) * SEG_FRAMES} frames')

    frames, sent, specs = build_ladder()
    print(f'Built {len(frames)} frames ({frames.nbytes / 1e9:.2f} GB raw)')

    if mode == 'local':
        preset = sys.argv[2] if len(sys.argv) > 2 else 'yt_avc_8m'
        rows = measure(encode_and_simulate(frames, preset), sent)
        report(rows, f'Simulated ({preset}) -- SCREENING ONLY, not a decision')
        json.dump({'mode': 'local', 'preset': preset, 'rows': rows},
                  open(os.path.join(HERE, 'levels_probe_local.json'), 'w'),
                  indent=1)

    elif mode == 'upload':
        import youtube_api as yt
        src = os.path.join(SCRATCH, 'levels_probe.mp4')
        size = ch.encode_video(frames, src, preset='nvenc_qp44')
        print(f'Encoded {size / 1e6:.1f} MB; uploading...')
        vid, url = yt.upload_video(src, title='vidcompiler level probe (v8)',
                                   progress=lambda m: print(f'  {m}', flush=True))
        print(f'Uploaded {url}')
        print('Wait for YouTube to finish the 1080p rendition, then:')
        print(f'    python bench/probe_levels.py measure {vid}')
        json.dump({'video_id': vid, 'url': url},
                  open(os.path.join(HERE, 'levels_probe_pending.json'), 'w'),
                  indent=1)

    elif mode == 'measure':
        import youtube_api as yt
        vid = sys.argv[2]
        dl = os.path.join(SCRATCH, f'{vid}.mp4')
        if not os.path.exists(dl):
            yt.download_video(f'https://www.youtube.com/watch?v={vid}', dl,
                              progress=lambda m: print(f'  {m}', flush=True))
        rows = measure(decode_frames(dl, len(frames)), sent)
        report(rows, f'REAL YouTube ({vid})')
        for r in rows:
            if 'error' not in r:
                r['bytes_per_frame_if_adopted'] = frame_bytes_for(
                    r['plane'], r['block'], r['levels'])
        json.dump({'video_id': vid, 'rows': rows},
                  open(os.path.join(HERE, 'levels_probe_youtube.json'), 'w'),
                  indent=1)
        print(f'\nWrote bench/levels_probe_youtube.json')
    else:
        raise SystemExit(__doc__)


if __name__ == '__main__':
    main()

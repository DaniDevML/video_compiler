"""
Measure the real YouTube channel: which formats actually survive?

The 8 MB round trip failed, so the question is no longer "is the pipeline
fast" but "what density does this channel support". One upload answers it:
the test video is built from consecutive segments, each modulated at a
different density, so a single round trip measures the whole capacity ladder
against the real service instead of a simulation.

Per-plane error rates are reported separately, because luma and chroma are not
equally damaged and the fix depends on which one fails.

Usage:
    python bench/diag_youtube_formats.py            # upload a new test video
    python bench/diag_youtube_formats.py <video_id> # re-measure an existing one
"""
import json
import os
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import imageio_ffmpeg

from channel import Format, W, H, CW, CH

HERE = os.path.dirname(os.path.abspath(__file__))
SCRATCH = os.environ.get(
    'VIDCOMPILER_SCRATCH',
    os.path.join(os.path.dirname(os.path.dirname(HERE)), 'bench_scratch'))
os.makedirs(SCRATCH, exist_ok=True)
tempfile.tempdir = SCRATCH
os.environ['TMP'] = os.environ['TEMP'] = os.environ['TMPDIR'] = SCRATCH

FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
FRAME_BYTES = W * H + 2 * CW * CH
SEG_FRAMES = 12
SEED = 20260822

# Probe 1 established the shape of the channel: chroma came back bit-perfect at
# every density tried, while luma broke between 2 and 3 bits per block. Probe 2
# follows that finding -- luma pinned at the last setting that survived, with
# the extra capacity pushed into chroma instead.
LADDERS = {
    'probe1': [
        ('Y 8px/1bpp  no chroma', Format(8, 1, 8, 1, use_chroma=False)),
        ('Y 8px/2bpp + C 8px/1bpp', Format(8, 2, 8, 1)),
        ('Y 8px/3bpp + C 8px/2bpp', Format(8, 3, 8, 2)),
        ('Y 4px/1bpp  no chroma', Format(4, 1, 4, 1, use_chroma=False)),
        ('Y 4px/1bpp + C 4px/1bpp', Format(4, 1, 4, 1)),
        ('Y 4px/2bpp  no chroma', Format(4, 2, 4, 1, use_chroma=False)),
        ('Y 4px/2bpp + C 4px/1bpp', Format(4, 2, 4, 1)),
        ('Y 4px/2bpp + C 4px/2bpp', Format(4, 2, 4, 2)),
        ('Y 4px/3bpp + C 4px/2bpp', Format(4, 3, 4, 2)),   # current v4/v5
        ('Y 4px/4bpp + C 4px/3bpp', Format(4, 4, 4, 3)),
    ],
    'probe2': [
        ('Y 4px/2bpp + C 4px/2bpp', Format(4, 2, 4, 2)),   # known-good control
        ('Y 4px/2bpp + C 4px/3bpp', Format(4, 2, 4, 3)),
        ('Y 4px/2bpp + C 4px/4bpp', Format(4, 2, 4, 4)),
        ('Y 4px/2bpp + C 4px/5bpp', Format(4, 2, 4, 5)),
        ('Y 4px/2bpp + C 2px/2bpp', Format(4, 2, 2, 2)),
        ('Y 4px/2bpp + C 2px/3bpp', Format(4, 2, 2, 3)),
        ('Y 4px/2bpp + C 2px/4bpp', Format(4, 2, 2, 4)),
        ('Y 2px/1bpp + C 4px/3bpp', Format(2, 1, 4, 3)),
        ('Y 2px/2bpp + C 4px/3bpp', Format(2, 2, 4, 3)),
        ('Y 8px/2bpp + C 2px/3bpp', Format(8, 2, 2, 3)),
    ],
    # Probe 2 found the two best halves separately: luma at 2px/1bpp came back
    # bit-perfect, and chroma at 2px/2bpp was near-perfect. Probe 3 combines
    # them and pushes one step past, with the control repeated at both ends of
    # the video to check the result is not an artefact of position in the
    # stream (encoder rate control varies along a clip).
    'probe3': [
        ('Y 4px/2bpp + C 2px/2bpp', Format(4, 2, 2, 2)),   # control, head
        ('Y 2px/1bpp  no chroma', Format(2, 1, 2, 2, use_chroma=False)),
        ('Y 4px/1bpp + C 2px/2bpp', Format(4, 1, 2, 2)),
        ('Y 2px/1bpp + C 4px/4bpp', Format(2, 1, 4, 4)),
        ('Y 2px/1bpp + C 2px/2bpp', Format(2, 1, 2, 2)),   # the candidate
        ('Y 2px/1bpp + C 2px/3bpp', Format(2, 1, 2, 3)),   # stretch
        ('Y 2px/1bpp + C 2px/2bpp*', Format(2, 1, 2, 2)),  # candidate, repeat
        ('Y 4px/2bpp + C 2px/2bpp*', Format(4, 2, 2, 2)),  # control, tail
    ],
}
LADDER = LADDERS['probe1']


def popen_kw(**extra):
    kw = dict(**extra)
    if sys.platform == 'win32':
        kw['creationflags'] = subprocess.CREATE_NO_WINDOW
    return kw


def build_segments(ladder):
    """Deterministic bits for every segment, plus the frames to encode."""
    rng = np.random.default_rng(SEED)
    segs = []
    for label, fmt in ladder:
        bits = rng.integers(0, 2, size=SEG_FRAMES * fmt.bits, dtype=np.uint8)
        segs.append(dict(label=label, fmt=fmt, bits=bits))
    return segs


def encode_test_video(segs, path):
    """One video: a leading marker frame, then each segment back to back."""
    cmd = [FFMPEG, '-f', 'rawvideo', '-pix_fmt', 'yuv420p', '-s', f'{W}x{H}',
           '-r', '30', '-i', 'pipe:0', '-c:v', 'h264_nvenc',
           '-preset', 'llhp', '-rc', 'constqp', '-qp', '18',
           '-g', '1', '-bf', '0', '-pix_fmt', 'yuv420p', '-an', '-y', path]
    p = subprocess.Popen(cmd, **popen_kw(stdin=subprocess.PIPE,
                                         stdout=subprocess.DEVNULL,
                                         stderr=subprocess.DEVNULL))
    # Marker: mid-grey frame, so segment 0 never starts at frame 0 and any
    # leading-frame trimming by YouTube is visible.
    marker = np.full(FRAME_BYTES, 128, dtype=np.uint8)
    p.stdin.write(marker.tobytes())
    for s in segs:
        p.stdin.write(s['fmt'].encode(s['bits'], SEG_FRAMES).tobytes())
    p.stdin.close()
    p.wait()
    return os.path.getsize(path)


def decode_frames(path):
    tmp = tempfile.mktemp(suffix='.yuv')
    subprocess.run([FFMPEG, '-i', path, '-f', 'rawvideo', '-pix_fmt',
                    'yuv420p', '-y', tmp],
                   **popen_kw(stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL))
    n = os.path.getsize(tmp) // FRAME_BYTES
    raw = np.fromfile(tmp, dtype=np.uint8, count=n * FRAME_BYTES)
    os.unlink(tmp)
    return raw.reshape(n, FRAME_BYTES)


def plane_errors(fmt, frames, want_bits):
    """Bit error rate overall and per plane for one segment."""
    n = len(frames)
    got = fmt.decode(frames)
    want = want_bits[:len(got)]
    overall = float(np.count_nonzero(got != want)) / len(want)

    per = {}
    wf = want.reshape(n, fmt.bits)
    gf = got.reshape(n, fmt.bits)
    yb = fmt.y.bits
    per['Y'] = float(np.count_nonzero(gf[:, :yb] != wf[:, :yb])) / (n * yb)
    if fmt.use_chroma:
        cb = fmt.c.bits
        per['Cb'] = float(np.count_nonzero(
            gf[:, yb:yb + cb] != wf[:, yb:yb + cb])) / (n * cb)
        per['Cr'] = float(np.count_nonzero(
            gf[:, yb + cb:] != wf[:, yb + cb:])) / (n * cb)
    return overall, per


def find_offset(segs, frames):
    """Locate segment 0 by trying small frame offsets and taking the best."""
    best, best_ber = 0, 1.0
    fmt = segs[0]['fmt']
    for off in range(0, min(6, max(1, len(frames) - SEG_FRAMES))):
        try:
            ber, _ = plane_errors(fmt, frames[off:off + SEG_FRAMES],
                                  segs[0]['bits'])
        except Exception:
            continue
        if ber < best_ber:
            best, best_ber = off, ber
    return best, best_ber


def measure(video_path, segs, tag):
    frames = decode_frames(video_path)
    need = 1 + SEG_FRAMES * len(segs)
    print(f'\n{tag}: {len(frames)} frames decoded (expected {need})')
    if len(frames) < need:
        print('  fewer frames than expected; results may be misaligned')

    off, off_ber = find_offset(segs, frames)
    print(f'  segment 0 found at frame offset {off} (ber {off_ber:.2e})\n')

    print(f'  {"format":<26} {"B/frame":>8} {"overall":>10} '
          f'{"Y":>10} {"Cb":>10} {"Cr":>10}')
    print('  ' + '-' * 78)
    rows = []
    for i, s in enumerate(segs):
        a = off + i * SEG_FRAMES
        chunk = frames[a:a + SEG_FRAMES]
        if len(chunk) < SEG_FRAMES:
            print(f'  {s["label"]:<26} truncated')
            continue
        ber, per = plane_errors(s['fmt'], chunk, s['bits'])
        rows.append(dict(label=s['label'], bytes=s['fmt'].bytes,
                         ber=ber, per=per))
        cb = f'{per.get("Cb"):>10.2e}' if 'Cb' in per else f'{"-":>10}'
        cr = f'{per.get("Cr"):>10.2e}' if 'Cr' in per else f'{"-":>10}'
        print(f'  {s["label"]:<26} {s["fmt"].bytes:>8,} {ber:>10.2e} '
              f'{per["Y"]:>10.2e} {cb} {cr}')
    return rows


def main():
    args = sys.argv[1:]
    ladder_name = 'probe1'
    for a in list(args):
        if a in LADDERS:
            ladder_name = a
            args.remove(a)
    ladder = LADDERS[ladder_name]

    segs = build_segments(ladder)
    total_frames = 1 + SEG_FRAMES * len(segs)
    print(f'ladder: {ladder_name}')
    print(f'{len(segs)} formats x {SEG_FRAMES} frames = {total_frames} frames '
          f'({total_frames/30:.1f}s)\n')

    if args:
        video_id = args[0]
        url = f'https://www.youtube.com/watch?v={video_id}'
        print(f'Re-measuring existing video {url}')
    else:
        local = os.path.join(SCRATCH, f'fmt_test_{ladder_name}.mp4')
        size = encode_test_video(segs, local)
        print(f'encoded test video: {size/1e6:.2f} MB')

        print('\n--- local baseline (our own encoder, no YouTube) ---')
        measure(local, segs, 'local')

        from youtube_api import upload_video
        t0 = time.perf_counter()
        video_id, url = upload_video(
            local, title=f'vidcompiler format {ladder_name} '
                         f'{time.strftime("%Y%m%d-%H%M%S")}',
            progress=lambda m: print(f'  {m}', flush=True))
        print(f'uploaded in {time.perf_counter()-t0:.0f}s -> {url}')

    from bench_youtube import wait_for_1080p
    wait_for_1080p(video_id)

    from youtube_api import download_video
    dl = os.path.join(SCRATCH, f'fmt_test_dl_{ladder_name}.mp4')
    download_video(url, dl, progress=None)
    print(f'downloaded {os.path.getsize(dl)/1e6:.2f} MB')

    print('\n--- after real YouTube transcode ---')
    rows = measure(dl, segs, 'youtube')

    out = os.path.join(HERE, f'youtube_format_{ladder_name}.json')
    with open(out, 'w') as f:
        json.dump({'video_id': video_id, 'url': url, 'ladder': ladder_name,
                   'rows': rows}, f, indent=2)
    print(f'\nresults written to {out}')
    print('\nRS(255,215) corrects ~7.8% byte errors; a bit error rate under '
          '~1e-3 is comfortably correctable.')


if __name__ == '__main__':
    main()

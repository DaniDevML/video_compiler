"""
Among the formats that survive YouTube, which is cheapest to upload?

Density is only half the story. A format that doubles the payload per frame but
triples the coded size of each frame is a loss, because upload and download time
tracks total bytes on the wire, not frames.

For each candidate profile this measures the highest quantiser that still
round-trips locally with zero errors, and the resulting uploaded bytes per
payload byte.

Usage: python bench/bench_profile_size.py [n_frames]
"""
import os
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import imageio_ffmpeg

import video_codec as vc

FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()

# Every profile here was measured surviving real YouTube in
# bench/diag_youtube_formats.py, with its measured bit error rate noted.
CANDIDATES = [
    ('Y4/2 + C4/2', vc.Profile(4, 2, 4, 2), '1.5e-05'),
    ('Y4/2 + C4/3', vc.Profile(4, 2, 4, 3), '1.1e-05'),
    ('Y4/1 + C2/2', vc.Profile(4, 1, 2, 2), '6.5e-07'),
    ('Y4/2 + C2/2', vc.Profile(4, 2, 2, 2), '4.3e-07'),
    ('Y2/1 + C4/3', vc.Profile(2, 1, 4, 3), '2.4e-07'),
    ('Y2/1 + C2/2', vc.Profile(2, 1, 2, 2), '1.6e-07'),
]


def kw():
    k = dict(stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
             stderr=subprocess.DEVNULL)
    if sys.platform == 'win32':
        k['creationflags'] = subprocess.CREATE_NO_WINDOW
    return k


def encode(frames, path, qp):
    cmd = [FFMPEG, '-f', 'rawvideo', '-pix_fmt', 'yuv420p',
           '-s', f'{vc.FRAME_WIDTH}x{vc.FRAME_HEIGHT}', '-r', str(vc.FPS),
           '-i', 'pipe:0', '-c:v', 'h264_nvenc', '-preset', 'llhp',
           '-rc', 'constqp', '-qp', str(qp), '-g', '1', '-bf', '0',
           '-pix_fmt', 'yuv420p', '-an', '-y', path]
    p = subprocess.Popen(cmd, **kw())
    p.stdin.write(frames.tobytes())
    p.stdin.close()
    p.wait()
    return os.path.getsize(path)


def decode(path, n, profile):
    tmp = tempfile.mktemp(suffix='.yuv')
    k = dict(stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if sys.platform == 'win32':
        k['creationflags'] = subprocess.CREATE_NO_WINDOW
    subprocess.run([FFMPEG, '-i', path, '-f', 'rawvideo', '-pix_fmt',
                    'yuv420p', '-y', tmp], **k)
    got = os.path.getsize(tmp) // vc.YUV_FRAME_BYTES
    arr = np.fromfile(tmp, dtype=np.uint8, count=got * vc.YUV_FRAME_BYTES)
    os.unlink(tmp)
    if got < n:
        return None
    return vc.yuv_frames_to_packed(
        arr.reshape(got, vc.YUV_FRAME_BYTES)[:n], profile)


def main(n=12):
    rng = np.random.default_rng(31)
    print(f'{n} frames per measurement, nvenc llhp\n')
    print(f'{"profile":<14} {"B/frame":>9} {"QP":>4} {"MB/frame":>10} '
          f'{"expansion":>10} {"rel. upload":>12}  YouTube BER')
    print('-' * 82)

    rows = []
    for label, prof, ber in CANDIDATES:
        payload = np.packbits(
            rng.integers(0, 2, size=n * prof.bits, dtype=np.uint8))
        src = vc.pad_for_packed(payload)
        frames = vc.packed_to_yuv_frames(src, n, 0, prof)

        best = None
        for qp in range(24, 52, 2):
            path = tempfile.mktemp(suffix='.mp4')
            try:
                size = encode(frames, path, qp)
                got = decode(path, n, prof)
                if got is None:
                    continue
                if np.array_equal(got, payload):
                    exp = size / (n * prof.bytes)
                    if best is None or exp < best[1]:
                        best = (qp, exp, size)
            finally:
                if os.path.exists(path):
                    os.unlink(path)
        if best is None:
            print(f'{label:<14} {prof.bytes:>9,}   no clean QP found')
            continue
        qp, exp, size = best
        rows.append((label, prof, qp, exp, ber))
        print(f'{label:<14} {prof.bytes:>9,} {qp:>4} {size/n/1e6:>10.3f} '
              f'{exp:>9.2f}x {"":>12}  {ber}')

    if rows:
        cheapest = min(rows, key=lambda r: r[3])
        print(f'\nCheapest upload per payload byte: {cheapest[0]} at '
              f'{cheapest[3]:.2f}x (qp {cheapest[2]}, '
              f'{cheapest[1].bytes:,} B/frame)')
        print('\nrelative upload cost, normalised to the cheapest:')
        for label, prof, qp, exp, ber in sorted(rows, key=lambda r: r[3]):
            print(f'  {label:<14} {exp/cheapest[3]:>5.2f}x   '
                  f'{prof.bytes:>9,} B/frame   qp {qp}')


if __name__ == '__main__':
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 12)

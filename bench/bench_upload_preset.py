"""
Pick the upload encoder preset: speed, size and correctness together.

A preset is only usable if the video round-trips with zero bit errors through
our own decoder. This measures encode fps, uploaded size and BER for each
candidate so the choice is made on all three at once.
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
import video_decoder as vd

FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()

CANDIDATES = []
for preset in ('default', 'llhp', 'll', 'llhq', 'hp'):
    for qp in (28, 34, 40, 44, 47, 50):
        args = ['-rc', 'constqp', '-qp', str(qp), '-g', '1', '-bf', '0']
        if preset != 'default':
            args = ['-preset', preset] + args
        CANDIDATES.append((f'nvenc {preset} qp{qp}', 'h264_nvenc', args))


def kw():
    k = dict(stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
             stderr=subprocess.DEVNULL)
    if sys.platform == 'win32':
        k['creationflags'] = subprocess.CREATE_NO_WINDOW
    return k


def main(n_frames=48):
    rng = np.random.default_rng(11)
    bits = rng.integers(0, 2, size=n_frames * vc.BITS_PER_FRAME, dtype=np.uint8)
    print(f'Generating {n_frames} frames...')
    frames = vc.bits_to_yuv_frames(bits)
    raw = frames.tobytes()
    payload = n_frames * vc.BYTES_PER_FRAME
    print(f'{payload/1e6:.2f} MB payload, format {vc.BYTES_PER_FRAME:,} B/frame\n')

    print(f'{"preset":<20} {"fps":>7} {"out MB":>8} {"expansion":>10} {"BER":>11}')
    print('-' * 62)

    best = None
    for label, codec, params in CANDIDATES:
        out = tempfile.mktemp(suffix='.mp4')
        cmd = [FFMPEG, '-f', 'rawvideo', '-pix_fmt', 'yuv420p',
               '-s', f'{vc.FRAME_WIDTH}x{vc.FRAME_HEIGHT}', '-r', str(vc.FPS),
               '-i', 'pipe:0', '-c:v', codec, *params,
               '-pix_fmt', 'yuv420p', '-an', '-y', out]
        try:
            t0 = time.perf_counter()
            p = subprocess.Popen(cmd, **kw())
            try:
                p.stdin.write(raw)
                p.stdin.close()
            except (BrokenPipeError, OSError):
                p.wait()
                print(f'{label:<20}   unsupported')
                continue
            p.wait()
            dt = time.perf_counter() - t0
            if p.returncode != 0 or not os.path.exists(out):
                print(f'{label:<20}   unsupported')
                continue

            size = os.path.getsize(out)
            got = _decode(out, n_frames)
            if got is None:
                print(f'{label:<20}   decode failed')
                continue
            ber = float(np.count_nonzero(got != bits)) / len(bits)
            print(f'{label:<20} {n_frames/dt:>7.1f} {size/1e6:>8.2f} '
                  f'{size/payload:>9.2f}x {ber:>11.2e}')
            if ber == 0 and (best is None or size < best[1]):
                best = (label, size, n_frames / dt, size / payload)
        finally:
            if os.path.exists(out):
                os.unlink(out)

    if best:
        print(f'\nSmallest clean upload: {best[0]} '
              f'-> {best[3]:.2f}x expansion at {best[2]:.0f} fps')


def _decode(path, n_frames):
    tmp = tempfile.mktemp(suffix='.yuv')
    k = dict(stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if sys.platform == 'win32':
        k['creationflags'] = subprocess.CREATE_NO_WINDOW
    subprocess.run([FFMPEG, '-i', path, '-f', 'rawvideo', '-pix_fmt',
                    'yuv420p', '-y', tmp], **k)
    if not os.path.exists(tmp):
        return None
    n = os.path.getsize(tmp) // vc.YUV_FRAME_BYTES
    arr = np.fromfile(tmp, dtype=np.uint8, count=n * vc.YUV_FRAME_BYTES)
    os.unlink(tmp)
    if n < n_frames:
        return None
    arr = arr.reshape(n, vc.YUV_FRAME_BYTES)[:n_frames]
    return vc.yuv_frames_to_bits(arr).reshape(-1)


if __name__ == '__main__':
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 48)

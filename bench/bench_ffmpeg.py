"""
ffmpeg encode benchmark.

With the pixel engine an order of magnitude faster, ffmpeg dominates encode
time, so this measures raw encoder throughput and output size for candidate
settings. Frames are generated once up front and reused, so the numbers are
the encoder's alone.

Usage: python bench/bench_ffmpeg.py [n_frames]
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

# The p1..p7 preset names need a newer NVENC API than Pascal-era drivers
# expose, so the legacy names (fast / hp / ll / llhp) are used here.
CANDIDATES = [
    ('nvenc qp18 g1 (v4 default)',
     'h264_nvenc', ['-rc', 'constqp', '-qp', '18', '-g', '1', '-bf', '0']),
    ('nvenc qp18 g1 fast',
     'h264_nvenc', ['-preset', 'fast', '-rc', 'constqp', '-qp', '18',
                    '-g', '1', '-bf', '0']),
    ('nvenc qp32 g1 fast',
     'h264_nvenc', ['-preset', 'fast', '-rc', 'constqp', '-qp', '32',
                    '-g', '1', '-bf', '0']),
    ('nvenc qp40 g1 fast',
     'h264_nvenc', ['-preset', 'fast', '-rc', 'constqp', '-qp', '40',
                    '-g', '1', '-bf', '0']),
    ('nvenc qp40 g1 hp',
     'h264_nvenc', ['-preset', 'hp', '-rc', 'constqp', '-qp', '40',
                    '-g', '1', '-bf', '0']),
    ('nvenc qp40 g1 llhp',
     'h264_nvenc', ['-preset', 'llhp', '-rc', 'constqp', '-qp', '40',
                    '-g', '1', '-bf', '0']),
    ('nvenc qp40 g30 fast',
     'h264_nvenc', ['-preset', 'fast', '-rc', 'constqp', '-qp', '40',
                    '-g', '30', '-bf', '0']),
    ('nvenc qp46 g1 fast',
     'h264_nvenc', ['-preset', 'fast', '-rc', 'constqp', '-qp', '46',
                    '-g', '1', '-bf', '0']),
    ('x264 qp30 ultrafast g1',
     'libx264', ['-qp', '30', '-preset', 'ultrafast', '-g', '1',
                 '-aq-mode', '0', '-threads', '0']),
]


def main(n_frames=60):
    rng = np.random.default_rng(11)
    bits = rng.integers(0, 2, size=n_frames * vc.BITS_PER_FRAME, dtype=np.uint8)
    print(f'Generating {n_frames} frames...')
    frames = vc.bits_to_yuv_frames(bits)
    raw = frames.tobytes()
    payload = n_frames * vc.BYTES_PER_FRAME
    print(f'{len(raw)/1e6:.0f} MB of raw YUV, {payload/1e6:.2f} MB payload\n')

    print(f'{"settings":<30} {"fps":>7} {"MB/s payload":>13} '
          f'{"out MB":>8} {"expansion":>10}')
    print('-' * 72)

    for label, codec, params in CANDIDATES:
        out = tempfile.mktemp(suffix='.mp4')
        cmd = [FFMPEG, '-f', 'rawvideo', '-pix_fmt', 'yuv420p',
               '-s', f'{vc.FRAME_WIDTH}x{vc.FRAME_HEIGHT}', '-r', str(vc.FPS),
               '-i', 'pipe:0', '-c:v', codec, *params,
               '-pix_fmt', 'yuv420p', '-an', '-y', out]
        kw = dict(stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                  stderr=subprocess.DEVNULL)
        if sys.platform == 'win32':
            kw['creationflags'] = subprocess.CREATE_NO_WINDOW
        try:
            t0 = time.perf_counter()
            p = subprocess.Popen(cmd, **kw)
            try:
                p.stdin.write(raw)
                p.stdin.close()
            except (BrokenPipeError, OSError):
                # ffmpeg rejected the settings and exited before reading input
                p.wait()
                print(f'{label:<30}   unsupported by this build/GPU')
                continue
            p.wait()
            dt = time.perf_counter() - t0
            if p.returncode != 0 or not os.path.exists(out):
                print(f'{label:<30}   unsupported by this build/GPU')
                continue
            size = os.path.getsize(out)
            print(f'{label:<30} {n_frames/dt:>7.1f} {payload/dt/1e6:>10.2f} MB/s '
                  f'{size/1e6:>8.2f} {size/payload:>9.2f}x')
        finally:
            if os.path.exists(out):
                os.unlink(out)


if __name__ == '__main__':
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 60)

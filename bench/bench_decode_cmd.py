"""
Which ffmpeg decode invocation is fastest?

The v4 command always inserted a scale filter and always tried CUDA hwaccel.
Neither is obviously right: the video is already at the target resolution, so
the filter is a full-frame no-op, and hwaccel has to copy frames back over PCIe
to reach system memory. This measures the alternatives.
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
import video_encoder as ve

FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
W, H = vc.FRAME_WIDTH, vc.FRAME_HEIGHT


def kw():
    k = dict(stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    if sys.platform == 'win32':
        k['creationflags'] = subprocess.CREATE_NO_WINDOW
    return k


VARIANTS = [
    ('v4: cuda + scale filter',
     lambda v: [FFMPEG, '-hwaccel', 'cuda', '-i', v,
                '-vf', f'scale={W}:{H}:flags=neighbor',
                '-f', 'rawvideo', '-pix_fmt', 'yuv420p', 'pipe:1']),
    ('cuda, no filter',
     lambda v: [FFMPEG, '-hwaccel', 'cuda', '-i', v,
                '-f', 'rawvideo', '-pix_fmt', 'yuv420p', 'pipe:1']),
    ('software -threads 0 + scale filter',
     lambda v: [FFMPEG, '-threads', '0', '-i', v,
                '-vf', f'scale={W}:{H}:flags=neighbor',
                '-f', 'rawvideo', '-pix_fmt', 'yuv420p', 'pipe:1']),
    ('software -threads 0, no filter',
     lambda v: [FFMPEG, '-threads', '0', '-i', v,
                '-f', 'rawvideo', '-pix_fmt', 'yuv420p', 'pipe:1']),
]


def main(mb=8.0):
    rng = np.random.default_rng(1234)
    payload = rng.integers(0, 256, int(mb * 1024 * 1024), dtype=np.uint8).tobytes()
    work = tempfile.mkdtemp()
    src = os.path.join(work, 'p.bin')
    with open(src, 'wb') as f:
        f.write(payload)
    ve._get_encoder()
    video = os.path.join(work, 'v.mp4')
    ve.encode_files_to_video([src], video, progress=None)
    print(f'video {os.path.getsize(video)/1e6:.2f} MB\n')

    print(f'{"decode command":<38} {"time":>8} {"fps":>7} {"MB/s raw":>10}')
    print('-' * 66)
    for label, build in VARIANTS:
        t0 = time.perf_counter()
        p = subprocess.Popen(build(video), **kw())
        total = 0
        buf = bytearray(1 << 22)
        mv = memoryview(buf)
        while True:
            n = p.stdout.readinto(mv)
            if not n:
                break
            total += n
        p.wait()
        dt = time.perf_counter() - t0
        frames = total // vc.YUV_FRAME_BYTES
        if frames == 0:
            print(f'{label:<38}   failed')
            continue
        print(f'{label:<38} {dt:>7.2f}s {frames/dt:>7.0f} {total/dt/1e6:>9.0f}')

    import shutil
    shutil.rmtree(work, ignore_errors=True)


if __name__ == '__main__':
    main(float(sys.argv[1]) if len(sys.argv) > 1 else 8.0)

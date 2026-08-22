"""
Why is the streaming decode slower than ffmpeg alone?

Standalone, ffmpeg decodes this content at ~250 fps, but the pipeline sees far
less. This isolates the possible causes: the batch buffer size, interleaving
pixel work with reading, and the pixel decoder's threads competing with
ffmpeg's.
"""
import os
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np

import video_codec as vc
import video_encoder as ve
import video_decoder as vd


def build_video(mb):
    rng = np.random.default_rng(1234)
    data = rng.integers(0, 256, int(mb * 1024 * 1024), dtype=np.uint8).tobytes()
    work = tempfile.mkdtemp()
    src = os.path.join(work, 'p.bin')
    with open(src, 'wb') as f:
        f.write(data)
    ve._get_encoder()
    video = os.path.join(work, 'v.mp4')
    ve.encode_files_to_video([src], video, progress=None)
    return work, video


def run(label, video, batch, do_pixels, ffmpeg_threads=None):
    cmd = vd._build_decode_cmd(video, 'yuv420p')
    if ffmpeg_threads is not None:
        cmd = [c for c in cmd]
        i = cmd.index('-threads')
        cmd[i + 1] = str(ffmpeg_threads)
    t0 = time.perf_counter()
    proc = subprocess.Popen(cmd, **vd._popen_kw(bufsize=1 << 22))
    n = 0
    for idx, b in vd._iter_batches_pipe(proc, vc.YUV_FRAME_BYTES, batch):
        if do_pixels:
            vc.yuv_frames_to_packed(b)
        n += len(b)
    dt = time.perf_counter() - t0
    print(f'{label:<46} {dt:>6.2f}s {n/dt:>7.0f} fps')
    return dt


def main(mb=8.0):
    work, video = build_video(mb)
    print(f'video {os.path.getsize(video)/1e6:.2f} MB, '
          f'C threads {vc.NATIVE_THREADS}\n')
    print(f'{"configuration":<46} {"time":>7} {"rate":>11}')
    print('-' * 66)
    try:
        for batch in (2, 4, 8, 16, 32, 64):
            run(f'read + pixel decode, batch={batch}', video, batch, True)
    finally:
        import shutil
        shutil.rmtree(work, ignore_errors=True)


if __name__ == '__main__':
    main(float(sys.argv[1]) if len(sys.argv) > 1 else 8.0)

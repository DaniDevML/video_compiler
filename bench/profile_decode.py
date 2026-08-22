"""
Stage-by-stage decode profile, to locate where decode time actually goes.

Usage: python bench/profile_decode.py [size_mb]
"""
import os
import subprocess
import sys
import tempfile
import time
import zlib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np

import video_codec as vc
import video_encoder as ve
import video_decoder as vd


def main(mb=8.0):
    rng = np.random.default_rng(1234)
    payload = rng.integers(0, 256, int(mb * 1024 * 1024), dtype=np.uint8).tobytes()
    work = tempfile.mkdtemp()
    src = os.path.join(work, 'payload.bin')
    with open(src, 'wb') as f:
        f.write(payload)

    ve.rs_encode(b'\x00' * 4096)
    ve._get_encoder()
    video = os.path.join(work, 'v.mp4')
    ve.encode_files_to_video([src], video, progress=None)
    print(f'video {os.path.getsize(video)/1e6:.2f} MB\n')

    marks = []
    t0 = time.perf_counter()
    last = [t0]

    def mark(name):
        now = time.perf_counter()
        marks.append((name, now - last[0]))
        last[0] = now

    w, h = vd.probe_video(video)
    mark('probe_video (resolution)')

    version, header, bs = vd._detect_version(video, lambda m: None)
    mark(f'_detect_version (first {vd._DETECT_FRAMES} frames)')

    # main streaming pass, split into pipe-read vs pixel-decode
    proc = vd._open_stream(video, 'yuv420p')
    read_s = pix_s = 0.0
    chunks = []
    n = 0
    it = vd._iter_batches_pipe(proc, vc.YUV_FRAME_BYTES, vd.DECODE_BATCH)
    while True:
        t = time.perf_counter()
        try:
            idx, batch = next(it)
        except StopIteration:
            break
        read_s += time.perf_counter() - t
        if idx == 0:
            batch = batch[1:]        # frame 0 is the header, not payload
        t = time.perf_counter()
        if len(batch):
            chunks.append(vc.yuv_frames_to_packed(batch))
        pix_s += time.perf_counter() - t
        n += len(batch)
    marks.append(('  ffmpeg decode + pipe read', read_s))
    marks.append(('  pixel decode -> packed bytes', pix_s))
    last[0] = time.perf_counter()

    enc = np.concatenate(chunks).tobytes()[:header['encoded_size']]
    mark('concatenate')

    arch = vd._rs_strip_parity(enc)[:header['archive_size']]
    crc_ok = (zlib.crc32(arch) & 0xFFFFFFFF) == header['crc32']
    mark('RS parity strip + CRC')

    total = last[0] - t0
    print(f'{"stage":<42} {"time":>8}   share')
    print('-' * 62)
    for name, dt in marks:
        print(f'{name:<42} {dt:>7.3f}s  {dt/total*100:5.1f}%')
    print(f'{"TOTAL":<42} {total:>7.3f}s')
    print(f'\nCRC ok: {crc_ok}   frames: {n}   '
          f'decode rate {n/read_s:.0f} fps (ffmpeg)')

    import shutil
    shutil.rmtree(work, ignore_errors=True)


if __name__ == '__main__':
    main(float(sys.argv[1]) if len(sys.argv) > 1 else 8.0)

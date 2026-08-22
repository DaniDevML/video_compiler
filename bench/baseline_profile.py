"""
Baseline stage-by-stage profile of the existing v4 pipeline.

Measures wall time for every stage of encode and decode separately so we can
see where the time actually goes before optimising anything.

Usage:  python bench/baseline_profile.py [size_mb]
"""
import os
import sys
import time
import zlib
import math
import subprocess
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import imageio_ffmpeg

import video_codec as vc
import video_encoder as ve
import video_decoder as vd


def make_payload(size_mb: float) -> bytes:
    """Incompressible payload (worst case, gzip cannot shrink it)."""
    rng = np.random.default_rng(1234)
    return rng.integers(0, 256, size=int(size_mb * 1024 * 1024), dtype=np.uint8).tobytes()


class Timer:
    def __init__(self):
        self.marks = []
        self.t0 = time.perf_counter()
        self.last = self.t0

    def mark(self, name):
        now = time.perf_counter()
        self.marks.append((name, now - self.last))
        self.last = now

    def report(self, title, total_bytes):
        total = self.last - self.t0
        print(f'\n=== {title} ===')
        for name, dt in self.marks:
            print(f'  {name:<34} {dt:8.3f} s   ({dt/total*100:5.1f} %)')
        print(f'  {"TOTAL":<34} {total:8.3f} s'
              f'   -> {total_bytes/1024/1024/total:6.2f} MB/s')
        return total


def profile(size_mb: float):
    payload = make_payload(size_mb)
    tmpdir = tempfile.mkdtemp()
    src = os.path.join(tmpdir, 'payload.bin')
    with open(src, 'wb') as f:
        f.write(payload)

    print(f'Payload: {len(payload):,} bytes ({size_mb} MB, incompressible)')
    print(f'Native pixel engine: {vc.NATIVE_AVAILABLE}')
    print(f'Bytes/frame: {vc.BYTES_PER_FRAME:,}')

    # warm up Reed-Solomon JIT so we do not measure numba compile time
    ve.rs_encode(b'\x00' * 1000)
    vd._RS.decode(vd._GF(np.zeros((1, 255), dtype=int)))

    # ---------------- ENCODE ----------------
    t = Timer()
    archive = ve.create_archive([src])
    archive_crc = zlib.crc32(archive) & 0xFFFFFFFF
    t.mark('tar.gz archive')

    encoded = ve.rs_encode(archive)
    t.mark('Reed-Solomon encode')

    data_bits = vc.bytes_to_bits(encoded)
    ndf = math.ceil(len(data_bits) / vc.BITS_PER_FRAME)
    pad = ndf * vc.BITS_PER_FRAME - len(data_bits)
    if pad:
        data_bits = np.concatenate([data_bits, np.zeros(pad, dtype=np.uint8)])
    t.mark('bytes -> bits + pad')

    header_raw = vc.pack_header(len(archive), ndf, len(encoded), archive_crc)
    header_yuv = vc.header_to_yuv_frame(header_raw)

    # pixel encode all frames (measured separately from ffmpeg)
    frames_buf = []
    for s in range(0, ndf, ve.ENCODE_BATCH):
        n = min(ve.ENCODE_BATCH, ndf - s)
        seg = data_bits[s * vc.BITS_PER_FRAME:(s + n) * vc.BITS_PER_FRAME]
        frames_buf.append(vc.bits_to_yuv_frames(seg))
    t.mark('pixel encode (bits -> YUV)')

    codec, hw_params = ve._get_encoder()
    out_mp4 = os.path.join(tmpdir, 'out.mp4')
    exe = imageio_ffmpeg.get_ffmpeg_exe()
    cmd = [exe, '-f', 'rawvideo', '-pix_fmt', 'yuv420p',
           '-s', f'{vc.FRAME_WIDTH}x{vc.FRAME_HEIGHT}', '-r', str(vc.FPS),
           '-i', 'pipe:0', '-c:v', codec, *hw_params,
           '-pix_fmt', 'yuv420p', '-an', '-y', out_mp4]
    kw = dict(stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
              stderr=subprocess.DEVNULL)
    if sys.platform == 'win32':
        kw['creationflags'] = subprocess.CREATE_NO_WINDOW
    p = subprocess.Popen(cmd, **kw)
    p.stdin.write(header_yuv.tobytes())
    for b in frames_buf:
        p.stdin.write(b.tobytes())
    p.stdin.close()
    p.wait()
    t.mark(f'ffmpeg encode ({codec})')

    enc_total = t.report(f'ENCODE  ({ndf + 1} frames)', len(payload))
    mp4_size = os.path.getsize(out_mp4)
    print(f'  video codec: {codec}')
    print(f'  mp4 size: {mp4_size:,} bytes '
          f'({mp4_size / len(payload):.2f}x payload)  '
          f'-> upload cost {mp4_size/1024/1024:.1f} MB')

    del frames_buf

    # ---------------- DECODE ----------------
    t2 = Timer()
    raw_tmp = os.path.join(tmpdir, 'raw.yuv')
    dcmd = vd._build_decode_cmd(out_mp4, 'yuv420p', output=raw_tmp)
    kw2 = dict(stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if sys.platform == 'win32':
        kw2['creationflags'] = subprocess.CREATE_NO_WINDOW
    subprocess.run(dcmd, **kw2)
    t2.mark('ffmpeg decode (mp4 -> raw YUV)')

    nfr = os.path.getsize(raw_tmp) // vc.YUV_FRAME_BYTES
    raw = np.fromfile(raw_tmp, dtype=np.uint8,
                      count=nfr * vc.YUV_FRAME_BYTES).reshape(nfr, -1)
    t2.mark('read raw YUV from disk')

    chunks = []
    for s in range(1, nfr, vd.DECODE_BATCH):
        chunks.append(vc.yuv_frames_to_bits(raw[s:s + vd.DECODE_BATCH]))
    t2.mark('pixel decode (YUV -> bits)')

    all_bits = np.concatenate(chunks).ravel()[:ndf * vc.BITS_PER_FRAME]
    encoded_bytes = vc.bits_to_bytes(all_bits)[:len(encoded)]
    t2.mark('bits -> bytes')

    bit_errors = int(np.count_nonzero(
        np.frombuffer(encoded_bytes, dtype=np.uint8) ^
        np.frombuffer(encoded, dtype=np.uint8)))

    archive_bytes = vd._rs_strip_parity(encoded_bytes)[:len(archive)]
    crc_ok = (zlib.crc32(archive_bytes) & 0xFFFFFFFF) == archive_crc
    if not crc_ok:
        archive_bytes = vd.rs_decode(encoded_bytes, len(archive))
        crc_ok = (zlib.crc32(archive_bytes) & 0xFFFFFFFF) == archive_crc
        t2.mark('Reed-Solomon FULL decode')
    else:
        t2.mark('RS parity strip (no errors)')

    t2.report(f'DECODE  ({nfr} frames)', len(payload))
    print(f'  byte errors before RS: {bit_errors:,} '
          f'({bit_errors/len(encoded)*100:.4f} %)')
    print(f'  CRC verified: {crc_ok}')

    os.unlink(raw_tmp)
    os.unlink(out_mp4)
    os.unlink(src)
    return enc_total


if __name__ == '__main__':
    mb = float(sys.argv[1]) if len(sys.argv) > 1 else 8.0
    profile(mb)

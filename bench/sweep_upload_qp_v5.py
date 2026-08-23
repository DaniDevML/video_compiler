"""
Re-tune the upload quantiser for the current profile.

UPLOAD_QP was chosen for the old v4 geometry. The v5 profile has different
block statistics, so the highest quantiser that still round-trips cleanly is a
different number -- and since upload time dominates end-to-end, it is worth
finding.

Runs the real encoder and the real decoder, so this reflects the shipping path
rather than a synthetic modulation.
"""
import hashlib
import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np

import video_codec as vc
import video_encoder as ve
import video_decoder as vd


def main(mb=6.0):
    rng = np.random.default_rng(1234)
    data = rng.integers(0, 256, int(mb * 1024 * 1024), dtype=np.uint8).tobytes()
    want = hashlib.sha256(data).hexdigest()
    work = tempfile.mkdtemp()
    src = os.path.join(work, 'p.bin')
    with open(src, 'wb') as f:
        f.write(data)

    print(f'{mb} MB payload, profile {vc.DEFAULT_PROFILE.bytes:,} B/frame\n')
    print(f'{"qp":>4} {"video MB":>10} {"expansion":>10} {"encode":>8} '
          f'{"decode":>8}  recovered')
    print('-' * 60)

    orig = ve.UPLOAD_QP
    results = []
    try:
        for qp in (38, 41, 44, 47, 50, 51):
            ve.UPLOAD_QP = qp
            ve._HW_CODEC = None          # force re-probe with the new qp
            ve._HW_PARAMS = None
            ve._get_encoder()
            video = os.path.join(work, f'v{qp}.mp4')
            out = os.path.join(work, f'o{qp}')
            t0 = time.perf_counter()
            ve.encode_files_to_video([src], video, progress=None)
            t_enc = time.perf_counter() - t0
            size = os.path.getsize(video)
            try:
                t0 = time.perf_counter()
                vd.decode_video_to_files(video, out, progress=None)
                t_dec = time.perf_counter() - t0
                got = hashlib.sha256(
                    open(os.path.join(out, 'p.bin'), 'rb').read()).hexdigest()
                ok = got == want
            except Exception as e:
                t_dec = float('nan')
                ok = False
            print(f'{qp:>4} {size/1e6:>10.2f} {size/len(data):>9.2f}x '
                  f'{t_enc:>7.2f}s {t_dec:>7.2f}s  {"yes" if ok else "NO"}')
            if ok:
                results.append((qp, size))
            shutil.rmtree(out, ignore_errors=True)
            os.unlink(video)
    finally:
        ve.UPLOAD_QP = orig
        ve._HW_CODEC = None
        ve._HW_PARAMS = None
        shutil.rmtree(work, ignore_errors=True)

    if results:
        qp, size = min(results, key=lambda r: r[1])
        base = dict(results).get(orig)
        print(f'\nsmallest clean upload: qp {qp} at {size/len(data):.2f}x')
        if base:
            print(f'vs current qp {orig}: {base/size:.2f}x less to upload')


if __name__ == '__main__':
    main(float(sys.argv[1]) if len(sys.argv) > 1 else 6.0)

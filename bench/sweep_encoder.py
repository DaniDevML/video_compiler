"""
Sweep 1: how much can we shrink the uploaded file without losing data?

Holds the v4 format fixed (4x4 blocks, 3bpp Y + 2bpp chroma) and varies only
the ffmpeg encoder preset, measuring uploaded size vs bit error rate.
"""
import os
import sys
import time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from channel import Format, measure

V4 = Format(block_y=4, bpp_y=3, block_c=4, bpp_c=2)

print(f'Format under test: {V4.label}')
print(f'Payload per frame: {V4.bytes:,} bytes\n')

presets = ['x264_lossless']
presets += [f'nvenc_qp{q}' for q in range(18, 52, 4)]
presets += [f'x264flat_qp{q}' for q in range(18, 52, 4)]

print(f'{"preset":<20} {"expansion":>10} {"BER":>11} {"enc s":>7}  verdict')
print('-' * 62)

best = None
for preset in presets:
    t0 = time.perf_counter()
    try:
        r = measure(V4, n_frames=6, preset=preset, yt='none')
    except Exception as e:
        print(f'{preset:<20} FAILED: {e}')
        continue
    dt = time.perf_counter() - t0
    if r is None:
        print(f'{preset:<20} FAILED (no frames back)')
        continue
    v = 'CLEAN' if r['ber'] == 0 else ('recoverable' if r['ber'] < 1e-3 else 'BAD')
    print(f'{preset:<20} {r["expansion"]:>9.2f}x {r["ber"]:>11.2e} {dt:>7.1f}  {v}')
    if r['ber'] == 0 and (best is None or r['expansion'] < best[1]):
        best = (preset, r['expansion'])

print()
if best:
    print(f'Smallest clean upload: {best[0]} at {best[1]:.2f}x expansion')

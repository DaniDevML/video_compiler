"""
Sweep 2: find the block size / bits-per-block that maximises real throughput.

For every candidate format we scan the quantiser and keep the *highest* QP
(= smallest uploaded file) that still round-trips within the error budget.
Reported metrics:

  bytes/frame  -> drives CPU cost (fewer frames = less ffmpeg work)
  expansion    -> drives upload/download time (uploaded bytes per payload byte)

Y plane and chroma planes are swept separately because they are independent
sub-channels.

Usage: python bench/sweep_format.py [yt_preset]
"""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from channel import Format, measure

# RS(255,215) corrects 20 byte errors per 255-byte chunk. Staying under a
# 1e-4 bit error rate leaves a very wide margin for YouTube's own transcode.
BER_BUDGET = 1e-4

YT = sys.argv[1] if len(sys.argv) > 1 else 'none'
QPS = list(range(20, 52, 3))
N = 4

print(f'YouTube simulation: {YT}   BER budget: {BER_BUDGET:g}\n')


def best_for(fmt, label):
    """Highest QP that stays within the error budget -> smallest upload."""
    best = None
    for qp in QPS:
        try:
            r = measure(fmt, n_frames=N, preset=f'nvenc_qp{qp}', yt=YT)
        except Exception:
            continue
        if r is None:
            continue
        if r['ber'] <= BER_BUDGET:
            if best is None or r['dl_expansion'] < best['dl_expansion']:
                best = dict(r, qp=qp)
    if best is None:
        print(f'{label:<28} {fmt.bytes:>9,}   -- no QP met the budget --')
        return None
    print(f'{label:<28} {fmt.bytes:>9,} {best["qp"]:>5} '
          f'{best["dl_expansion"]:>10.2f}x {best["ber"]:>10.1e}')
    return best


print('--- Y plane only (chroma disabled) ---')
print(f'{"format":<28} {"B/frame":>9} {"QP":>5} {"expansion":>11} {"BER":>10}')
print('-' * 68)

y_results = {}
for block in (2, 3, 4, 8):
    for bpp in (1, 2, 3, 4, 5):
        if block == 2 and bpp > 3:
            continue
        f = Format(block_y=block, bpp_y=bpp, block_c=4, bpp_c=2,
                   use_chroma=False)
        r = best_for(f, f'Y block={block} bpp={bpp}')
        if r:
            y_results[(block, bpp)] = r

if y_results:
    win = max(y_results.items(), key=lambda kv: kv[1]['bytes_per_frame'])
    print(f'\nHighest-capacity Y config that met budget: block={win[0][0]} '
          f'bpp={win[0][1]} -> {win[1]["bytes_per_frame"]:,} B/frame')
    cheap = min(y_results.items(), key=lambda kv: kv[1]['dl_expansion'])
    print(f'Lowest-expansion Y config: block={cheap[0][0]} bpp={cheap[0][1]} '
          f'-> {cheap[1]["dl_expansion"]:.2f}x')

print('\n--- chroma contribution (Y fixed at the winning config) ---')
print(f'{"format":<28} {"B/frame":>9} {"QP":>5} {"expansion":>11} {"BER":>10}')
print('-' * 68)

by, by_bpp = (win[0] if y_results else (4, 3))
for block in (2, 4, 8):
    for bpp in (1, 2, 3):
        f = Format(block_y=by, bpp_y=by_bpp, block_c=block, bpp_c=bpp)
        best_for(f, f'+ chroma block={block} bpp={bpp}')

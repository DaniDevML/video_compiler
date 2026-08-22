"""
Sweep 4: does uploading a smaller file hurt recovery?

YouTube throws our bitstream away and re-encodes. If recovery is insensitive to
our upload quantiser, we can upload a much smaller file for free -- a direct
upload-time win. This measures post-transcode BER as a function of upload QP.
"""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from channel import Format, measure

FMT = Format(4, 3, 4, 2)          # current v4 format
N = 24                            # enough frames for rate control to settle
YTS = ['none', 'yt_avc_8m', 'yt_vp9_4m']

print(f'Format: {FMT.label}  ({FMT.bytes:,} B/frame), {N} frames\n')
hdr = f'{"upload":<14} {"upload MB":>10} {"expansion":>10} ' + \
      ' '.join(f'{y:>12}' for y in YTS)
print(hdr)
print('-' * len(hdr))

for qp in (18, 24, 30, 36, 42, 46):
    preset = f'nvenc_qp{qp}'
    row, up_mb, exp = [], None, None
    for yt in YTS:
        try:
            r = measure(FMT, n_frames=N, preset=preset, yt=yt)
        except Exception as e:
            row.append('    ERR     ')
            continue
        if r is None:
            row.append('    n/a     ')
            continue
        up_mb, exp = r['up_size'] / 1e6, r['expansion']
        row.append(f'{r["ber"]:>12.2e}')
    print(f'{preset:<14} {up_mb:>10.2f} {exp:>9.2f}x ' + ' '.join(row))

print('\nIf the YouTube columns are flat across QP, the upload quantiser is '
      'free to raise -> smaller uploads at no cost to recovery.')

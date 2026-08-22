"""
Sweep 3: what actually survives a YouTube-style re-encode?

The upload quantiser barely matters in the end: YouTube discards our bitstream
and re-encodes to its own ladder. What matters is how much payload survives
*their* encoder. This sweeps candidate formats through simulated YouTube
renditions and reports the bit error rate of each.

Calibration note: the v4 format is included so the simulator can be checked
against a format that is known to work on real YouTube.
"""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from channel import Format, measure

CANDIDATES = [
    ('v4 baseline   Y b4/3 + C b4/2', Format(4, 3, 4, 2)),
    ('Y b4/2 + C b4/1              ', Format(4, 2, 4, 1)),
    ('Y b4/3 + C b4/3              ', Format(4, 3, 4, 3)),
    ('Y b4/4 + C b4/2              ', Format(4, 4, 4, 2)),
    ('Y b4/4 + C b4/3              ', Format(4, 4, 4, 3)),
    ('Y b4/5 + C b4/3              ', Format(4, 5, 4, 3)),
    ('Y b2/2 + C b2/2              ', Format(2, 2, 2, 2)),
    ('Y b2/3 + C b2/3              ', Format(2, 3, 2, 3)),
    ('Y b8/4 + C b8/3              ', Format(8, 4, 8, 3)),
]

YTS = ['none', 'yt_avc_8m', 'yt_avc_5m', 'yt_vp9_4m']
UPLOAD_QP = 'nvenc_qp24'      # high-quality upload; YouTube re-encodes anyway

print(f'Upload preset: {UPLOAD_QP}   (4 frames per measurement)\n')
hdr = f'{"format":<32} {"B/frame":>9} ' + ' '.join(f'{y:>11}' for y in YTS)
print(hdr)
print('-' * len(hdr))

for label, fmt in CANDIDATES:
    cells = []
    for yt in YTS:
        try:
            r = measure(fmt, n_frames=4, preset=UPLOAD_QP, yt=yt)
            cells.append('   n/a     ' if r is None else f'{r["ber"]:>11.2e}')
        except Exception:
            cells.append('   ERR     ')
    print(f'{label:<32} {fmt.bytes:>9,} ' + ' '.join(cells))

print('\nBER budget for RS(255,215): ~1e-3 is comfortably correctable, '
      '1e-2 is marginal, >3e-2 is unrecoverable.')

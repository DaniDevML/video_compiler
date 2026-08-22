"""
Sweep 5: payload capacity as a function of delivered channel bitrate.

A format carrying B bytes/frame at 30 fps needs 240*B bits/s of *incompressible*
payload to get through. No channel below that rate can carry it, whatever the
error correction. So the right question is not "what is the densest format" but
"which format maximises error-free payload at the bitrate the platform actually
delivers".

This measures BER for a ladder of formats against a ladder of channel bitrates,
so the format can be chosen from the delivered rate rather than guessed.
"""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from channel import Format, measure

FORMATS = [
    ('Y b16/2 + C b16/1', Format(16, 2, 16, 1)),
    ('Y b8/2  + C b8/1 ', Format(8, 2, 8, 1)),
    ('Y b8/3  + C b8/2 ', Format(8, 3, 8, 2)),
    ('Y b4/1  + C b4/1 ', Format(4, 1, 4, 1)),
    ('Y b4/2  + C b4/1 ', Format(4, 2, 4, 1)),
    ('Y b4/2  + C b4/2 ', Format(4, 2, 4, 2)),
    ('Y b4/3  + C b4/2 ', Format(4, 3, 4, 2)),
    ('Y b4/4  + C b4/3 ', Format(4, 4, 4, 3)),
]

CHANNELS = ['yt_vp9_2m', 'yt_vp9_4m', 'yt_vp9_8m', 'yt_avc_8m', 'yt_avc_12m']
N = 24
UP = 'nvenc_qp30'

print(f'{N} frames per point, upload preset {UP}')
print('payload rate = bytes/frame * 30 fps * 8 = required channel bits/s\n')

hdr = f'{"format":<20} {"B/frame":>9} {"Mbps req":>9} ' + \
      ' '.join(f'{c.replace("yt_",""):>10}' for c in CHANNELS)
print(hdr)
print('-' * len(hdr))

for label, fmt in FORMATS:
    req = fmt.bytes * 30 * 8 / 1e6
    cells = []
    for ch in CHANNELS:
        try:
            r = measure(fmt, n_frames=N, preset=UP, yt=ch)
            cells.append('   n/a    ' if r is None else f'{r["ber"]:>10.1e}')
        except Exception:
            cells.append('   ERR    ')
    print(f'{label:<20} {fmt.bytes:>9,} {req:>9.1f} ' + ' '.join(cells))

print('\nRS(255,215) corrects up to ~7.8% byte errors; a bit error rate below '
      '~1e-3 is comfortably correctable.')

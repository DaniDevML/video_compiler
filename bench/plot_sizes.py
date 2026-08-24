"""
Chart the size sweep produced by bench/bench_sizes.py.

Reads bench/size_sweep_results.json and writes static/size_sweep.png. Every
point plotted is a measured round trip that verified byte-identical; nothing
is fitted, extrapolated or smoothed. The dashed reference line is the only
drawn-not-measured element on the figure, and it is labelled as such.

Usage: python bench/plot_sizes.py
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt                                   # noqa: E402
from matplotlib.ticker import FuncFormatter                       # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
RESULTS = os.path.join(HERE, 'size_sweep_results.json')
OUT = os.path.join(ROOT, 'static', 'size_sweep.png')

ENC = '#4f8ef7'
DEC = '#7c5bf7'
INK = '#1b212a'
MUTED = '#64748b'
GRID = '#dde3ec'


def human(n):
    if n >= 1e9:
        v = n / 1e9
        return f'{v:g} GB'
    return f'{n / 1e6:g} MB'


def main():
    data = json.load(open(RESULTS))
    runs = [r for r in data['runs'] if r.get('ok')]
    failed = [r for r in data['runs'] if not r.get('ok')]
    if not runs:
        print('No successful runs to plot.')
        return 1

    size = [r['payload_bytes'] for r in runs]
    t_enc = [r['t_encode_total'] for r in runs]
    t_dec = [r['t_decode_total'] for r in runs]
    mbps_enc = [b / t / 1e6 for b, t in zip(size, t_enc)]
    mbps_dec = [b / t / 1e6 for b, t in zip(size, t_dec)]

    plt.rcParams.update({
        'font.family': 'DejaVu Sans',
        'font.size': 9,
        'axes.edgecolor': GRID,
        'axes.labelcolor': INK,
        'text.color': INK,
        'xtick.color': MUTED,
        'ytick.color': MUTED,
        'figure.facecolor': 'white',
        'axes.facecolor': 'white',
    })

    fig, axes = plt.subplots(1, 3, figsize=(15, 4.9))

    # ---------------------------------------------------------- time vs size
    ax = axes[0]
    ax.loglog(size, t_enc, 'o-', color=ENC, lw=2, ms=6, label='encode', zorder=3)
    ax.loglog(size, t_dec, 's-', color=DEC, lw=2, ms=6, label='decode', zorder=3)

    # A perfectly linear cost, anchored to the largest measured encode point.
    ref_x = [size[0], size[-1]]
    scale = t_enc[-1] / size[-1]
    ax.loglog(ref_x, [x * scale for x in ref_x], '--', color=MUTED, lw=1,
              label='linear reference', zorder=2)

    ax.set_xlabel('payload')
    ax.set_ylabel('seconds')
    ax.set_title('Time vs payload size', fontweight='bold', loc='left')
    ax.grid(True, which='both', color=GRID, lw=.6, zorder=0)
    ax.legend(frameon=False, loc='upper left')
    ax.xaxis.set_major_formatter(FuncFormatter(lambda v, _p: human(v)))
    ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _p: f'{v:g}s'))

    # Label the endpoints, which are the numbers people actually want.
    ax.annotate(f'decode {t_dec[-1] / 60:.1f} min', (size[-1], t_dec[-1]),
                textcoords='offset points', xytext=(-6, 6), ha='right',
                fontsize=8, color=DEC, fontweight='bold')
    ax.annotate(f'encode {t_enc[-1] / 60:.1f} min', (size[-1], t_enc[-1]),
                textcoords='offset points', xytext=(-6, -14), ha='right',
                fontsize=8, color=ENC, fontweight='bold')

    # --------------------------------------------------------- throughput
    ax = axes[1]
    ax.semilogx(size, mbps_enc, 'o-', color=ENC, lw=2, ms=6, label='encode')
    ax.semilogx(size, mbps_dec, 's-', color=DEC, lw=2, ms=6, label='decode')
    ax.set_xlabel('payload')
    ax.set_ylabel('MB/s')
    ax.set_title('Throughput', fontweight='bold', loc='left')
    ax.grid(True, which='both', color=GRID, lw=.6, zorder=0)
    ax.legend(frameon=False, loc='lower right')
    ax.set_ylim(bottom=0)
    ax.xaxis.set_major_formatter(FuncFormatter(lambda v, _p: human(v)))

    # ----------------------------------------------------- stage breakdown
    ax = axes[2]
    labels = [human(b) for b in size]
    idx = range(len(runs))
    w = 0.38

    enc_stack = [
        ('encode: H.264 + pixels + RS', [r['t_video_encode'] for r in runs], ENC),
        ('encode: encrypt', [r['t_encrypt'] for r in runs], '#16a34a'),
        ('encode: tar', [r['t_archive'] for r in runs], '#fbbf24'),
    ]
    dec_stack = [
        ('decode: H.264 + pixels + RS', [r['t_video_decode'] for r in runs], DEC),
        ('decode: decrypt', [r['t_decrypt'] for r in runs], '#22c55e'),
        ('decode: untar', [r['t_extract'] for r in runs], '#f59e0b'),
    ]

    for stack, offset in ((enc_stack, -w / 2), (dec_stack, w / 2)):
        bottom = [0.0] * len(runs)
        for label, vals, colour in stack:
            ax.bar([i + offset for i in idx], vals, w, bottom=bottom,
                   color=colour, label=label, zorder=3)
            bottom = [b + v for b, v in zip(bottom, vals)]

    ax.set_xticks(list(idx))
    ax.set_xticklabels(labels, rotation=45, ha='right')
    ax.set_ylabel('seconds')
    ax.set_yscale('log')
    ax.set_title('Where the time goes  (left: encode, right: decode)',
                 fontweight='bold', loc='left')
    ax.grid(True, axis='y', which='both', color=GRID, lw=.6, zorder=0)
    ax.legend(frameon=False, fontsize=7.5, ncol=2, loc='upper left')

    exp = runs[-1]['expansion']
    fig.suptitle(
        f'VidCompiler — encode and decode time by payload size   '
        f'({len(runs)} measured round trips, all byte-identical)',
        fontweight='bold', fontsize=12, x=0.008, ha='left', y=0.99)
    fig.text(
        0.008, 0.925,
        f'Local compute only — no network transfer. Incompressible payloads, '
        f'encrypted (AES-256-GCM), {exp:.2f}x video expansion. '
        f'{data["machine"]["cores"]}-core CPU, NVENC.',
        fontsize=8.5, color=MUTED, ha='left')

    if failed:
        fig.text(0.008, 0.9,
                 'Not plotted: ' + ', '.join(
                     f'{human(r["payload_bytes"])} ({r.get("error", "failed")})'
                     for r in failed),
                 fontsize=8.5, color='#b91c1c', ha='left')

    fig.tight_layout(rect=(0, 0, 1, 0.90))
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    fig.savefig(OUT, dpi=140)
    print(f'Wrote {OUT}')

    print(f'\n{"size":>9} {"encode":>10} {"decode":>10} {"enc MB/s":>9} '
          f'{"dec MB/s":>9} {"videos":>7}')
    for r in runs:
        print(f'{human(r["payload_bytes"]):>9} '
              f'{r["t_encode_total"]:>9.1f}s {r["t_decode_total"]:>9.1f}s '
              f'{r["payload_bytes"] / r["t_encode_total"] / 1e6:>9.1f} '
              f'{r["payload_bytes"] / r["t_decode_total"] / 1e6:>9.1f} '
              f'{r["shards"]:>7}')
    return 0


if __name__ == '__main__':
    sys.exit(main())

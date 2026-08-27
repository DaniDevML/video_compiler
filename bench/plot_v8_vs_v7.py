"""
Chart bench/v8_vs_v7_results.json -> static/v8_vs_v7.png

Every point is a measured round trip that verified byte-identical. The only
derived quantity is estimated upload time, which is measured video bytes
divided by the 18.8 Mbit/s that bench_youtube.py measured against the live
service; it is labelled as an estimate on the figure.
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
RESULTS = os.path.join(HERE, 'v8_vs_v7_results.json')
OUT = os.path.join(ROOT, 'static', 'v8_vs_v7.png')

V7 = '#4f8ef7'
V8 = '#7C3AED'
INK = '#1b212a'
MUTED = '#64748b'
GRID = '#dde3ec'


def human(n):
    return f'{n / 1e9:g} GB' if n >= 1e9 else f'{n / 1e6:g} MB'


def main():
    data = json.load(open(RESULTS))
    runs = [r for r in data['runs']
            if r['formats'].get('v7', {}).get('ok')
            and r['formats'].get('v8', {}).get('ok')]
    if not runs:
        print('nothing to plot')
        return 1

    size = [r['payload_bytes'] for r in runs]
    labels = [human(b) for b in size]
    idx = range(len(runs))

    def col(fmt, key):
        return [r['formats'][fmt][key] for r in runs]

    plt.rcParams.update({
        'font.family': 'DejaVu Sans', 'font.size': 9,
        'axes.edgecolor': GRID, 'axes.labelcolor': INK, 'text.color': INK,
        'xtick.color': MUTED, 'ytick.color': MUTED,
        'figure.facecolor': 'white', 'axes.facecolor': 'white',
    })
    fig, axes = plt.subplots(1, 4, figsize=(18, 4.6))
    w = 0.36

    # ---- 1. compute time --------------------------------------------------
    ax = axes[0]
    ax.bar([i - w / 2 for i in idx], col('v7', 't_encode'), w,
           color=V7, label='v7 encode', zorder=3)
    ax.bar([i - w / 2 for i in idx], col('v7', 't_decode'), w,
           bottom=col('v7', 't_encode'), color=V7, alpha=.45,
           label='v7 decode', zorder=3)
    ax.bar([i + w / 2 for i in idx], col('v8', 't_encode'), w,
           color=V8, label='v8 encode', zorder=3)
    ax.bar([i + w / 2 for i in idx], col('v8', 't_decode'), w,
           bottom=col('v8', 't_encode'), color=V8, alpha=.45,
           label='v8 decode', zorder=3)
    ax.set_xticks(list(idx)); ax.set_xticklabels(labels)
    ax.set_ylabel('seconds')
    ax.set_title('Local compute  (v8 is faster)', fontweight='bold', loc='left')
    ax.grid(True, axis='y', color=GRID, lw=.6, zorder=0)
    ax.legend(frameon=False, fontsize=7.5)

    # ---- 2. bytes on the wire --------------------------------------------
    ax = axes[1]
    ax.bar([i - w / 2 for i in idx], [b / 1e9 for b in col('v7', 'video_bytes')],
           w, color=V7, label='v7', zorder=3)
    ax.bar([i + w / 2 for i in idx], [b / 1e9 for b in col('v8', 'video_bytes')],
           w, color=V8, label='v8', zorder=3)
    for i, r in enumerate(runs):
        ratio = (r['formats']['v8']['video_bytes']
                 / r['formats']['v7']['video_bytes'])
        ax.annotate(f'{ratio:.2f}x',
                    (i + w / 2, r['formats']['v8']['video_bytes'] / 1e9),
                    textcoords='offset points', xytext=(0, 3),
                    ha='center', fontsize=7.5, color=V8, fontweight='bold')
    ax.set_xticks(list(idx)); ax.set_xticklabels(labels)
    ax.set_ylabel('GB uploaded')
    ax.set_title('Bytes on the wire  (v8 costs more)', fontweight='bold', loc='left')
    ax.grid(True, axis='y', color=GRID, lw=.6, zorder=0)
    ax.legend(frameon=False)

    # ---- 3. estimated end-to-end ------------------------------------------
    ax = axes[2]
    for fmt, colour, mark in (('v7', V7, 'o'), ('v8', V8, 's')):
        total = [f_e + f_d + f_u for f_e, f_d, f_u in
                 zip(col(fmt, 't_encode'), col(fmt, 't_decode'),
                     col(fmt, 'est_upload_s'))]
        ax.plot(idx, [t / 60 for t in total], mark + '-', color=colour,
                lw=2, ms=6, label=f'{fmt} total', zorder=3)
    ax.set_xticks(list(idx)); ax.set_xticklabels(labels)
    ax.set_ylabel('minutes')
    ax.set_title('Estimated end to end  (upload dominates)',
                 fontweight='bold', loc='left')
    ax.grid(True, color=GRID, lw=.6, zorder=0)
    ax.legend(frameon=False)

    # ---- 4. uploads needed -------------------------------------------------
    ax = axes[3]
    ax.bar([i - w / 2 for i in idx], col('v7', 'videos_needed'), w,
           color=V7, label='v7', zorder=3)
    ax.bar([i + w / 2 for i in idx], col('v8', 'videos_needed'), w,
           color=V8, label='v8', zorder=3)
    ax.set_xticks(list(idx)); ax.set_xticklabels(labels)
    ax.set_ylabel('videos')
    ax.set_yticks(range(0, max(col('v7', 'videos_needed')) + 2))
    ax.set_title('Uploads needed  (v8 needs fewer)', fontweight='bold', loc='left')
    ax.grid(True, axis='y', color=GRID, lw=.6, zorder=0)
    ax.legend(frameon=False)

    fig.suptitle('VidCompiler v8 vs v7 — 8 measured round trips, all byte-identical',
                 fontweight='bold', fontsize=12, x=0.006, ha='left', y=0.99)
    fig.text(0.006, 0.925,
             'v8: 166,540 B/frame (2.97x v7) from 3 luma levels instead of 2. '
             'Compute measured locally; upload time estimated from measured bytes '
             f'at {data["upload_mbit"]} Mbit/s, the rate measured against real YouTube.',
             fontsize=8.5, color=MUTED, ha='left')

    fig.tight_layout(rect=(0, 0, 1, 0.90))
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    fig.savefig(OUT, dpi=140)
    print(f'Wrote {OUT}')
    return 0


if __name__ == '__main__':
    sys.exit(main())

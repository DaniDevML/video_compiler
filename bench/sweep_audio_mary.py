"""
Find the fastest M-ary FSK setting that survives a real AAC round trip.

Three knobs interact:

  * Orthogonality needs tone spacing >= the symbol rate.
  * AAC low-passes somewhere around 15-16 kHz at 128 kbps, so the highest tone
    must stay below that. With M tones that caps M * spacing.
  * AAC's transform smears very short symbols, which puts a floor on symbol
    duration independent of bandwidth.

Together those bound throughput at log2(M)/M * B, which pure theory maximises
near M = 3 -- i.e. theory says M-ary FSK barely beats binary. Whether that
holds through an actual AAC encoder is an empirical question, so this measures
every viable combination rather than arguing about it.

Usage: python bench/sweep_audio_mary.py
"""
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import imageio_ffmpeg

import audio_codec as ac

FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()

# AAC at 128 kbps keeps roughly this much bandwidth; tones above it are gone.
MAX_TONE_HZ = 15_000
MIN_TONE_HZ = 700

# symbol rates that divide 44100 exactly
RATES = [245, 300, 350, 420, 490, 525, 630, 700, 735, 882, 980,
         1050, 1225, 1260, 1470, 1575, 2100, 2450, 2940, 3150, 4410]


def kw():
    k = dict(stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if sys.platform == 'win32':
        k['creationflags'] = subprocess.CREATE_NO_WINDOW
    return k


def aac_roundtrip(pcm: bytes, bitrate='128k') -> bytes:
    a = tempfile.mktemp(suffix='.raw')
    m = tempfile.mktemp(suffix='.m4a')
    b = tempfile.mktemp(suffix='.raw')
    try:
        with open(a, 'wb') as f:
            f.write(pcm)
        subprocess.run([FFMPEG, '-f', 's16le', '-ar', str(ac.SAMPLE_RATE),
                        '-ac', '1', '-i', a, '-c:a', 'aac', '-b:a', bitrate,
                        '-y', m], **kw())
        subprocess.run([FFMPEG, '-i', m, '-f', 's16le', '-ar',
                        str(ac.SAMPLE_RATE), '-ac', '1', '-y', b], **kw())
        with open(b, 'rb') as f:
            return f.read()
    finally:
        for q in (a, m, b):
            if os.path.exists(q):
                os.unlink(q)


def try_config(k, rate, payload, seconds, bitrate='128k'):
    """Returns (ok, bits_per_sec, capacity_bytes_per_sec) or None if infeasible."""
    M = 1 << k
    spacing = rate                        # minimum orthogonal spacing
    base = max(MIN_TONE_HZ, spacing)
    if base + (M - 1) * spacing > MAX_TONE_HZ:
        return None
    if ac.SAMPLE_RATE % rate:
        return None

    saved = (ac.BITS_PER_SYMBOL, ac.SYMBOL_RATE, ac.FREQ_BASE, ac.FREQ_SPACING)
    try:
        ac.configure(bits_per_symbol=k, symbol_rate=rate,
                     freq_base=base, freq_spacing=spacing)
        total = int(seconds * ac.SAMPLE_RATE)
        cap = ac.max_payload_bytes(total)
        if cap < len(payload):
            return None
        pcm = ac.float32_to_s16(ac.encode_audio(payload, total))
        got = ac.decode_audio(aac_roundtrip(pcm, bitrate))
        return (got == payload, k * rate, cap / seconds)
    finally:
        ac.configure(bits_per_symbol=saved[0], symbol_rate=saved[1],
                     freq_base=saved[2], freq_spacing=saved[3])


def main():
    payload = bytes(range(64))
    seconds = 12.0
    print(f'{seconds:.0f}s of audio, {len(payload)}-byte payload, real AAC 128k')
    print(f'tones constrained to {MIN_TONE_HZ}-{MAX_TONE_HZ} Hz\n')
    print(f'{"bits/sym":>9} {"M":>4} {"sym rate":>9} {"top tone":>9} '
          f'{"raw bps":>8} {"bytes/s":>8}  result')
    print('-' * 66)

    winners = []
    for k in (1, 2, 3, 4):
        M = 1 << k
        for rate in RATES:
            r = try_config(k, rate, payload, seconds)
            if r is None:
                continue
            ok, bps, cap = r
            top = max(MIN_TONE_HZ, rate) + (M - 1) * rate
            print(f'{k:>9} {M:>4} {rate:>9} {top:>9} {bps:>8} {cap:>8.0f}  '
                  f'{"ok" if ok else "FAIL"}')
            if ok:
                winners.append((cap, k, rate, bps))

    print()
    if not winners:
        print('nothing survived; widen the search')
        return
    winners.sort(reverse=True)
    cap, k, rate, bps = winners[0]
    M = 1 << k
    print(f'best: {k} bits/symbol ({M}-FSK) at {rate} sym/s '
          f'= {bps} raw bits/s, {cap:.0f} payload bytes/s')
    print('\ntop five by payload throughput:')
    for cap, k, rate, bps in winners[:5]:
        print(f'  {1<<k:>3}-FSK  {rate:>5} sym/s  {bps:>6} bps  '
              f'{cap:>6.0f} B/s')

    base_cap = 215.0   # the shipping binary-FSK setting, measured earlier
    print(f'\nvs the previous binary setting ({base_cap:.0f} B/s): '
          f'{winners[0][0]/base_cap:.2f}x')


if __name__ == '__main__':
    main()

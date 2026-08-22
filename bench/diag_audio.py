"""
Diagnose the audio channel: separate codec bugs from AAC damage.

Runs each symbol rate twice -- once on the clean generated signal and once
through a real AAC encode -- so a failure can be attributed to the modem
itself or to the lossy codec.
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


def kw():
    k = dict(stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if sys.platform == 'win32':
        k['creationflags'] = subprocess.CREATE_NO_WINDOW
    return k


def aac_roundtrip(pcm_s16: bytes, bitrate='128k') -> bytes:
    a = tempfile.mktemp(suffix='.raw')
    m = tempfile.mktemp(suffix='.m4a')
    b = tempfile.mktemp(suffix='.raw')
    try:
        open(a, 'wb').write(pcm_s16)
        subprocess.run([FFMPEG, '-f', 's16le', '-ar', str(ac.SAMPLE_RATE),
                        '-ac', '1', '-i', a, '-c:a', 'aac', '-b:a', bitrate,
                        '-y', m], **kw())
        subprocess.run([FFMPEG, '-i', m, '-f', 's16le', '-ar',
                        str(ac.SAMPLE_RATE), '-ac', '1', '-y', b], **kw())
        return open(b, 'rb').read()
    finally:
        for p in (a, m, b):
            if os.path.exists(p):
                os.unlink(p)


def configure(rate, f0, f1):
    ac.BITS_PER_SEC = rate
    ac.SAMPLES_PER_BIT = ac.SAMPLE_RATE // rate
    ac.FREQ_0, ac.FREQ_1 = f0, f1
    ac.PREAMBLE_BITS = int(ac.PREAMBLE_SECS * rate)
    ac.SILENCE_BITS = int(ac.SILENCE_SECS * rate)


ORIG = (ac.BITS_PER_SEC, ac.SAMPLES_PER_BIT, ac.FREQ_0, ac.FREQ_1,
        ac.PREAMBLE_BITS, ac.SILENCE_BITS)

payload = bytes(range(36))          # a header-sized payload
print(f'payload {len(payload)} bytes\n')
print(f'{"rate":>6} {"tones":>12} {"spb":>5} {"secs":>6} {"cap":>6} '
      f'{"clean":>8} {"via AAC":>9}')
print('-' * 60)

for rate, f0, f1 in [(100, 1500, 3000), (441, 1500, 3000), (900, 2000, 4000),
                     (1470, 3000, 6000), (2100, 3000, 6000),
                     (2100, 4000, 8000), (2940, 4000, 8000),
                     (2940, 5000, 10000), (4410, 5000, 10000),
                     (4410, 6000, 12000), (5512, 6000, 12000)]:
    configure(rate, f0, f1)
    # give each rate enough audio to fit one RS chunk plus overhead
    secs = 2.0 + (255 * 8) / rate + 1.0
    total = int(secs * ac.SAMPLE_RATE)
    cap = ac.max_payload_bytes(total)
    if cap < len(payload):
        print(f'{rate:>6} {f"{f0}/{f1}":>12} {ac.SAMPLES_PER_BIT:>5} '
              f'{secs:>6.1f} {cap:>6}   capacity too small')
        continue
    sig = ac.encode_audio(payload, total)
    pcm = ac.float32_to_s16(sig)

    clean = ac.decode_audio(pcm)
    viaac = ac.decode_audio(aac_roundtrip(pcm))
    print(f'{rate:>6} {f"{f0}/{f1}":>12} {ac.SAMPLES_PER_BIT:>5} '
          f'{secs:>6.1f} {cap:>6} '
          f'{"OK" if clean == payload else "FAIL":>8} '
          f'{"OK" if viaac == payload else "FAIL":>9}')

(ac.BITS_PER_SEC, ac.SAMPLES_PER_BIT, ac.FREQ_0, ac.FREQ_1,
 ac.PREAMBLE_BITS, ac.SILENCE_BITS) = ORIG

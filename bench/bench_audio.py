"""
Audio channel: what actually survives YouTube's AAC re-encode, and how fast?

The audio track is currently run at 100 bits/s. This pushes the symbol rate up
and puts every setting through a real AAC 128k encode/decode -- the same thing
YouTube does to the audio -- to find the fastest rate that still recovers the
payload intact.

It also puts the result in perspective against the pixel channel, which is the
honest way to report what the audio track contributes.
"""
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import imageio_ffmpeg

import audio_codec as ac
import video_codec as vc

FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()


def kw():
    k = dict(stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if sys.platform == 'win32':
        k['creationflags'] = subprocess.CREATE_NO_WINDOW
    return k


def aac_roundtrip(pcm_s16: bytes, bitrate='128k') -> bytes:
    """Encode mono PCM to AAC and decode it back, as YouTube would."""
    raw_in = tempfile.mktemp(suffix='.raw')
    m4a = tempfile.mktemp(suffix='.m4a')
    raw_out = tempfile.mktemp(suffix='.raw')
    try:
        with open(raw_in, 'wb') as f:
            f.write(pcm_s16)
        subprocess.run(
            [FFMPEG, '-f', 's16le', '-ar', str(ac.SAMPLE_RATE), '-ac', '1',
             '-i', raw_in, '-c:a', 'aac', '-b:a', bitrate, '-y', m4a], **kw())
        subprocess.run(
            [FFMPEG, '-i', m4a, '-f', 's16le', '-ar', str(ac.SAMPLE_RATE),
             '-ac', '1', '-y', raw_out], **kw())
        with open(raw_out, 'rb') as f:
            return f.read()
    finally:
        for p in (raw_in, m4a, raw_out):
            if os.path.exists(p):
                os.unlink(p)


def try_rate(bits_per_sec, freq0, freq1, payload, seconds):
    """Reconfigure the codec for a symbol rate and test a full round-trip."""
    orig = (ac.BITS_PER_SEC, ac.SAMPLES_PER_BIT, ac.FREQ_0, ac.FREQ_1,
            ac.PREAMBLE_BITS, ac.SILENCE_BITS)
    ac.BITS_PER_SEC = bits_per_sec
    ac.SAMPLES_PER_BIT = ac.SAMPLE_RATE // bits_per_sec
    ac.FREQ_0, ac.FREQ_1 = freq0, freq1
    ac.PREAMBLE_BITS = int(ac.PREAMBLE_SECS * bits_per_sec)
    ac.SILENCE_BITS = int(ac.SILENCE_SECS * bits_per_sec)
    try:
        total_samples = int(seconds * ac.SAMPLE_RATE)
        cap = ac.max_payload_bytes(total_samples)
        if cap < len(payload):
            return None, cap
        sig = ac.encode_audio(payload, total_samples)
        pcm = ac.float32_to_s16(sig)
        back = aac_roundtrip(pcm)
        got = ac.decode_audio(back)
        return got == payload, cap
    finally:
        (ac.BITS_PER_SEC, ac.SAMPLES_PER_BIT, ac.FREQ_0, ac.FREQ_1,
         ac.PREAMBLE_BITS, ac.SILENCE_BITS) = orig


def main():
    seconds = 20.0
    payload = bytes(range(256)) * 2      # 512 bytes
    print(f'{seconds:.0f}s of audio, {len(payload)} byte payload, '
          f'through real AAC 128k\n')

    print(f'{"rate":>7} {"tones (Hz)":>14} {"capacity":>10} {"bytes/s":>9}  result')
    print('-' * 58)

    configs = [
        (100, 1500, 3000),
        (200, 1500, 3000),
        (400, 1500, 3000),
        (400, 2000, 4000),
        (800, 2000, 4000),
        (800, 3000, 6000),
        (1600, 3000, 6000),
        (2100, 4000, 8000),
    ]
    best = None
    for rate, f0, f1 in configs:
        try:
            ok, cap = try_rate(rate, f0, f1, payload, seconds)
        except Exception as e:
            print(f'{rate:>7} {f"{f0}/{f1}":>14}  error {type(e).__name__}')
            continue
        if ok is None:
            print(f'{rate:>7} {f"{f0}/{f1}":>14} {cap:>10} '
                  f'{cap/seconds:>9.1f}  payload exceeds capacity')
            continue
        verdict = 'exact recovery' if ok else 'FAILED to recover'
        print(f'{rate:>7} {f"{f0}/{f1}":>14} {cap:>10} '
              f'{cap/seconds:>9.1f}  {verdict}')
        if ok and (best is None or rate > best[0]):
            best = (rate, cap / seconds)

    print()
    if best:
        rate, bps = best
        pixel_rate = vc.BYTES_PER_FRAME * vc.FPS
        print(f'Fastest reliable audio rate: {rate} bits/s '
              f'= {bps:.0f} payload bytes/s')
        print(f'Pixel channel for comparison: {pixel_rate:,} bytes/s '
              f'({pixel_rate/max(bps,1e-9):,.0f}x the audio channel)')
        print(f'Audio contributes {bps/pixel_rate*100:.4f}% of total capacity.')


if __name__ == '__main__':
    main()

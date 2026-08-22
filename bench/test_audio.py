"""
Audio channel validation at the configured symbol rate.

Checks exact recovery across many random payloads, at AAC bitrates down to
64 kbps (below anything YouTube serves), plus the boundary cases that the
length-framing fix exists to handle.
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
fails = 0


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


def check(name, cond):
    global fails
    print(f'  {"PASS" if cond else "FAIL"}  {name}')
    if not cond:
        fails += 1


def roundtrip(payload, secs, bitrate='128k'):
    total = int(secs * ac.SAMPLE_RATE)
    if ac.max_payload_bytes(total) < len(payload):
        return None
    pcm = ac.float32_to_s16(ac.encode_audio(payload, total))
    return ac.decode_audio(aac_roundtrip(pcm, bitrate))


print(f'symbol rate {ac.BITS_PER_SEC} bits/s, tones '
      f'{ac.FREQ_0}/{ac.FREQ_1} Hz, {ac.SAMPLES_PER_BIT} samples/symbol\n')

print('[1] random payloads through AAC 128k')
rng = np.random.default_rng(17)
ok_all = True
for i in range(8):
    p = rng.integers(0, 256, 36, dtype=np.uint8).tobytes()
    if roundtrip(p, 6.0) != p:
        ok_all = False
check('8 random 36-byte payloads recovered exactly', ok_all)

print('\n[2] payload lengths around the RS chunk boundary')
for n in (1, 35, 36, 214, 215, 216):
    p = rng.integers(0, 256, n, dtype=np.uint8).tobytes()
    got = roundtrip(p, 12.0)
    check(f'{n:>3}-byte payload round-trips', got == p)

print('\n[3] lower AAC bitrates (below anything YouTube serves)')
p = rng.integers(0, 256, 36, dtype=np.uint8).tobytes()
for br in ('128k', '96k', '64k'):
    check(f'AAC {br}', roundtrip(p, 6.0, br) == p)

print('\n[4] a real header survives the audio channel')
hdr = vc.pack_header(9_999_999, 1234, 10_000_000, 0x12345678, audio_bytes=36)
got = roundtrip(hdr, 6.0)
check('header bytes identical', got == hdr)
if got == hdr:
    parsed = vc.unpack_header(got)
    check('header parses back correctly',
          parsed['archive_size'] == 9_999_999 and parsed['crc32'] == 0x12345678)

rate_bytes = ac.max_payload_bytes(ac.SAMPLE_RATE * 60) / 60
pixel = vc.BYTES_PER_FRAME * vc.FPS
print(f'\nsustained audio capacity: {rate_bytes:.0f} payload bytes/s')
print(f'pixel channel:            {pixel:,} bytes/s')
print(f'audio share of total capacity: {rate_bytes/pixel*100:.4f}%')

print(f'\n{"ALL AUDIO TESTS PASSED" if fails == 0 else f"{fails} FAILED"}')
sys.exit(1 if fails else 0)

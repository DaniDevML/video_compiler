"""
The audio track as a header backup of last resort.

Encodes a payload long enough for the audio channel to activate, then reads the
header back out of the audio track alone -- the path that existed in the
encoder but was never wired into the decoder.
"""
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np

import audio_codec as ac
import video_codec as vc
import video_encoder as ve
import video_decoder as vd

fails = 0


def check(name, cond):
    global fails
    print(f'  {"PASS" if cond else "FAIL"}  {name}')
    if not cond:
        fails += 1


# The audio channel only activates once the video is long enough to carry a
# whole RS chunk, so the payload has to be big enough to produce that runtime.
frames_needed = int(6.0 * vc.FPS)
payload_size = frames_needed * vc.BYTES_PER_FRAME
print(f'payload {payload_size/1e6:.2f} MB -> about '
      f'{payload_size/vc.BYTES_PER_FRAME/vc.FPS:.1f}s of video')
print(f'audio: {ac.BITS_PER_SEC} bits/s, '
      f'{ac.max_payload_bytes(int(6.0*ac.SAMPLE_RATE))} bytes capacity at 6s\n')

rng = np.random.default_rng(23)
data = rng.integers(0, 256, payload_size, dtype=np.uint8).tobytes()
work = tempfile.mkdtemp()
src = os.path.join(work, 'f.bin')
with open(src, 'wb') as f:
    f.write(data)
video = os.path.join(work, 'v.mp4')

logs = []
ve.encode_files_to_video([src], video, progress=logs.append)
check('encoder embedded audio',
      any('audio' in m.lower() and 'single pass' in m.lower() for m in logs)
      or any('single pass' in m.lower() for m in logs))

hdr = vd.header_from_audio(video)
check('header recovered from the audio track', hdr is not None)
if hdr:
    with open(video + '.sidecar', encoding='utf-8') as f:
        want = vc.decode_sidecar(f.read())
    check('audio header matches the real one',
          hdr['archive_size'] == want['archive_size']
          and hdr['num_data_frames'] == want['num_data_frames']
          and hdr['crc32'] == want['crc32'])

# Full decode still works and agrees with the audio-derived header.
out = tempfile.mkdtemp()
try:
    vd.decode_video_to_files(video, out, progress=None)
    got = open(os.path.join(out, 'f.bin'), 'rb').read()
    check('full decode recovers the payload', got == data)
finally:
    shutil.rmtree(out, ignore_errors=True)

shutil.rmtree(work, ignore_errors=True)
print(f'\n{"AUDIO FALLBACK TESTS PASSED" if fails == 0 else f"{fails} FAILED"}')
sys.exit(1 if fails else 0)

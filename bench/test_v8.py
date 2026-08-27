"""
The v8 format end to end: 3-level luma, and payload in the audio track.

Two things are new in v8 and both can fail quietly, which is why they are
tested against the real encoder and decoder rather than at the kernel level:

  * a level count that is not a power of two, so bytes travel as base-3 digits
  * payload in the audio track, which the frames then never carry

The second is the one to be careful about. Before v8 the audio was pure
redundancy and losing it cost nothing; now those bytes exist nowhere else, so
this checks that a missing track is reported as an error rather than silently
producing a short archive.
"""
import os
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np

import paths
import video_codec as vc
import video_encoder as ve
import video_decoder as vd

WORK = os.environ.get('VIDCOMPILER_BENCH_WORK', 'E:/vid_compiler/benchwork')
WORK = os.path.join(WORK, 'test_v8')
fails = 0


def check(name, cond, detail=''):
    global fails
    print(f'  {"PASS" if cond else "FAIL"}  {name}'
          + (f'  [{detail}]' if detail and not cond else ''))
    if not cond:
        fails += 1


def main():
    global fails
    shutil.rmtree(WORK, ignore_errors=True)
    os.makedirs(WORK)
    rng = np.random.default_rng(2026)

    print('The v8 profile is what the measurements chose')
    p = vc.PROFILE_V8
    check('luma carries 3 levels', p.levels_y == 3)
    check('chroma stays at 4 levels', p.levels_c == 4)
    check('base-3 packs 12 digits into 19 bits',
          (p.dig_y, p.gbits_y) == (12, 19))
    check('every plane is byte-aligned per frame',
          p.bits % 8 == 0 and p.bits_y % 8 == 0 and p.bits_c % 8 == 0)
    check(f'{p.bytes:,} bytes per frame, 2.97x the v5 default',
          p.bytes == 166540)

    print('\nHeaders')
    h = vc.pack_header(1, 2, 3, 4, audio_bytes=5, profile=p)
    d = vc.unpack_header(h)
    check('a v8 profile writes a v8 header', d['magic'] == vc.MAGIC_V8)
    check('level counts survive the header', (d['levels_y'], d['levels_c']) == (3, 4))
    check('the profile rebuilds from the header',
          vc.profile_from_header(d).bytes == p.bytes)
    h5 = vc.pack_header(1, 2, 3, 4, profile=vc.PROFILE_V5)
    check('a power-of-two profile still writes a v5 header',
          vc.unpack_header(h5)['magic'] == vc.MAGIC)

    print('\nRound trip through the real encoder and decoder')
    # Large enough that the video runs long enough for the audio to hold bytes.
    src = os.path.join(WORK, 'payload.bin')
    with open(src, 'wb') as f:
        f.write(rng.bytes(40_000_000))
    archive = ve.create_archive([src])

    vid = os.path.join(WORK, 'v8.mp4')
    msgs = []
    ve.encode_bytes_to_video(archive, vid, progress=msgs.append, profile=p)
    got = vd.decode_video_to_bytes(vid, progress=None)
    check('v8 recovers the archive byte-identically', got == archive)

    hdr_line = [m for m in msgs if m.startswith('Audio channel:')]
    check('the audio track was given real payload', bool(hdr_line),
          '; '.join(m for m in msgs if 'Audio' in m))
    carried = 0
    if hdr_line:
        carried = int(hdr_line[0].split()[2].replace(',', ''))
        check(f'{carried:,} payload bytes travelled in the audio', carried > 0)

    print('\nLosing the audio track is reported, not silently tolerated')
    stripped = os.path.join(WORK, 'v8_noaudio.mp4')
    exe = paths.ffmpeg_exe()
    kw = dict(stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if sys.platform == 'win32':
        kw['creationflags'] = subprocess.CREATE_NO_WINDOW
    subprocess.run([exe, '-y', '-i', vid, '-an', '-c:v', 'copy', stripped], **kw)
    if carried and os.path.exists(stripped):
        try:
            vd.decode_video_to_bytes(stripped, progress=None)
            check('a video whose audio was stripped raises', False,
                  'it returned data instead')
        except Exception as exc:
            check('a video whose audio was stripped raises',
                  'audio' in str(exc).lower(), str(exc)[:80])

    print('\nThe audio payload can be turned off')
    vid2 = os.path.join(WORK, 'v8_noap.mp4')
    msgs2 = []
    ve.encode_bytes_to_video(archive, vid2, progress=msgs2.append,
                             profile=p, audio_payload=False)
    got2 = vd.decode_video_to_bytes(vid2, progress=None)
    check('v8 without audio payload round-trips', got2 == archive)
    check('and it puts nothing in the track',
          not any(m.startswith('Audio channel:') for m in msgs2))

    print('\nOlder profiles are unaffected')
    vid3 = os.path.join(WORK, 'v5.mp4')
    ve.encode_bytes_to_video(archive, vid3, progress=None,
                             profile=vc.PROFILE_V5)
    check('v5 still round-trips',
          vd.decode_video_to_bytes(vid3, progress=None) == archive)

    shutil.rmtree(WORK, ignore_errors=True)
    print()
    if fails:
        print(f'{fails} check(s) FAILED')
        return 1
    print('All v8 checks passed.')
    return 0


if __name__ == '__main__':
    sys.exit(main())

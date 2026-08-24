"""
Playlist round trip, without spending a video upload.

Creating a playlist and adding items to it costs a fraction of what uploading
a video does, so this validates the whole playlist path against videos that are
already on the channel: create the playlist, add the shards to it (deliberately
in the wrong order), read it back by link, and decode the archive from that one
link.

Pass the video ids of an existing shard set, lowest index first:

    python bench/test_playlist.py Seehpj5J5_s qjBNRIryxUg Fc798hU7ZyU --size 96

--size is the payload size in MB used to create them, so the recovered bytes
can be checked against a regenerated copy.
"""
import hashlib
import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

SCRATCH = os.environ.get(
    'VIDCOMPILER_SCRATCH',
    os.path.join(os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.abspath(__file__)))), 'bench_scratch'))
os.makedirs(SCRATCH, exist_ok=True)
tempfile.tempdir = SCRATCH

import numpy as np  # noqa: E402

import shards  # noqa: E402
import youtube_api as yt  # noqa: E402
from youtube_api import download_video, fetch_description  # noqa: E402

fails = 0


def check(name, cond):
    global fails
    print(f'  {"PASS" if cond else "FAIL"}  {name}')
    if not cond:
        fails += 1


def main(video_ids, size_mb):
    if not yt.can_manage_playlists():
        print('The saved token cannot create playlists. Run:')
        print('    python bench/yt_auth.py --manage')
        return 1

    print(f'{len(video_ids)} videos, {size_mb} MB payload\n')

    print('[1] create a playlist and add the videos in REVERSE order')
    pid = yt.create_playlist(
        f'vidcompiler playlist test {time.strftime("%Y%m%d-%H%M%S")}',
        'Order deliberately reversed; the manifest should sort it out.')
    check('playlist created', bool(pid))
    for i, vid in enumerate(reversed(video_ids)):
        yt.add_to_playlist(pid, vid, position=i)
    url = yt.playlist_url(pid)
    print(f'  {url}')

    print('\n[2] read it back by link')
    got = yt.expand_playlist(url)
    check(f'playlist lists {len(video_ids)} videos', len(got) == len(video_ids))
    ids = [u.rsplit('=', 1)[-1] for u in got]
    check('returned in the order they were added (reversed)',
          ids == list(reversed(video_ids)))

    print('\n[3] decode the archive from the playlist link alone')
    work = tempfile.mkdtemp(prefix='pltest_')
    try:
        paths, descs = [], []
        for i, u in enumerate(got):
            p = os.path.join(work, f'v{i}.mp4')
            descs.append(fetch_description(u))
            download_video(u, p, progress=None)
            paths.append(p)
            print(f'  downloaded {i + 1}/{len(got)}')

        out = os.path.join(work, 'out')
        t0 = time.perf_counter()
        names = shards.decode_shards_to_files(paths, out, descs, progress=None,
                                              max_workers=len(paths))
        print(f'  decoded in {time.perf_counter() - t0:.0f}s -> {names}')

        rng = np.random.default_rng(2024)
        want = hashlib.sha256(
            rng.integers(0, 256, int(size_mb * 1024 * 1024),
                         dtype=np.uint8).tobytes()).hexdigest()
        h = hashlib.sha256()
        with open(os.path.join(out, 'payload.bin'), 'rb') as f:
            for b in iter(lambda: f.read(1 << 20), b''):
                h.update(b)
        check('archive reassembled byte-identically despite reversed order',
              h.hexdigest() == want)
    finally:
        shutil.rmtree(work, ignore_errors=True)

    print(f'\n{"PLAYLIST TESTS PASSED" if fails == 0 else f"{fails} FAILED"}')
    print(f'(playlist left in place: {url})')
    return 1 if fails else 0


if __name__ == '__main__':
    args = [a for a in sys.argv[1:] if not a.startswith('--')]
    size = 96.0
    if '--size' in sys.argv:
        size = float(sys.argv[sys.argv.index('--size') + 1])
        args = [a for a in args if a != str(size) and a != f'{size:g}']
    if not args:
        print(__doc__)
        sys.exit(2)
    sys.exit(main(args, size))

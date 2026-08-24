"""
The file explorer, end to end, at several file sizes.

A real waitress server, real HTTP, real multipart uploads streamed from disk,
real SSE progress, and the real pipeline underneath: tar, AES-256-GCM,
Reed-Solomon, YUV frames, H.264, and all of it again backwards.

**What is substituted:** the four YouTube transport calls -- upload, download,
description fetch, and playlist expansion -- are replaced by a local directory
standing in for the channel. Nothing else is faked. The videos written to that
directory are the same H.264 files that would have been uploaded, and they are
decoded from disk exactly as they would be after a download.

That substitution is not a way of dodging the real thing. Uploading a
gigabyte measured 1441 s against the live service and the daily quota caps how
many of these can run at all; `bench/bench_youtube.py` covers the network path
against real YouTube at sizes where that is practical. This covers the
explorer's own logic at sizes where the network would make it impossible.

Sizes are chosen to cross the behavioural boundaries rather than to look
impressive: below the sharding threshold, just above it, and far enough above
to force the maximum shard count and a playlist.

Usage: python bench/test_explorer_sizes.py [sizes_in_MB ...]
"""
import hashlib
import json
import os
import shutil
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import requests

# Point everything at a roomy disk before the app resolves its paths.
WORK = os.environ.get('VIDCOMPILER_BENCH_WORK', 'E:/vid_compiler/benchwork')
_data = os.path.join(WORK, 'explorer_test_data')
shutil.rmtree(_data, ignore_errors=True)
os.makedirs(_data, exist_ok=True)
os.environ['VIDCOMPILER_DATA'] = _data
os.environ['VIDCOMPILER_SCRATCH'] = os.path.join(_data, 'scratch')

import youtube_api as yt                                          # noqa: E402

# --------------------------------------------------------------------------
# The stand-in channel
# --------------------------------------------------------------------------
CHANNEL = os.path.join(_data, 'fake_channel')
os.makedirs(CHANNEL, exist_ok=True)
_uploads = {}          # video_id -> {'path', 'description'}
_playlists = {}        # playlist_id -> [video_id, ...]
_counter = [0]
_lock = threading.Lock()


def fake_upload(video_path, title='Data Archive', progress=None):
    with _lock:
        _counter[0] += 1
        # Exactly 11 characters: extract_video_id matches YouTube's real id
        # length and rejects anything else, so a shorter stand-in would fail
        # inside the code under test rather than in the substitution.
        vid = f'vid{_counter[0]:08d}'
    dest = os.path.join(CHANNEL, vid + '.mp4')
    shutil.copyfile(video_path, dest)
    sidecar = video_path + '.sidecar'
    desc = open(sidecar).read() if os.path.exists(sidecar) else ''
    _uploads[vid] = {'path': dest, 'description': desc}
    if progress:
        progress(f'Uploaded {os.path.basename(video_path)}.')
    return vid, f'https://www.youtube.com/watch?v={vid}'


def fake_download(url, output_path, progress=None):
    vid = yt.extract_video_id(url)
    shutil.copyfile(_uploads[vid]['path'], output_path)
    return output_path


def fake_description(url):
    return _uploads[yt.extract_video_id(url)]['description']


def fake_create_playlist(title, description='', progress=None):
    with _lock:
        _counter[0] += 1
        pid = f'PL{_counter[0]:010d}'      # 'list=' needs 10+ characters
    _playlists[pid] = []
    return pid


def fake_add_to_playlist(playlist_id, video_id, position=None):
    items = _playlists[playlist_id]
    if position is None:
        items.append(video_id)
    else:
        items.insert(position, video_id)


def fake_expand_playlist(url, progress=None):
    pid = yt.extract_playlist_id(url)
    return [f'https://www.youtube.com/watch?v={v}' for v in _playlists[pid]]


yt.upload_video = fake_upload
yt.download_video = fake_download
yt.fetch_description = fake_description
yt.create_playlist = fake_create_playlist
yt.add_to_playlist = fake_add_to_playlist
yt.expand_playlist = fake_expand_playlist

import app as webapp                                              # noqa: E402

fails = 0
PORT = int(os.environ.get('VIDCOMPILER_TEST_PORT', '5077'))
BASE = f'http://127.0.0.1:{PORT}'


def check(name, cond):
    global fails
    print(f'  {"PASS" if cond else "FAIL"}  {name}', flush=True)
    if not cond:
        fails += 1


def human(n):
    return f'{n / 1e9:.2f} GB' if n >= 1e9 else f'{n / 1e6:.0f} MB'


def serve():
    """Serve exactly as launcher.py does.

    The settings matter to what is being tested. waitress caps request bodies
    at 1 GB by default, which would reject the larger uploads here -- and
    would be a real product bug if the launcher did not already raise it.
    Copying its arguments means this test fails if that ever regresses,
    instead of passing against a server configured more generously than the
    one that ships.
    """
    from waitress import serve as wserve
    wserve(webapp.app, host='127.0.0.1', port=PORT, threads=8,
           channel_timeout=7200, cleanup_interval=30,
           max_request_body_size=1 << 42, expose_tracebacks=False)


def wait_for_server(timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        try:
            if requests.get(BASE + '/api/fs/stats', timeout=2).status_code == 200:
                return True
        except requests.RequestException:
            time.sleep(0.3)
    return False


def make_file(path, nbytes):
    rng = np.random.default_rng(4242)
    h = hashlib.sha256()
    left = nbytes
    with open(path, 'wb', buffering=1 << 20) as f:
        while left > 0:
            block = rng.bytes(min(64 << 20, left))
            f.write(block)
            h.update(block)
            left -= len(block)
    return h.hexdigest()


def sha_of(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for blk in iter(lambda: f.read(1 << 20), b''):
            h.update(blk)
    return h.hexdigest()


def follow(job_id, label):
    """Drain the SSE stream. Returns (ok, last_message, elapsed)."""
    t0 = time.perf_counter()
    last = ''
    with requests.get(f'{BASE}/status/{job_id}', stream=True,
                      timeout=(10, 3600)) as r:
        for line in r.iter_lines(decode_unicode=True):
            if not line or not line.startswith('data: '):
                continue
            ev = json.loads(line[6:])
            if ev['type'] == 'progress':
                last = ev['message']
            elif ev['type'] == 'done':
                return True, last, time.perf_counter() - t0
            elif ev['type'] == 'error':
                return False, ev['message'], time.perf_counter() - t0
    return False, f'{label}: stream ended without a result', time.perf_counter() - t0


# --------------------------------------------------------------------------
def run_size(mb, encrypt):
    nbytes = int(mb * 1e6)
    name = f'{"sealed" if encrypt else "plain"}_{mb}MB.bin'
    passphrase = 'a realistic passphrase for the size test' if encrypt else ''
    src = os.path.join(WORK, name)

    print(f'\n--- {human(nbytes)}, {"encrypted" if encrypt else "plain"} ---',
          flush=True)
    src_sha = make_file(src, nbytes)

    try:
        # ------------------------------------------------------------ upload
        t0 = time.perf_counter()
        with open(src, 'rb') as fh:
            data = {'passphrase': passphrase} if encrypt else {}
            r = requests.post(f'{BASE}/api/fs/upload',
                              files={'file': (name, fh,
                                              'application/octet-stream')},
                              data=data, timeout=(10, 3600))
        check(f'{human(nbytes)}: upload accepted', r.status_code == 200)
        job = r.json().get('job_id')
        ok, msg, _ = follow(job, 'upload')
        check(f'{human(nbytes)}: stored ({msg[:44]})', ok)
        if not ok:
            return
        t_store = time.perf_counter() - t0

        # ------------------------------------------------------------- index
        entries = requests.get(f'{BASE}/api/fs/list', timeout=30).json()['entries']
        entry = next((e for e in entries if e['name'] == name), None)
        check(f'{human(nbytes)}: appears in the listing', entry is not None)
        if entry is None:
            return
        check(f'{human(nbytes)}: size recorded exactly',
              entry['size'] == nbytes)
        check(f'{human(nbytes)}: encryption flag correct',
              entry['encrypted'] == encrypt)
        check(f'{human(nbytes)}: a link was stored',
              entry['url'].startswith('https://'))
        if entry['shards'] > 1:
            check(f'{human(nbytes)}: {entry["shards"]} shards collected into '
                  f'one playlist link', 'playlist?list=' in entry['url'])

        # --------------------------------------------------- wrong passphrase
        if encrypt:
            r = requests.post(f'{BASE}/api/fs/fetch/{entry["id"]}',
                              json={'passphrase': 'not the right one'},
                              timeout=60)
            check(f'{human(nbytes)}: wrong passphrase refused before any '
                  f'download', r.status_code == 401)

        # ------------------------------------------------------------- fetch
        t0 = time.perf_counter()
        r = requests.post(f'{BASE}/api/fs/fetch/{entry["id"]}',
                          json={'passphrase': passphrase}, timeout=60)
        check(f'{human(nbytes)}: fetch started', r.status_code == 200)
        body = r.json()
        if 'job_id' in body:
            ok, msg, _ = follow(body['job_id'], 'fetch')
            check(f'{human(nbytes)}: fetched and decoded', ok)
            if not ok:
                print(f'      {msg}', flush=True)
                return
        t_fetch = time.perf_counter() - t0

        # ---------------------------------------------------------- download
        got = os.path.join(WORK, 'got_' + name)
        with requests.get(f'{BASE}/api/fs/download/{entry["id"]}',
                          stream=True, timeout=(10, 3600)) as r:
            check(f'{human(nbytes)}: download served', r.status_code == 200)
            with open(got, 'wb') as f:
                for chunk in r.iter_content(1 << 20):
                    f.write(chunk)
        check(f'{human(nbytes)}: recovered byte-identically',
              sha_of(got) == src_sha)
        os.unlink(got)

        # ------------------------------------------------- second open is fast
        t0 = time.perf_counter()
        r = requests.post(f'{BASE}/api/fs/fetch/{entry["id"]}',
                          json={'passphrase': passphrase}, timeout=60)
        t_cached = time.perf_counter() - t0
        check(f'{human(nbytes)}: second open is served from cache '
              f'({t_cached * 1000:.0f} ms vs {t_fetch:.0f} s)',
              r.json().get('cached') is True)

        print(f'      store {t_store:.1f}s | fetch {t_fetch:.1f}s | '
              f'{entry["shards"]} video(s) | '
              f'{entry["stored_size"] / nbytes:.2f}x uploaded', flush=True)

    finally:
        if os.path.exists(src):
            os.unlink(src)


def main():
    sizes = [float(a) for a in sys.argv[1:]] or [1, 10, 100, 500, 1000]

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    if not wait_for_server():
        print('FAIL: the server never came up')
        return 1
    print(f'Server on {BASE}, channel at {CHANNEL}')

    # Alternate so both paths are covered without doubling the run time; the
    # largest size is encrypted, because that is the expensive combination.
    for i, mb in enumerate(sizes):
        run_size(mb, encrypt=(i % 2 == 1 or mb == max(sizes)))

    print('\nFinal state')
    s = requests.get(f'{BASE}/api/fs/stats', timeout=30).json()
    print(f'  {s["files"]} files, {s["bytes"] / 1e6:.0f} MB of content, '
          f'{s["stored"] / 1e6:.0f} MB uploaded, index {s["index_bytes"] / 1024:.0f} KB')
    check('the index stayed small next to the content it indexes',
          s['index_bytes'] < 200_000)
    check('every file is still listed', s['files'] == len(sizes))

    print()
    if fails:
        print(f'{fails} check(s) FAILED')
        return 1
    print('All explorer size checks passed.')
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    finally:
        shutil.rmtree(_data, ignore_errors=True)

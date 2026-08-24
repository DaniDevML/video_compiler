import json
import os
import queue
import shutil
import tempfile
import threading
import uuid
import zipfile
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

# Point numba's JIT cache to a writable directory so the compiled kernels
# survive across Python restarts (avoids the ~30s cold-start penalty).
_numba_cache = os.path.join(os.path.expanduser('~'), '.cache', 'numba_vidcompiler')
os.makedirs(_numba_cache, exist_ok=True)
os.environ.setdefault('NUMBA_CACHE_DIR', _numba_cache)

from flask import Flask, Response, jsonify, request, send_from_directory, send_file

import paths

app = Flask(__name__, static_folder=paths.resource('static'))

# No hard cap on the upload. A 512 MB limit contradicted the whole point of the
# tool -- a 1 GB file was rejected with a 413 before anything else ran. Set
# VIDCOMPILER_MAX_UPLOAD_MB to reinstate one.
_max_mb = os.environ.get('VIDCOMPILER_MAX_UPLOAD_MB')
app.config['MAX_CONTENT_LENGTH'] = int(_max_mb) * 1024 * 1024 if _max_mb else None

# Everything transient lands here: the uploaded copy, the encoded video (about
# 3x the payload), and the downloaded copy on the way back -- roughly 7x the
# payload, which is far more than a system temp directory usually has room for.
SCRATCH = paths.scratch_dir()
tempfile.tempdir = SCRATCH
os.environ['TMP'] = os.environ['TEMP'] = os.environ['TMPDIR'] = SCRATCH


def scratch_free_gb() -> float:
    return shutil.disk_usage(SCRATCH).free / 1e9

# In-memory job registry  {job_id: {'q': Queue, 'result': dict}}
_jobs: dict = {}
_jobs_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Job helpers
# ---------------------------------------------------------------------------

def _new_job() -> str:
    job_id = str(uuid.uuid4())
    with _jobs_lock:
        _jobs[job_id] = {'q': queue.Queue(), 'result': {}}
    return job_id


def _job_progress(job_id: str, msg: str):
    with _jobs_lock:
        q = _jobs[job_id]['q']
    q.put({'type': 'progress', 'message': msg})


def _job_done(job_id: str, **kwargs):
    with _jobs_lock:
        _jobs[job_id]['result'].update(kwargs)
        _jobs[job_id]['q'].put({'type': 'done', **kwargs})


def _job_error(job_id: str, msg: str):
    with _jobs_lock:
        _jobs[job_id]['q'].put({'type': 'error', 'message': msg})


# ---------------------------------------------------------------------------
# Background workers
# ---------------------------------------------------------------------------

def _keep_on_failure(job_id: str, videos: list) -> None:
    """Preserve encoded videos when the upload is what failed.

    Encoding a large archive costs minutes, and the usual upload failures --
    a daily quota, a dropped connection -- are worth retrying against rather
    than re-encoding from scratch.
    """
    for i, src in enumerate(videos):
        if not src or not os.path.exists(src):
            continue
        kept = os.path.join(SCRATCH, f'encoded_{job_id[:8]}_{i}.mp4')
        try:
            shutil.move(src, kept)
            if os.path.exists(src + '.sidecar'):
                shutil.move(src + '.sidecar', kept + '.sidecar')
            _job_progress(job_id, f'Upload failed; the encoded video was kept '
                                  f'at {kept}')
        except OSError:
            pass


def _upload_payload(payload: bytes, title: str, progress, videos: list) -> dict:
    """Encode a payload into videos, upload them, and collect the links.

    Shared by the codec page and the file explorer -- the pipelined
    encode/upload below is the part worth not having twice. `videos` is the
    caller's list, appended to as each file appears, so a failed upload can
    keep the encoded videos rather than throwing minutes of work away.
    """
    import shards
    import youtube_api as yt

    n = shards.suggest_shard_count(len(payload))
    progress(f'Payload: {len(payload):,} bytes -> '
             f'{n} video{"s" if n > 1 else ""}.')

    out_dir = tempfile.mkdtemp(prefix='enc_')

    def enc_progress(msg):
        # Frame counters from several concurrent encodes are noise; the
        # milestones around them are worth showing.
        if n > 1 and 'frame ' in msg:
            return
        progress(msg)

    def send(job):
        vid, url = yt.upload_video(
            job['path'],
            title=title if n == 1 else f'{title} [{job["index"] + 1}/{n}]',
            progress=progress if n == 1 else None)
        progress(f'Uploaded video {job["index"] + 1} of {n}.')
        return dict(index=job['index'], url=url, video_id=vid)

    if n == 1:
        jobs = shards.encode_bytes_to_shards(
            payload, out_dir, n_shards=1, progress=enc_progress,
            max_workers=1)
        videos.append(jobs[0]['path'])
        results = [send(jobs[0])]
    else:
        # Upload each shard the moment it finishes encoding, rather than
        # waiting for every encode to finish first. Uploading dominates,
        # so starting the first one ~25 s in instead of ~95 s in takes
        # that time off the total outright.
        progress(f'Encoding and uploading {n} videos in parallel...')
        with ThreadPoolExecutor(max_workers=n) as up_pool:
            pending = []
            for job in shards.iter_encoded_shards(
                    payload, out_dir, n_shards=n, progress=enc_progress,
                    max_workers=min(n, shards.encode_workers())):
                videos.append(job['path'])
                progress(f'Encoded video {job["index"] + 1} of {n}; '
                         f'upload started.')
                pending.append(up_pool.submit(send, job))
            results = [f.result() for f in pending]

    # Measured before cleanup: how much was actually pushed to YouTube, which
    # is the number that tells you what the expansion cost you.
    stored = 0
    for v in videos:
        try:
            stored += os.path.getsize(v)
        except OSError:
            pass

    results.sort(key=lambda r: r['index'])
    urls = [r['url'] for r in results]
    video_ids = [r['video_id'] for r in results]

    # A single link beats a list of links the user must not lose. The
    # playlist also records the order, which is what the decoder needs.
    playlist = None
    if n > 1:
        try:
            pid = yt.create_playlist(
                title,
                'Encoded data archive split across several videos. '
                'Paste this playlist link into VidCompiler to decode.',
                progress=progress)
            for i, vid in enumerate(video_ids):
                yt.add_to_playlist(pid, vid, position=i)
            playlist = yt.playlist_url(pid)
            progress('Playlist created. This one link is all you need.')
        except Exception as exc:
            progress(f'Could not create a playlist ({exc.__class__.__name__}). '
                     'Every video URL below is needed to decode - keep '
                     'them together.')

    return dict(urls=urls, video_ids=video_ids, playlist=playlist,
                shards=n, stored_size=stored)


def _encode_worker(job_id: str, file_paths: list, title: str):
    progress = lambda m: _job_progress(job_id, m)
    videos: list = []
    uploaded = False
    try:
        from video_encoder import create_archive

        progress('Creating archive...')
        archive = create_archive(file_paths)

        out = _upload_payload(archive, title, progress, videos)
        uploaded = True

        _job_done(job_id, url=out['playlist'] or out['urls'][0],
                  urls=out['urls'], playlist=out['playlist'],
                  video_id=out['video_ids'][0], video_ids=out['video_ids'],
                  shards=out['shards'])

    except Exception as exc:
        if not uploaded:
            _keep_on_failure(job_id, videos)
            videos = []
        _job_error(job_id, str(exc))
    finally:
        # Cleanup must never raise. An exception here runs after the result has
        # been queued but before the thread returns, and it killed the worker
        # silently -- the browser then waited on a status stream that would
        # never produce a result.
        for v in videos:
            for leftover in (v, v + '.sidecar'):
                if leftover and os.path.exists(leftover):
                    try:
                        os.unlink(leftover)
                    except OSError as e:
                        _job_progress(job_id, f'Note: could not remove a '
                                              f'temporary file '
                                              f'({e.__class__.__name__}).')
        for fp in file_paths:
            try:
                shutil.rmtree(fp) if os.path.isdir(fp) else os.unlink(fp)
            except Exception:
                pass


def _decode_worker(job_id: str, urls: list):
    progress = lambda m: _job_progress(job_id, m)
    tmp_dir = None
    try:
        import shards
        import youtube_api as yt
        from youtube_api import download_video, fetch_description

        # A playlist link stands in for the whole set, in order.
        if len(urls) == 1 and yt.is_playlist_url(urls[0]):
            urls = yt.expand_playlist(urls[0], progress=progress)

        n = len(urls)
        tmp_dir = tempfile.mkdtemp(prefix='dec_')

        def fetch(i):
            path = os.path.join(tmp_dir, f'v{i}.mp4')
            desc = fetch_description(urls[i])
            download_video(urls[i], path, progress=None)
            progress(f'Downloaded video {i + 1} of {n}.')
            return i, path, desc

        if n == 1:
            progress('Downloading video...')
            got = [fetch(0)]
        else:
            progress(f'Downloading {n} videos in parallel...')
            with ThreadPoolExecutor(max_workers=n) as ex:
                got = sorted(ex.map(fetch, range(n)))

        paths = [p for _, p, _ in got]
        descs = [d for _, _, d in got]

        out_dir = tempfile.mkdtemp(prefix='out_')
        names = shards.decode_shards_to_files(
            paths, out_dir, descs, progress=progress,
            max_workers=min(n, 4))

        # Zip everything up for browser download
        zip_path = out_dir + '_decoded.zip'
        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
            for root, dirs, files in os.walk(out_dir):
                for fname in files:
                    abs_path = os.path.join(root, fname)
                    arc_name = os.path.relpath(abs_path, out_dir)
                    zf.write(abs_path, arc_name)

        _job_done(job_id, names=names, zip_path=zip_path)

    except Exception as exc:
        _job_error(job_id, str(exc))
    finally:
        if tmp_dir and os.path.isdir(tmp_dir):
            shutil.rmtree(tmp_dir, ignore_errors=True)


@app.route('/')
def index():
    return send_from_directory(paths.resource('static'), 'explorer.html')


@app.route('/codec')
def codec_tool():
    """The raw encode/decode tool, for working with links directly."""
    return send_from_directory(paths.resource('static'), 'index.html')


@app.route('/encode', methods=['POST'])
def encode():
    files = request.files.getlist('files')
    if not files or (len(files) == 1 and files[0].filename == ''):
        return jsonify({'error': 'No files provided'}), 400

    title = request.form.get('title', 'Data Archive').strip() or 'Data Archive'

    # Save uploaded files preserving relative paths
    tmp_dir = tempfile.mkdtemp()
    top_level_paths = set()

    for f in files:
        rel = f.filename.replace('\\', '/')
        dest = os.path.normpath(os.path.join(tmp_dir, rel))
        # Guard against path traversal
        if not dest.startswith(tmp_dir):
            return jsonify({'error': 'Invalid file path'}), 400
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        f.save(dest)
        # Track top-level item
        top = os.path.join(tmp_dir, rel.split('/')[0])
        top_level_paths.add(top)

    job_id = _new_job()
    t = threading.Thread(
        target=_encode_worker,
        args=(job_id, list(top_level_paths), title),
        daemon=True,
    )
    t.start()
    return jsonify({'job_id': job_id})


def _parse_urls(data: dict) -> list:
    """Pull a list of video URLs out of a decode request.

    Accepts a list under "urls", or a single "url" that may itself hold several
    separated by newlines or commas -- pasting a block of URLs is the natural
    thing to do once an archive spans several videos.
    """
    raw = data.get('urls')
    if isinstance(raw, str):
        raw = [raw]
    if not raw:
        raw = [data.get('url') or '']

    out = []
    for entry in raw:
        for part in str(entry).replace(',', chr(10)).split(chr(10)):
            part = part.strip()
            if part and part not in out:
                out.append(part)
    return out


@app.route('/decode', methods=['POST'])
def decode():
    data = request.get_json(silent=True) or {}
    urls = _parse_urls(data)
    if not urls:
        return jsonify({'error': 'No YouTube URL provided'}), 400

    job_id = _new_job()
    t = threading.Thread(
        target=_decode_worker,
        args=(job_id, urls),
        daemon=True,
    )
    t.start()
    return jsonify({'job_id': job_id})


@app.route('/status/<job_id>')
def status(job_id: str):
    with _jobs_lock:
        if job_id not in _jobs:
            return jsonify({'error': 'Job not found'}), 404
        q = _jobs[job_id]['q']

    def stream():
        while True:
            try:
                event = q.get(timeout=25)
                yield f'data: {json.dumps(event)}\n\n'
                if event['type'] in ('done', 'error'):
                    break
            except queue.Empty:
                yield 'data: {"type":"ping"}\n\n'

    return Response(
        stream(),
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'X-Accel-Buffering': 'no',
        },
    )


@app.route('/download/<job_id>')
def download(job_id: str):
    with _jobs_lock:
        if job_id not in _jobs:
            return jsonify({'error': 'Job not found'}), 404
        result = _jobs[job_id].get('result', {})

    zip_path = result.get('zip_path')
    if not zip_path or not os.path.exists(zip_path):
        return jsonify({'error': 'No download ready yet'}), 404

    return send_file(
        zip_path,
        as_attachment=True,
        download_name='decoded_files.zip',
        mimetype='application/zip',
    )


@app.route('/api/creds_ok')
def creds_ok():
    """Return whether a valid client_secrets.json is already present."""
    path = paths.data('client_secrets.json')
    try:
        with open(path, 'r') as f:
            data = json.load(f)
        # Minimal check: must have the standard OAuth structure
        ok = isinstance(data, dict) and any(
            k in data and isinstance(data[k], dict) and 'client_id' in data[k]
            for k in ('installed', 'web')
        )
    except Exception:
        ok = False
    return jsonify({'ok': ok})


@app.route('/api/upload_creds', methods=['POST'])
def upload_creds():
    """Accept a dragged-in client_secrets.json and save it next to app.py."""
    f = request.files.get('file')
    if not f:
        return jsonify({'error': 'No file received'}), 400
    try:
        data = json.loads(f.read())
        ok = isinstance(data, dict) and any(
            k in data and isinstance(data[k], dict) and 'client_id' in data[k]
            for k in ('installed', 'web')
        )
        if not ok:
            return jsonify({'error': (
                'This JSON file doesn\'t look like a Google OAuth credentials file. '
                'Make sure you downloaded the right file from Google Cloud Console '
                '(OAuth 2.0 Client ID → Desktop App).'
            )}), 400
    except Exception:
        return jsonify({'error': 'That file isn\'t valid JSON. Please re-download it from Google Cloud Console.'}), 400

    dest = paths.data('client_secrets.json')
    with open(dest, 'w') as out:
        json.dump(data, out)
    return jsonify({'ok': True})


# ---------------------------------------------------------------------------
# File explorer
#
# The illusion is that these files are stored here. They are not: every route
# below moves rows in a SQLite index, and the bytes live on YouTube. The two
# workers are the only places that touch real data, and both are transient.
# ---------------------------------------------------------------------------

import mimetypes                                                    # noqa: E402

import crypto_box                                                   # noqa: E402
import filecache                                                    # noqa: E402
import library                                                      # noqa: E402


def _guess_mime(name: str) -> str:
    return mimetypes.guess_type(name)[0] or 'application/octet-stream'


def _fs_upload_worker(job_id: str, tmp_dir: str, src: str, name: str,
                      parent_id, passphrase: str):
    progress = lambda m: _job_progress(job_id, m)
    videos: list = []
    uploaded = False
    try:
        import hashlib

        from video_encoder import create_archive

        size = os.path.getsize(src)

        progress('Hashing...')
        digest = hashlib.sha256()
        with open(src, 'rb') as f:
            for block in iter(lambda: f.read(1 << 20), b''):
                digest.update(block)
        sha = digest.hexdigest()

        progress('Creating archive...')
        payload = create_archive([src])

        verify_salt = ''
        verify = ''
        if passphrase:
            payload = crypto_box.encrypt(payload, passphrase, progress=progress)
            # Reuse the ciphertext's own salt for the stored verifier. It is
            # public -- it sits in the header inside the video -- so storing it
            # locally gives away nothing that the link does not.
            salt = crypto_box.header_info(payload)['salt']
            verify_salt = salt.hex()
            verify = crypto_box.verifier(passphrase, salt)

        out = _upload_payload(payload, name, progress, videos)
        uploaded = True

        entry = library.add_file(
            parent_id, name, size=size, mime=_guess_mime(name), sha256=sha,
            encrypted=bool(passphrase), verify_salt=verify_salt,
            verifier=verify, url=out['playlist'] or out['urls'][0],
            playlist=out['playlist'] or '', urls=out['urls'],
            video_ids=out['video_ids'], shards=out['shards'],
            stored_size=out['stored_size'])

        progress('Saved to your library.')
        _job_done(job_id, entry=entry)

    except Exception as exc:
        if not uploaded:
            _keep_on_failure(job_id, videos)
            videos = []
        _job_error(job_id, str(exc))
    finally:
        for v in videos:
            for leftover in (v, v + '.sidecar'):
                if leftover and os.path.exists(leftover):
                    try:
                        os.unlink(leftover)
                    except OSError:
                        pass
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _fs_fetch_worker(job_id: str, entry: dict, passphrase: str):
    """Pull a file back from YouTube into the local cache.

    This is the expensive direction -- a download, a full decode, error
    correction and a decryption -- which is exactly why the result is cached
    rather than recomputed for every preview.
    """
    progress = lambda m: _job_progress(job_id, m)
    tmp_dir = None
    out_dir = None
    try:
        import shards
        import youtube_api as yt
        from youtube_api import download_video, fetch_description

        urls = list(entry['urls']) or [entry['url']]
        if len(urls) == 1 and yt.is_playlist_url(urls[0]):
            urls = yt.expand_playlist(urls[0], progress=progress)

        n = len(urls)
        tmp_dir = tempfile.mkdtemp(prefix='fsget_')

        def fetch(i):
            path = os.path.join(tmp_dir, f'v{i}.mp4')
            desc = fetch_description(urls[i])
            download_video(urls[i], path, progress=None)
            progress(f'Downloaded video {i + 1} of {n}.')
            return i, path, desc

        if n == 1:
            progress('Downloading...')
            got = [fetch(0)]
        else:
            progress(f'Downloading {n} videos in parallel...')
            with ThreadPoolExecutor(max_workers=n) as ex:
                got = sorted(ex.map(fetch, range(n)))

        payload = shards.decode_shards_to_bytes(
            [p for _, p, _ in got], [d for _, _, d in got],
            progress=progress, max_workers=min(n, 4))

        if crypto_box.is_encrypted(payload):
            if not passphrase:
                raise RuntimeError('This file is encrypted. A passphrase is '
                                   'needed to open it.')
            payload = crypto_box.decrypt(payload, passphrase, progress=progress)

        import video_decoder as vd
        out_dir = tempfile.mkdtemp(prefix='fsout_')
        names = vd._extract_archive(payload, out_dir, progress)
        del payload

        # Each explorer upload is one file, so the archive holds one member.
        # Walking rather than trusting `names` keeps this correct if a
        # directory ever gets in here.
        found = None
        for root, _dirs, files in os.walk(out_dir):
            for fname in files:
                found = os.path.join(root, fname)
                break
            if found:
                break
        if not found:
            raise RuntimeError(f'The archive decoded but held no file '
                               f'({", ".join(names) or "empty"}).')

        cached = filecache.put(entry['id'], entry['name'], found)
        progress('Ready.')
        _job_done(job_id, entry_id=entry['id'],
                  size=os.path.getsize(cached), cached=True)

    except crypto_box.WrongPassphrase as exc:
        _job_error(job_id, str(exc))
    except Exception as exc:
        _job_error(job_id, str(exc))
    finally:
        for d in (tmp_dir, out_dir):
            if d and os.path.isdir(d):
                shutil.rmtree(d, ignore_errors=True)


def _fs_entry_or_404(entry_id):
    try:
        entry = library.get(int(entry_id))
    except (TypeError, ValueError):
        return None
    return entry


@app.route('/api/fs/list')
def fs_list():
    parent = request.args.get('parent', type=int) or None
    term = (request.args.get('q') or '').strip()
    if term:
        return jsonify({'entries': library.search(term), 'trail': [],
                        'parent': None, 'query': term})
    if parent is not None and not library.get(parent):
        return jsonify({'error': 'No such folder'}), 404
    return jsonify({'entries': library.list_dir(parent),
                    'trail': library.breadcrumbs(parent),
                    'parent': parent, 'query': ''})


@app.route('/api/fs/stats')
def fs_stats():
    s = library.stats()
    s['cache_bytes'] = filecache.size()
    s['free_gb'] = round(scratch_free_gb(), 1)
    return jsonify(s)


@app.route('/api/fs/folders')
def fs_folders():
    """Every folder with its full path, for the "move to" picker."""
    def walk(parent, prefix):
        out = []
        for e in library.list_dir(parent):
            if e['kind'] != 'folder':
                continue
            path = f'{prefix}/{e["name"]}'
            out.append({'id': e['id'], 'path': path})
            out.extend(walk(e['id'], path))
        return out

    return jsonify({'folders': [{'id': 0, 'path': '/'}] + walk(None, '')})


@app.route('/api/fs/folder', methods=['POST'])
def fs_folder():
    data = request.get_json(silent=True) or {}
    try:
        entry = library.mkdir(data.get('parent') or None, data.get('name', ''))
    except Exception as exc:
        return jsonify({'error': str(exc)}), 400
    return jsonify({'entry': entry})


@app.route('/api/fs/upload', methods=['POST'])
def fs_upload():
    f = request.files.get('file')
    if not f or not f.filename:
        return jsonify({'error': 'No file provided'}), 400

    parent = request.form.get('parent', type=int) or None
    if parent is not None and not library.get(parent):
        return jsonify({'error': 'No such folder'}), 404

    # The passphrase arrives, is used, and is discarded when the worker ends.
    # It is never written to the index, the log, or the job record.
    passphrase = request.form.get('passphrase', '')

    name = os.path.basename(f.filename.replace('\\', '/')) or 'file'
    tmp_dir = tempfile.mkdtemp(prefix='fsin_')
    src = os.path.join(tmp_dir, name)
    f.save(src)

    job_id = _new_job()
    threading.Thread(target=_fs_upload_worker,
                     args=(job_id, tmp_dir, src, name, parent, passphrase),
                     daemon=True).start()
    return jsonify({'job_id': job_id})


@app.route('/api/fs/fetch/<int:entry_id>', methods=['POST'])
def fs_fetch(entry_id: int):
    entry = _fs_entry_or_404(entry_id)
    if not entry or entry['kind'] != 'file':
        return jsonify({'error': 'No such file'}), 404

    data = request.get_json(silent=True) or {}
    passphrase = data.get('passphrase', '')

    if entry['encrypted']:
        if not passphrase:
            return jsonify({'error': 'This file is encrypted.',
                            'need_passphrase': True}), 401
        # Check before spending several minutes on a download. Costs one
        # scrypt derivation, which is the same work the real key needs.
        if entry['verifier']:
            salt = bytes.fromhex(entry['verify_salt'])
            if crypto_box.verifier(passphrase, salt) != entry['verifier']:
                return jsonify({'error': 'Wrong passphrase for this file.',
                                'need_passphrase': True}), 401

    cached = filecache.get(entry_id, entry['name'])
    if cached and not data.get('force'):
        return jsonify({'cached': True})

    job_id = _new_job()
    threading.Thread(target=_fs_fetch_worker,
                     args=(job_id, entry, passphrase), daemon=True).start()
    return jsonify({'job_id': job_id})


def _serve_cached(entry: dict, as_attachment: bool):
    cached = filecache.get(entry['id'], entry['name'])
    if not cached:
        return jsonify({'error': 'Not fetched yet', 'need_fetch': True}), 409
    # conditional=True gives byte-range support, which is what lets a cached
    # video or audio file scrub in the browser instead of restarting.
    return send_file(cached, mimetype=entry['mime'] or None,
                     as_attachment=as_attachment,
                     download_name=entry['name'], conditional=True)


@app.route('/api/fs/download/<int:entry_id>')
def fs_download(entry_id: int):
    entry = _fs_entry_or_404(entry_id)
    if not entry or entry['kind'] != 'file':
        return jsonify({'error': 'No such file'}), 404
    return _serve_cached(entry, as_attachment=True)


@app.route('/api/fs/preview/<int:entry_id>')
def fs_preview(entry_id: int):
    entry = _fs_entry_or_404(entry_id)
    if not entry or entry['kind'] != 'file':
        return jsonify({'error': 'No such file'}), 404
    return _serve_cached(entry, as_attachment=False)


@app.route('/api/fs/rename', methods=['POST'])
def fs_rename():
    data = request.get_json(silent=True) or {}
    try:
        entry = library.rename(int(data['id']), data.get('name', ''))
    except KeyError:
        return jsonify({'error': 'No such item'}), 404
    except Exception as exc:
        return jsonify({'error': str(exc)}), 400
    return jsonify({'entry': entry})


@app.route('/api/fs/move', methods=['POST'])
def fs_move():
    data = request.get_json(silent=True) or {}
    try:
        entry = library.move(int(data['id']), data.get('parent') or None)
    except KeyError as exc:
        return jsonify({'error': str(exc)}), 404
    except Exception as exc:
        return jsonify({'error': str(exc)}), 400
    return jsonify({'entry': entry})


@app.route('/api/fs/delete', methods=['POST'])
def fs_delete():
    """Remove from the library, and only on request from YouTube as well.

    Dropping the index row is cheap and reversible: the videos stay unlisted
    on the channel and the links come back in the response, so a mistake can
    be undone by pasting them into the codec page. Deleting the videos cannot
    be undone, so it happens only when explicitly asked for.
    """
    data = request.get_json(silent=True) or {}
    also_remote = bool(data.get('delete_videos'))
    try:
        removed = library.delete(int(data['id']))
    except KeyError:
        return jsonify({'error': 'No such item'}), 404
    except Exception as exc:
        return jsonify({'error': str(exc)}), 400

    for entry in removed:
        filecache.drop(entry['id'], entry['name'])

    links = [e['url'] for e in removed if e['url']]
    errors = []
    if also_remote:
        import youtube_api as yt
        for entry in removed:
            for vid in entry['video_ids']:
                try:
                    yt.delete_video(vid)
                except Exception as exc:
                    errors.append(f'{vid}: {exc.__class__.__name__}')
    return jsonify({'ok': True, 'removed': len(removed), 'links': links,
                    'delete_errors': errors})


@app.route('/api/fs/cache', methods=['DELETE'])
def fs_cache_purge():
    """Empty the local cache, including plaintext of encrypted files."""
    return jsonify({'freed': filecache.purge()})


def _startup_warmup():
    """
    Warm up ALL JIT kernels and probe hardware encoder BEFORE accepting requests.
    Covers: galois RS, numba frame gen/decode, hardware encoder probe.
    """
    import os
    import numpy as np

    print('[startup] Warming up JIT kernels (galois)...', flush=True)

    # galois RS kernels
    from video_encoder import rs_encode, _get_encoder
    from video_decoder import rs_decode
    dummy_data = os.urandom(215 * 4)
    enc = rs_encode(dummy_data)
    rs_decode(enc, len(dummy_data))

    # 3. Hardware encoder probe (result is cached)
    codec, params = _get_encoder()

    print(f'[startup] All JIT ready. Encoder: {codec}', flush=True)


if __name__ == '__main__':
    # Only run warmup when not inside Werkzeug reloader child process
    if os.environ.get('WERKZEUG_RUN_MAIN') != 'true':
        _startup_warmup()
    app.run(host='0.0.0.0', port=5000, debug=True, use_reloader=True, threaded=True)

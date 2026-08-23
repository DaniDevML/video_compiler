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

app = Flask(__name__, static_folder='static')

# No hard cap on the upload. A 512 MB limit contradicted the whole point of the
# tool -- a 1 GB file was rejected with a 413 before anything else ran. Set
# VIDCOMPILER_MAX_UPLOAD_MB to reinstate one.
_max_mb = os.environ.get('VIDCOMPILER_MAX_UPLOAD_MB')
app.config['MAX_CONTENT_LENGTH'] = int(_max_mb) * 1024 * 1024 if _max_mb else None

# Scratch directory. Everything transient lands here: the uploaded copy, the
# encoded video (about 3x the payload), and the downloaded copy on the way
# back. That is roughly 7x the payload, which is far more than a system temp
# directory usually has room for -- so it defaults to a folder beside the app
# rather than to the system drive. Override with VIDCOMPILER_SCRATCH.
SCRATCH = os.environ.get(
    'VIDCOMPILER_SCRATCH',
    os.path.join(os.path.dirname(os.path.abspath(__file__)), '.scratch'))
os.makedirs(SCRATCH, exist_ok=True)
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

def _keep_on_failure(job_id: str, paths: list) -> None:
    """Preserve encoded videos when the upload is what failed.

    Encoding a large archive costs minutes, and the usual upload failures --
    a daily quota, a dropped connection -- are worth retrying against rather
    than re-encoding from scratch.
    """
    for i, src in enumerate(paths):
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


def _encode_worker(job_id: str, file_paths: list, title: str):
    progress = lambda m: _job_progress(job_id, m)
    videos: list = []
    uploaded = False
    try:
        import shards
        from video_encoder import create_archive
        from youtube_api import upload_video

        progress('Creating archive...')
        archive = create_archive(file_paths)
        n = shards.suggest_shard_count(len(archive))
        progress(f'Archive: {len(archive):,} bytes -> '
                 f'{n} video{"s" if n > 1 else ""}.')

        out_dir = tempfile.mkdtemp(prefix='enc_')
        jobs = shards.encode_bytes_to_shards(
            archive, out_dir, n_shards=n,
            progress=progress if n == 1 else lambda m: None,
            max_workers=n)
        videos = [j['path'] for j in jobs]
        if n > 1:
            progress(f'Encoded {n} videos, '
                     f'{sum(j["bytes"] for j in jobs):,} bytes total.')

        # Uploads run concurrently: YouTube throttles each stream rather than
        # the connection, so several at once measured 2.14x the throughput of
        # one. With a single video this is just a direct call.
        def send(j):
            part = (f' ({j["index"] + 1}/{n})' if n > 1 else '')
            vid, url = upload_video(
                j['path'],
                title=title if n == 1 else f'{title} [{j["index"] + 1}/{n}]',
                progress=(lambda m: progress(f'{m}{part}')) if n == 1 else None)
            progress(f'Uploaded video {j["index"] + 1} of {n}.')
            return dict(index=j['index'], url=url, video_id=vid)

        if n == 1:
            results = [send(jobs[0])]
        else:
            progress(f'Uploading {n} videos in parallel...')
            with ThreadPoolExecutor(max_workers=n) as ex:
                results = list(ex.map(send, jobs))
        uploaded = True
        results.sort(key=lambda r: r['index'])

        urls = [r['url'] for r in results]
        if n > 1:
            progress('All videos uploaded. Every URL below is needed to '
                     'decode -- keep them together.')
        _job_done(job_id, url=urls[0], urls=urls,
                  video_id=results[0]['video_id'],
                  video_ids=[r['video_id'] for r in results],
                  shards=n)

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
            for path in (v, v + '.sidecar'):
                if path and os.path.exists(path):
                    try:
                        os.unlink(path)
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
        from youtube_api import download_video, fetch_description

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
    return send_from_directory('static', 'index.html')


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
    path = os.path.join(os.path.dirname(__file__), 'client_secrets.json')
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

    dest = os.path.join(os.path.dirname(__file__), 'client_secrets.json')
    with open(dest, 'w') as out:
        json.dump(data, out)
    return jsonify({'ok': True})


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

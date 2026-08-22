import json
import os
import queue
import tempfile
import threading
import uuid
import zipfile
from pathlib import Path

# Point numba's JIT cache to a writable directory so the compiled kernels
# survive across Python restarts (avoids the ~30s cold-start penalty).
_numba_cache = os.path.join(os.path.expanduser('~'), '.cache', 'numba_vidcompiler')
os.makedirs(_numba_cache, exist_ok=True)
os.environ.setdefault('NUMBA_CACHE_DIR', _numba_cache)

from flask import Flask, Response, jsonify, request, send_from_directory, send_file

app = Flask(__name__, static_folder='static')
app.config['MAX_CONTENT_LENGTH'] = 512 * 1024 * 1024  # 512 MB upload limit

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

def _encode_worker(job_id: str, file_paths: list, title: str):
    progress = lambda m: _job_progress(job_id, m)
    tmp_video = None
    try:
        from video_encoder import encode_files_to_video
        from youtube_api import upload_video

        with tempfile.NamedTemporaryFile(suffix='.mp4', delete=False) as tf:
            tmp_video = tf.name  # close the handle before ffmpeg writes to it
        encode_files_to_video(file_paths, tmp_video, progress=progress)

        _video_id, url = upload_video(tmp_video, title=title, progress=progress)
        _job_done(job_id, url=url, video_id=_video_id)

    except Exception as exc:
        _job_error(job_id, str(exc))
    finally:
        if tmp_video and os.path.exists(tmp_video):
            os.unlink(tmp_video)
        for fp in file_paths:
            try:
                import shutil
                shutil.rmtree(fp) if os.path.isdir(fp) else os.unlink(fp)
            except Exception:
                pass


def _decode_worker(job_id: str, youtube_url: str):
    progress = lambda m: _job_progress(job_id, m)
    tmp_video = None
    try:
        from youtube_api import download_video, fetch_description
        from video_decoder import decode_video_to_files

        with tempfile.NamedTemporaryFile(suffix='.mp4', delete=False) as tf:
            tmp_video = tf.name  # close handle before yt-dlp writes to it
        description = fetch_description(youtube_url)
        tmp_video = download_video(youtube_url, tmp_video, progress=progress)

        out_dir = tempfile.mkdtemp()
        names = decode_video_to_files(tmp_video, out_dir, progress=progress,
                                      description=description)

        # Zip everything up for browser download
        zip_path = out_dir + '_decoded.zip'
        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
            for root, dirs, files in os.walk(out_dir):
                for fname in files:
                    abs_path = os.path.join(root, fname)
                    arc_name = os.path.relpath(abs_path, out_dir)
                    zf.write(abs_path, arc_name)

        _job_done(job_id, zip_path=zip_path, names=names)

    except Exception as exc:
        _job_error(job_id, str(exc))
    finally:
        if tmp_video and os.path.exists(tmp_video):
            os.unlink(tmp_video)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

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


@app.route('/decode', methods=['POST'])
def decode():
    data = request.get_json(silent=True) or {}
    url = (data.get('url') or '').strip()
    if not url:
        return jsonify({'error': 'No YouTube URL provided'}), 400

    job_id = _new_job()
    t = threading.Thread(
        target=_decode_worker,
        args=(job_id, url),
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

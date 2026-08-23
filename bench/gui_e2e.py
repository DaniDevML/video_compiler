"""
End-to-end test through the web app's own HTTP endpoints.

Drives exactly what static/index.html sends: a multipart POST to /encode with
the file under the field name "files", then the SSE stream at /status/<job_id>,
then the same for /decode and /download. Running against the live Flask server
exercises the whole GUI server path -- upload buffering, the job queue, SSE
progress over a long job, and the download packaging -- which is where the
size-dependent behaviour lives.

Usage:
    python bench/gui_e2e.py 1024            # payload size in MB
    python bench/gui_e2e.py 8 --keep        # leave the recovered zip in place
"""
import hashlib
import io
import json
import os
import shutil
import sys
import tempfile
import time
import urllib.request
import uuid
import zipfile

BASE = os.environ.get('VIDCOMPILER_URL', 'http://127.0.0.1:5000')
HERE = os.path.dirname(os.path.abspath(__file__))
SCRATCH = os.environ.get(
    'VIDCOMPILER_SCRATCH',
    os.path.join(os.path.dirname(os.path.dirname(HERE)), 'bench_scratch'))
os.makedirs(SCRATCH, exist_ok=True)
tempfile.tempdir = SCRATCH


def log(m):
    print(f'  {m}', flush=True)


def human(n):
    for u in ('B', 'KB', 'MB', 'GB'):
        if abs(n) < 1024 or u == 'GB':
            return f'{n:.2f} {u}' if u != 'B' else f'{n:.0f} B'
        n /= 1024


def make_payload(path, size_bytes, seed=4242):
    """Same generator as bench_youtube, so payloads are comparable."""
    import numpy as np
    rng = np.random.default_rng(seed)
    h = hashlib.sha256()
    written = 0
    with open(path, 'wb') as f:
        while written < size_bytes:
            n = min(8 << 20, size_bytes - written)
            buf = rng.integers(0, 256, n, dtype=np.uint8).tobytes()
            f.write(buf)
            h.update(buf)
            written += n
    return h.hexdigest()


class _Multipart(io.RawIOBase):
    """Streams a multipart body from disk instead of building it in memory.

    A 1 GB payload cannot be assembled as a bytes object and handed to urllib
    without doubling it in RAM, which is exactly the kind of thing that only
    shows up at size.
    """

    def __init__(self, fields, file_field, file_path, file_name):
        self.boundary = uuid.uuid4().hex
        pre = ''.join(
            f'--{self.boundary}\r\n'
            f'Content-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'
            for k, v in fields.items())
        pre += (f'--{self.boundary}\r\n'
                f'Content-Disposition: form-data; name="{file_field}"; '
                f'filename="{file_name}"\r\n'
                f'Content-Type: application/octet-stream\r\n\r\n')
        self._pre = pre.encode()
        self._post = f'\r\n--{self.boundary}--\r\n'.encode()
        self._path = file_path
        self._size = os.path.getsize(file_path)
        self.length = len(self._pre) + self._size + len(self._post)
        self._fh = None
        self._stage = 0
        self._pos = 0

    def readable(self):
        return True

    def readinto(self, b):
        want = len(b)
        if self._stage == 0:
            chunk = self._pre[self._pos:self._pos + want]
            self._pos += len(chunk)
            if self._pos >= len(self._pre):
                self._stage, self._pos = 1, 0
                self._fh = open(self._path, 'rb')
            b[:len(chunk)] = chunk
            return len(chunk)
        if self._stage == 1:
            chunk = self._fh.read(want)
            if chunk:
                b[:len(chunk)] = chunk
                return len(chunk)
            self._fh.close()
            self._stage, self._pos = 2, 0
        chunk = self._post[self._pos:self._pos + want]
        self._pos += len(chunk)
        b[:len(chunk)] = chunk
        return len(chunk)


def post_encode(file_path, title):
    body = _Multipart({'title': title}, 'files', file_path,
                      os.path.basename(file_path))
    req = urllib.request.Request(
        f'{BASE}/encode', data=io.BufferedReader(body, 1 << 20), method='POST')
    req.add_header('Content-Type',
                   f'multipart/form-data; boundary={body.boundary}')
    req.add_header('Content-Length', str(body.length))
    with urllib.request.urlopen(req, timeout=7200) as r:
        return json.loads(r.read())


def post_decode(urls):
    if isinstance(urls, str):
        urls = [urls]
    data = json.dumps({'urls': urls}).encode()
    req = urllib.request.Request(f'{BASE}/decode', data=data, method='POST',
                                 headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=300) as r:
        return json.loads(r.read())


def stream_job(job_id, quiet_after=6):
    """Follow the SSE job stream the page follows. Returns the done payload."""
    seen = 0
    with urllib.request.urlopen(f'{BASE}/status/{job_id}', timeout=14400) as r:
        for raw in r:
            line = raw.decode('utf-8', 'replace').strip()
            if not line.startswith('data:'):
                continue
            msg = json.loads(line[5:].strip())
            kind = msg.get('type')
            if kind == 'progress':
                seen += 1
                text = msg.get('message', '')
                # the frame counters are noisy; keep the milestones
                if 'frame ' not in text or seen % 25 == 0:
                    log(text)
            elif kind == 'done':
                return msg
            elif kind == 'error':
                raise RuntimeError(msg.get('message', 'unknown error'))
    raise RuntimeError('status stream ended without a result')


def main(mb=8.0, keep=False):
    size = int(mb * 1024 * 1024)
    work = tempfile.mkdtemp(prefix='guie2e_')
    src = os.path.join(work, 'payload.bin')

    try:
        print(f'GUI end-to-end against {BASE}')
        print(f'payload {mb:g} MB, free scratch '
              f'{shutil.disk_usage(SCRATCH).free/1e9:.0f} GB\n')

        log('generating payload...')
        want = make_payload(src, size)
        log(f'sha256 {want[:16]}...')

        t0 = time.perf_counter()
        r = post_encode(src, f'gui e2e {mb:g}MB {time.strftime("%H%M%S")}')
        if 'error' in r:
            raise RuntimeError(f'/encode rejected the upload: {r["error"]}')
        log(f'POST /encode accepted in {time.perf_counter()-t0:.1f}s '
            f'-> job {r["job_id"]}')
        upload_s = time.perf_counter() - t0

        t0 = time.perf_counter()
        done = stream_job(r['job_id'])
        encode_s = time.perf_counter() - t0
        urls = done.get('urls') or ([done['url']] if done.get('url') else [])
        log(f'encode+upload finished in {encode_s:.1f}s -> '
            f'{len(urls)} video(s)')
        for u in urls:
            log(f'  {u}')

        log('waiting for the 1080p rendition on every video...')
        sys.path.insert(0, HERE)
        from bench_youtube import wait_for_1080p
        from youtube_api import extract_video_id
        t0 = time.perf_counter()
        import concurrent.futures as _f
        with _f.ThreadPoolExecutor(max_workers=len(urls)) as ex:
            list(ex.map(lambda u: wait_for_1080p(extract_video_id(u)), urls))
        proc_s = time.perf_counter() - t0

        t0 = time.perf_counter()
        d = post_decode(urls)
        if 'error' in d:
            raise RuntimeError(f'/decode rejected: {d["error"]}')
        done2 = stream_job(d['job_id'])
        decode_s = time.perf_counter() - t0
        log(f'decode finished in {decode_s:.1f}s')

        t0 = time.perf_counter()
        zpath = os.path.join(work, 'recovered.zip')
        with urllib.request.urlopen(f'{BASE}/download/{d["job_id"]}',
                                    timeout=7200) as resp, \
                open(zpath, 'wb') as out:
            shutil.copyfileobj(resp, out, 1 << 20)
        dl_s = time.perf_counter() - t0
        log(f'GET /download -> {human(os.path.getsize(zpath))} in {dl_s:.1f}s')

        with zipfile.ZipFile(zpath) as z:
            names = z.namelist()
            h = hashlib.sha256()
            with z.open(names[0]) as f:
                for blk in iter(lambda: f.read(1 << 20), b''):
                    h.update(blk)
            got = h.hexdigest()

        ok = got == want
        print()
        print(f'  {"BYTES IDENTICAL" if ok else "DATA MISMATCH"}  '
              f'({names[0]}, sha256 {got[:16]}...)')
        print(f'  upload to server {upload_s:6.1f}s')
        print(f'  encode+publish   {encode_s:6.1f}s')
        print(f'  yt processing    {proc_s:6.1f}s')
        print(f'  decode           {decode_s:6.1f}s')
        print(f'  zip download     {dl_s:6.1f}s')
        print(f'  total            {upload_s+encode_s+proc_s+decode_s+dl_s:6.1f}s')
        if keep:
            shutil.move(zpath, os.path.join(SCRATCH, 'recovered.zip'))
        return 0 if ok else 1

    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == '__main__':
    args = [a for a in sys.argv[1:] if not a.startswith('--')]
    sys.exit(main(float(args[0]) if args else 8.0, '--keep' in sys.argv))

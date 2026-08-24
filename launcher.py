"""
launcher.py — the whole product from one double-click.

Starts the server, waits until it is actually answering, opens the browser at
it, and stays up until the window is closed. This is the entry point the
packaged executable runs; `python launcher.py` does the same thing from source.

Notes on the packaged build:

  * The Flask development server is not used. It is single-threaded per
    connection by default and prints to a console a windowed build does not
    have. waitress serves the same app properly, and falls back to Flask's
    server if it is unavailable.

  * Startup is slow the first time regardless: galois compiles its
    Reed-Solomon kernels with numba on first use, which takes tens of seconds.
    The cache is warmed in the background while the browser is already
    showing the page.

  * Port 5000 is often taken (on macOS by AirPlay, on Windows by whatever
    else). A free port is chosen and the browser is pointed at it.
"""

import os
import socket
import sys
import threading
import time
import webbrowser

import paths

HOST = '127.0.0.1'
PREFERRED_PORT = 5000
BANNER = r"""
  VidCompiler
  ------------------------------------------------------------
  Store files inside YouTube videos, and get them back intact.
"""


def find_port(preferred: int = PREFERRED_PORT) -> int:
    """The preferred port if it is free, otherwise any free port."""
    for candidate in (preferred, 0):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind((HOST, candidate))
                return s.getsockname()[1]
            except OSError:
                continue
    raise RuntimeError('No free port available')


def wait_until_serving(port: int, timeout: float = 60.0) -> bool:
    """Block until the server accepts a connection, or give up."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            if s.connect_ex((HOST, port)) == 0:
                return True
        time.sleep(0.2)
    return False


def warm_up():
    """Compile the JIT kernels and probe the encoder, off the critical path.

    Doing this before the browser opens would leave the user staring at
    nothing for half a minute on a cold cache.
    """
    try:
        from video_encoder import rs_encode, _get_encoder
        from video_decoder import rs_decode
        data = os.urandom(215 * 4)
        rs_decode(rs_encode(data), len(data))
        codec, _ = _get_encoder()
        print(f'  ready - video encoder: {codec}', flush=True)
    except Exception as exc:
        print(f'  warm-up skipped: {type(exc).__name__}: {exc}', flush=True)


def serve(port: int):
    """Run the app. Prefers waitress; falls back to Flask's own server."""
    from app import app as flask_app
    try:
        from waitress import serve as waitress_serve
        waitress_serve(flask_app, host=HOST, port=port, threads=8,
                       channel_timeout=7200, cleanup_interval=30,
                       max_request_body_size=1 << 42, expose_tracebacks=False)
    except ImportError:
        flask_app.run(host=HOST, port=port, debug=False, use_reloader=False,
                      threaded=True)


def main() -> int:
    print(BANNER, flush=True)
    print(f'  data directory: {paths.DATA_DIR}', flush=True)
    print(f'  scratch:        {paths.scratch_dir()}', flush=True)

    port = find_port()
    url = f'http://{HOST}:{port}/'

    server = threading.Thread(target=serve, args=(port,), daemon=True)
    server.start()

    if not wait_until_serving(port):
        print('  The server did not start. Nothing was opened.', flush=True)
        input('  Press Enter to close...')
        return 1

    print(f'  serving at {url}', flush=True)
    threading.Thread(target=warm_up, daemon=True).start()

    try:
        webbrowser.open(url)
    except Exception:
        pass
    print('  A browser window should have opened. If not, paste the address '
          'above.', flush=True)
    print('  Close this window to stop the server.', flush=True)

    try:
        while server.is_alive():
            server.join(timeout=1.0)
    except KeyboardInterrupt:
        print('  Shutting down.', flush=True)
    return 0


if __name__ == '__main__':
    sys.exit(main())

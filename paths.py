"""
paths.py — where things live, whether running from source or from a frozen exe.

PyInstaller unpacks a one-file build into a temporary directory and deletes it
on exit, so the two kinds of path have to be kept apart:

  RESOURCE_DIR  read-only files shipped inside the build (the web page, the
                native library). Under a frozen build this is the unpack
                directory; from source it is the project directory.

  DATA_DIR      files that must outlive the process and belong to the user:
                OAuth credentials, the saved token, scratch space. Under a
                frozen build this sits next to the .exe, never inside the
                bundle -- credentials written into the unpack directory would
                be silently discarded when the app closes.
"""

import os
import sys

FROZEN = getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS')

if FROZEN:
    RESOURCE_DIR = sys._MEIPASS
    DATA_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    RESOURCE_DIR = os.path.dirname(os.path.abspath(__file__))
    DATA_DIR = RESOURCE_DIR

# A container mounts its writable volume somewhere unrelated to the code, so
# the data directory has to be settable independently.
DATA_DIR = os.environ.get('VIDCOMPILER_DATA') or DATA_DIR
os.makedirs(DATA_DIR, exist_ok=True)


def resource(*parts) -> str:
    """A read-only file shipped with the application."""
    return os.path.join(RESOURCE_DIR, *parts)


def data(*parts) -> str:
    """A file belonging to the user, beside the executable."""
    return os.path.join(DATA_DIR, *parts)


def scratch_dir() -> str:
    """Working directory for large temporary files.

    A job needs roughly seven times the payload here (the uploaded copy, the
    ~3x encoded video, the downloaded copy), which is more than a system temp
    directory usually has room for.
    """
    p = os.environ.get('VIDCOMPILER_SCRATCH') or data('.scratch')
    os.makedirs(p, exist_ok=True)
    return p


def ffmpeg_exe() -> str:
    """Path to an ffmpeg binary.

    Prefers the binary imageio-ffmpeg carries. That is deliberate: the upload
    quantiser and NVENC preset were tuned against it, and a different build
    encodes the same frames to a noticeably different size -- swapping in the
    system ffmpeg measured 4.14x expansion where the bundled one gives 3.16x,
    which is a third more to upload for no benefit.

    VIDCOMPILER_FFMPEG overrides, and a system ffmpeg is the fallback when the
    bundled one is unavailable.
    """
    import shutil

    override = os.environ.get('VIDCOMPILER_FFMPEG')
    if override and os.path.exists(override):
        return override

    try:
        import imageio_ffmpeg
        exe = imageio_ffmpeg.get_ffmpeg_exe()
        if exe and os.path.exists(exe):
            return exe
    except Exception:
        pass

    found = shutil.which('ffmpeg')
    if found:
        return found
    raise FileNotFoundError('No ffmpeg binary found.')

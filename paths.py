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

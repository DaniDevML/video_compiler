# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for the one-file VidCompiler executable.

Build with:  python build_exe.py     (or: pyinstaller vidcompiler.spec)

The parts that need help:

  native/frame_ops.dll   loaded by ctypes at an explicit path, so PyInstaller
                         cannot see the dependency. Shipped as a data file and
                         found again via sys._MEIPASS.

  ffmpeg                 imageio_ffmpeg carries its own binary in a package
                         subdirectory that the analysis does not follow.

  galois / numba         both build things at import time and pull modules in
                         dynamically; collect them wholesale rather than
                         guessing which pieces are reachable.
"""

import os

from PyInstaller.utils.hooks import collect_all, collect_data_files

datas = [
    ('static', 'static'),
]

# The native pixel/Reed-Solomon library, if it has been built.
for lib in ('native/frame_ops.dll', 'native/frame_ops.so'):
    if os.path.exists(lib):
        datas.append((lib, 'native'))

# ffmpeg binary that imageio_ffmpeg ships.
datas += collect_data_files('imageio_ffmpeg', include_py_files=False)

hiddenimports = [
    'paths', 'shards', 'video_codec', 'video_encoder', 'video_decoder',
    'audio_codec', 'youtube_api', 'app',
    'waitress',
    'engineio.async_drivers.threading',
]

binaries = []
for pkg in ('galois', 'numba', 'llvmlite', 'yt_dlp', 'google_auth_oauthlib',
            'googleapiclient', 'imageio_ffmpeg'):
    try:
        d, b, h = collect_all(pkg)
        datas += d
        binaries += b
        hiddenimports += h
    except Exception:
        pass


a = Analysis(
    ['launcher.py'],
    pathex=[os.path.abspath('.')],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # Nothing here needs a GUI toolkit; excluding them saves a lot of size.
    excludes=['tkinter', 'matplotlib', 'PyQt5', 'PySide2', 'IPython',
              'notebook', 'pytest'],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='VidCompiler',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    # A console is kept on purpose: the launcher prints the address it is
    # serving on, and encoding a large archive is a long enough operation that
    # a visible log is worth more than a tidy taskbar.
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

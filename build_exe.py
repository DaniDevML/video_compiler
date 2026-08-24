"""
build_exe.py — produce dist/VidCompiler.exe

    python build_exe.py

Builds the native library first if a compiler is available, so the executable
ships with the fast pixel and Reed-Solomon paths rather than falling back to
NumPy (which is roughly twenty times slower).
"""

import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def run(cmd, **kw):
    print(f'$ {" ".join(str(c) for c in cmd)}', flush=True)
    return subprocess.run(cmd, cwd=HERE, **kw).returncode


def main() -> int:
    lib = os.path.join(HERE, 'native',
                       'frame_ops.dll' if sys.platform == 'win32'
                       else 'frame_ops.so')
    if not os.path.exists(lib):
        print('Native library missing; building it...')
        run([sys.executable, os.path.join('native', 'build.py')])
    if os.path.exists(lib):
        print(f'Native library: {lib} ({os.path.getsize(lib):,} bytes)')
    else:
        print('WARNING: building without the native library. The executable '
              'will work but the pixel and Reed-Solomon paths fall back to '
              'NumPy, which is far slower.')

    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print('PyInstaller is not installed. Run:  pip install pyinstaller')
        return 1

    for stale in ('build', 'dist'):
        shutil.rmtree(os.path.join(HERE, stale), ignore_errors=True)

    rc = run([sys.executable, '-m', 'PyInstaller', '--noconfirm',
              'vidcompiler.spec'])
    if rc != 0:
        return rc

    exe = os.path.join(HERE, 'dist',
                       'VidCompiler.exe' if sys.platform == 'win32'
                       else 'VidCompiler')
    if not os.path.exists(exe):
        print('Build reported success but no executable was produced.')
        return 1

    print()
    print(f'Built {exe}  ({os.path.getsize(exe) / 1e6:.0f} MB)')
    print()
    print('Put client_secrets.json next to the executable before first use;')
    print('the app writes yt_token.pickle and a .scratch folder there too.')
    return 0


if __name__ == '__main__':
    sys.exit(main())

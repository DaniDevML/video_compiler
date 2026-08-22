"""
native/build.py — Compile frame_ops.c into a shared library.

Run once before starting the app for maximum performance:
    python native/build.py

Tries compilers in order: MSVC cl.exe -> GCC -> Clang.
Falls back gracefully if no compiler is found (app uses NumPy path instead).
"""

import os
import shutil
import subprocess
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC  = os.path.join(_HERE, 'frame_ops.c')
_OUT  = os.path.join(_HERE, 'frame_ops.dll' if sys.platform == 'win32' else 'frame_ops.so')


def _run(cmd, **kw):
    kw.setdefault('capture_output', True)
    r = subprocess.run(cmd, **kw)
    return r.returncode == 0, r.stderr.decode(errors='replace')


def _loadable(path):
    """A DLL that links against toolchain runtime DLLs compiles fine but fails
    to load later, so verify it actually opens before declaring success."""
    import ctypes
    try:
        ctypes.CDLL(path)
        return True
    except OSError:
        return False


def _find_cl():
    """Locate cl.exe, preferring vswhere over guessing install locations."""
    import glob

    vswhere = os.path.join(
        os.environ.get('ProgramFiles(x86)', r'C:\Program Files (x86)'),
        r'Microsoft Visual Studio\Installer\vswhere.exe',
    )
    if os.path.exists(vswhere):
        r = subprocess.run(
            [vswhere, '-latest', '-products', '*', '-requires',
             'Microsoft.VisualStudio.Component.VC.Tools.x86.x64',
             '-property', 'installationPath'],
            capture_output=True, text=True,
        )
        root = r.stdout.strip().splitlines()
        if root:
            hits = sorted(glob.glob(os.path.join(
                root[0], r'VC\Tools\MSVC\*\bin\Hostx64\x64\cl.exe')))
            if hits:
                return hits[-1]

    for base in (r'C:\Program Files\Microsoft Visual Studio',
                 r'C:\Program Files (x86)\Microsoft Visual Studio'):
        hits = sorted(glob.glob(
            base + r'\**\bin\Hostx64\x64\cl.exe', recursive=True))
        if hits:
            return hits[-1]
    return None


def build():
    if sys.platform == 'win32':
        cl_path = _find_cl()
        if cl_path and os.path.exists(cl_path):
            # cl_path: …/VC/Tools/MSVC/<ver>/bin/Hostx64/x64/cl.exe
            msvc_ver = cl_path
            for _ in range(4):
                msvc_ver = os.path.dirname(msvc_ver)
            msvc_lib = os.path.join(msvc_ver, 'lib', 'x64')
            cl_dir   = os.path.dirname(cl_path)

            obj  = os.path.join(_HERE, 'frame_ops.obj')
            lib1 = os.path.join(msvc_lib, 'msvcrt.lib')
            lib2 = os.path.join(msvc_lib, 'vcruntime.lib')

            # cl.exe is invoked directly rather than through vcvars64.bat:
            # the batch file mangles install paths containing characters like
            # '!', and frame_ops.c deliberately includes no system headers, so
            # no INCLUDE/LIB search paths are needed -- the two CRT import
            # libraries are passed by absolute path.
            env = dict(os.environ)
            env['PATH'] = cl_dir + os.pathsep + env.get('PATH', '')

            if os.path.exists(_OUT):
                try:
                    os.unlink(_OUT)
                except OSError:
                    print(f'[native] {_OUT} is locked (in use by another '
                          f'process?) — close it and retry.')
                    return False

            # /MD targets the DLL CRT so the object's default-library
            # directive matches the msvcrt.lib/vcruntime.lib pair linked below.
            # (/O2 can lower the hand-rolled fill loops to memset calls, so the
            # CRT import libraries genuinely are needed.)
            ok, err = _run([cl_path, '/O2', '/MD', '/c', '/nologo', _SRC,
                            f'/Fo:{obj}'], cwd=_HERE, env=env)
            if ok:
                link = os.path.join(cl_dir, 'link.exe')
                ok, err = _run([link, '/DLL', '/NOENTRY', '/nologo',
                                f'/OUT:{_OUT}', obj, lib1, lib2],
                               cwd=_HERE, env=env)
            for junk in [obj,
                         os.path.join(_HERE, 'frame_ops.exp'),
                         os.path.join(_HERE, 'frame_ops.lib')]:
                if os.path.exists(junk):
                    os.unlink(junk)
            if ok and os.path.exists(_OUT):
                print(f'[native] Built with MSVC -> {_OUT}')
                return True
            print(f'[native] MSVC failed: {err[:300]}')

        # ── Try GCC / MinGW ────────────────────────────────────────────────
        gcc = shutil.which('gcc')
        if gcc:
            # -static -static-libgcc matters: without it a MinGW build links
            # against libgomp-1.dll / libwinpthread-1.dll from the toolchain's
            # bin directory, which is not on PATH for the Python process and
            # makes the DLL fail to load on any machine without MinGW.
            static = ['-static', '-static-libgcc']
            for label, extra in [('GCC + OpenMP', ['-fopenmp', *static]),
                                 ('GCC', static),
                                 ('GCC (shared runtime)', [])]:
                ok, err = _run([gcc, '-O3', '-march=native', '-shared', *extra,
                                '-o', _OUT, _SRC])
                if ok and os.path.exists(_OUT) and _loadable(_OUT):
                    print(f'[native] Built with {label} -> {_OUT}')
                    return True
                print(f'[native] {label} failed: {err[:200] or "not loadable"}')

    else:  # Linux / macOS
        base = ['-O3', '-march=native', '-shared', '-fPIC']
        for compiler in ('gcc', 'clang'):
            exe = shutil.which(compiler)
            if not exe:
                continue
            for label, extra in [(f'{compiler} + OpenMP', ['-fopenmp']),
                                 (compiler, [])]:
                ok, err = _run([exe, *base, *extra, '-o', _OUT, _SRC])
                if ok and os.path.exists(_OUT):
                    print(f'[native] Built with {label} -> {_OUT}')
                    return True
                print(f'[native] {label} failed: {err[:200]}')

    print('[native] No compiler found — app will use the NumPy fallback path.')
    print('         Install GCC (MinGW on Windows) or MSVC to enable the C fast path.')
    return False


if __name__ == '__main__':
    success = build()
    sys.exit(0 if success else 1)

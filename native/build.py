"""
native/build.py — Compile frame_ops.c into a shared library.

Run once before starting the app for maximum performance:
    python native/build.py

Tries compilers in order: MSVC cl.exe → GCC → Clang.
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


def build():
    if sys.platform == 'win32':
        # ── Try MSVC (cl.exe) ──────────────────────────────────────────────
        # Locate cl.exe via vswhere
        vswhere = os.path.join(
            os.environ.get('ProgramFiles(x86)', r'C:\Program Files (x86)'),
            r'Microsoft Visual Studio\Installer\vswhere.exe',
        )
        cl_path = None
        if os.path.exists(vswhere):
            r = subprocess.run(
                [vswhere, '-latest', '-requires',
                 'Microsoft.VisualStudio.Component.VC.Tools.x86.x64',
                 '-find', r'VC\Tools\MSVC\**\bin\Hostx64\x64\cl.exe'],
                capture_output=True, text=True,
            )
            candidates = r.stdout.strip().splitlines()
            if candidates:
                cl_path = candidates[0]

        # Fallback: direct glob search for cl.exe
        if not (cl_path and os.path.exists(cl_path)):
            import glob
            hits = glob.glob(
                r'C:\Program Files\Microsoft Visual Studio\**\bin\Hostx64\x64\cl.exe',
                recursive=True,
            ) + glob.glob(
                r'C:\Program Files (x86)\Microsoft Visual Studio\**\bin\Hostx64\x64\cl.exe',
                recursive=True,
            )
            if hits:
                cl_path = hits[0]

        if cl_path and os.path.exists(cl_path):
            # Locate accompanying lib directory and Python headers
            import glob as _g
            msvc_base = os.path.dirname(os.path.dirname(os.path.dirname(cl_path)))  # …/MSVC/<ver>
            msvc_lib  = os.path.join(msvc_base, 'lib', 'x64')
            py_inc    = _g.glob(
                r'C:\Program Files\WindowsApps\PythonSoftwareFoundation.Python.3.12*\include',
            )
            py_inc = py_inc[0] if py_inc else ''
            vcvars = os.path.join(
                os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(cl_path)))),
                r'Auxiliary\Build\vcvars64.bat',
            )

            obj    = os.path.join(_HERE, 'frame_ops.obj')
            lib1   = os.path.join(msvc_lib, 'msvcrt.lib')
            lib2   = os.path.join(msvc_lib, 'vcruntime.lib')
            script = (
                f'@echo off\r\n'
                f'call "{vcvars}"\r\n'
                f'cl /O2 /c /nologo'
                + (f' /I"{py_inc}"' if py_inc else '') +
                f' "{_SRC}" /Fo:"{obj}"\r\n'
                f'if errorlevel 1 exit /b 1\r\n'
                f'link /DLL /NOENTRY /nologo /OUT:"{_OUT}" "{obj}" "{lib1}" "{lib2}"\r\n'
            )
            bat = os.path.join(_HERE, '_build_tmp.bat')
            with open(bat, 'w') as f:
                f.write(script)
            ok, err = _run([bat], cwd=_HERE, capture_output=True)
            os.unlink(bat)
            for junk in [obj,
                         os.path.join(_HERE, 'frame_ops.exp'),
                         os.path.join(_HERE, 'frame_ops.lib')]:
                if os.path.exists(junk):
                    os.unlink(junk)
            if ok and os.path.exists(_OUT):
                print(f'[native] Built with MSVC → {_OUT}')
                return True
            print(f'[native] MSVC failed: {err[:300]}')

        # ── Try GCC / MinGW ────────────────────────────────────────────────
        gcc = shutil.which('gcc')
        if gcc:
            ok, err = _run([gcc, '-O3', '-march=native', '-shared',
                            '-o', _OUT, _SRC])
            if ok and os.path.exists(_OUT):
                print(f'[native] Built with GCC → {_OUT}')
                return True
            print(f'[native] GCC failed: {err[:300]}')

    else:  # Linux / macOS
        for compiler, extra in [
            ('gcc',   ['-O3', '-march=native', '-shared', '-fPIC']),
            ('clang', ['-O3', '-march=native', '-shared', '-fPIC']),
        ]:
            exe = shutil.which(compiler)
            if not exe:
                continue
            ok, err = _run([exe, *extra, '-o', _OUT, _SRC])
            if ok and os.path.exists(_OUT):
                print(f'[native] Built with {compiler} → {_OUT}')
                return True
            print(f'[native] {compiler} failed: {err[:300]}')

    print('[native] No compiler found — app will use the NumPy fallback path.')
    print('         Install GCC (MinGW on Windows) or MSVC to enable the C fast path.')
    return False


if __name__ == '__main__':
    success = build()
    sys.exit(0 if success else 1)

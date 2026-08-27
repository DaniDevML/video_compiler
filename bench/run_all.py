"""
Run the whole correctness suite.

Usage: python bench/run_all.py
"""
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

TESTS = [
    ('native pixel engine',    'test_native.py'),
    ('native Reed-Solomon',    'test_rs_native.py'),
    ('end-to-end pipeline',    'test_e2e.py'),
    ('shard split/reassemble', 'test_shards.py'),
    ('description sidecar',    'test_sidecar.py'),
    ('audio channel',          'test_audio.py'),
    ('audio header fallback',  'test_audio_fallback.py'),
    ('v4 backward compat',     'test_compat.py'),
    ('base-N block codec',     'test_basen.py'),
    ('v8 format end to end',   'test_v8.py'),
    ('encryption',             'test_crypto.py'),
    ('file explorer index',    'test_library.py'),
    ('file explorer API',      'test_explorer_api.py'),
    ('file explorer e2e',      'test_explorer_e2e.py'),
]


def main():
    results = []
    for label, script in TESTS:
        print(f'\n{"=" * 66}\n== {label}  ({script})\n{"=" * 66}')
        t0 = time.perf_counter()
        r = subprocess.run([sys.executable, os.path.join(HERE, script)],
                           cwd=ROOT)
        dt = time.perf_counter() - t0
        results.append((label, r.returncode == 0, dt))

    print(f'\n{"=" * 66}\nSUMMARY\n{"=" * 66}')
    for label, ok, dt in results:
        print(f'  {"PASS" if ok else "FAIL"}  {label:<28} {dt:>6.1f}s')
    failed = [l for l, ok, _ in results if not ok]
    print()
    if failed:
        print(f'{len(failed)} suite(s) failed: {", ".join(failed)}')
        return 1
    print('All suites passed.')
    return 0


if __name__ == '__main__':
    sys.exit(main())

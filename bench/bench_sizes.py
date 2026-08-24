"""
Encode and decode time against payload size, 10 MB to 5 GB.

Measures the file explorer's real pipeline, stage by stage:

    file -> tar -> AES-256-GCM -> RS(255,215) -> YUV frames -> H.264
    H.264 -> YUV frames -> RS repair -> AES-256-GCM -> tar -> file

What this does **not** measure is the network. Uploading a gigabyte to
YouTube took 1441 s in the round-trip benchmark and the daily quota makes a
5 GB sweep impossible anyway, so upload and download are excluded and every
number here is local compute. That is the honest scope: it answers "how long
does my machine take", not "how long until my file is on YouTube".

Every payload is incompressible random data. That is the worst case for the
archive stage and the densest possible input to the frame encoder, and it is
also what the explorer actually sees, since encryption makes everything
incompressible before it reaches the codec.

Correctness is checked at every size -- a timing that does not round-trip
byte-identically is not reported as a timing.

Usage: python bench/bench_sizes.py [sizes_in_MB ...]
"""
import gc
import hashlib
import json
import os
import shutil
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np

import crypto_box as cb
import shards
import video_decoder as vd
from video_encoder import create_archive

HERE = os.path.dirname(os.path.abspath(__file__))
RESULTS = os.path.join(HERE, 'size_sweep_results.json')

# E: has the room; a 5 GB payload needs about 26 GB of working space.
WORK = os.environ.get('VIDCOMPILER_BENCH_WORK', 'E:/vid_compiler/benchwork')

PASSPHRASE = 'benchmark passphrase, long enough to be realistic'

# Decimal megabytes throughout: 10 MB is 10,000,000 bytes.
DEFAULT_SIZES = [10, 25, 50, 100, 250, 500, 1000, 2000, 5000]


def human(n):
    return f'{n / 1e9:.2f} GB' if n >= 1e9 else f'{n / 1e6:.0f} MB'


def write_random(path, nbytes, chunk=64 << 20):
    """Incompressible payload, generated fast enough not to dominate the run."""
    rng = np.random.default_rng(12345)
    h = hashlib.sha256()
    left = nbytes
    with open(path, 'wb', buffering=1 << 20) as f:
        while left > 0:
            n = min(chunk, left)
            block = rng.bytes(n)
            f.write(block)
            h.update(block)
            left -= n
    return h.hexdigest()


def free_gb(path):
    return shutil.disk_usage(path).free / 1e9


def run_one(mb):
    """One size, end to end, with per-stage timings. Returns a result dict."""
    nbytes = int(mb * 1e6)
    case = os.path.join(WORK, f'case_{mb}')
    shutil.rmtree(case, ignore_errors=True)
    os.makedirs(case)
    vids = os.path.join(case, 'vids')
    os.makedirs(vids)

    rec = {'mb': mb, 'payload_bytes': nbytes}
    src = os.path.join(case, f'payload_{mb}MB.bin')

    try:
        print(f'\n=== {human(nbytes)} '
              f'(free: {free_gb(WORK):.0f} GB) ===', flush=True)

        t0 = time.perf_counter()
        src_sha = write_random(src, nbytes)
        print(f'  generated in {time.perf_counter() - t0:.1f}s', flush=True)

        # ---------------------------------------------------------- ENCODE
        t = time.perf_counter()
        archive = create_archive([src])
        t_archive = time.perf_counter() - t
        rec['archive_bytes'] = len(archive)

        t = time.perf_counter()
        payload = cb.encrypt(archive, PASSPHRASE)
        t_encrypt = time.perf_counter() - t
        rec['cipher_bytes'] = len(payload)
        del archive
        gc.collect()

        n = shards.suggest_shard_count(len(payload))
        rec['shards'] = n

        t = time.perf_counter()
        jobs = shards.encode_bytes_to_shards(
            payload, vids, n_shards=n, progress=None,
            max_workers=min(n, shards.encode_workers()))
        t_video_enc = time.perf_counter() - t

        del payload
        gc.collect()

        paths = [j['path'] for j in jobs]
        rec['video_bytes'] = sum(os.path.getsize(p) for p in paths)
        rec['expansion'] = rec['video_bytes'] / nbytes

        rec['t_archive'] = t_archive
        rec['t_encrypt'] = t_encrypt
        rec['t_video_encode'] = t_video_enc
        rec['t_encode_total'] = t_archive + t_encrypt + t_video_enc

        print(f'  encode  {rec["t_encode_total"]:8.1f}s  '
              f'({nbytes / rec["t_encode_total"] / 1e6:.1f} MB/s)  '
              f'-> {human(rec["video_bytes"])} in {n} video(s), '
              f'{rec["expansion"]:.2f}x', flush=True)

        # ---------------------------------------------------------- DECODE
        descs = []
        for p in paths:
            sc = p + '.sidecar'
            descs.append(open(sc).read() if os.path.exists(sc) else '')

        t = time.perf_counter()
        recovered = shards.decode_shards_to_bytes(
            paths, descs, progress=None, max_workers=min(n, 4))
        t_video_dec = time.perf_counter() - t

        t = time.perf_counter()
        archive2 = cb.decrypt(recovered, PASSPHRASE)
        t_decrypt = time.perf_counter() - t
        del recovered
        gc.collect()

        out = os.path.join(case, 'out')
        os.makedirs(out)
        t = time.perf_counter()
        vd._extract_archive(archive2, out, lambda _m: None)
        t_extract = time.perf_counter() - t
        del archive2
        gc.collect()

        rec['t_video_decode'] = t_video_dec
        rec['t_decrypt'] = t_decrypt
        rec['t_extract'] = t_extract
        rec['t_decode_total'] = t_video_dec + t_decrypt + t_extract

        print(f'  decode  {rec["t_decode_total"]:8.1f}s  '
              f'({nbytes / rec["t_decode_total"] / 1e6:.1f} MB/s)', flush=True)

        # ------------------------------------------------------ CORRECTNESS
        got = os.path.join(out, os.path.basename(src))
        h = hashlib.sha256()
        with open(got, 'rb') as f:
            for blk in iter(lambda: f.read(1 << 20), b''):
                h.update(blk)
        rec['identical'] = (h.hexdigest() == src_sha)
        rec['ok'] = rec['identical']
        print(f'  {"IDENTICAL" if rec["identical"] else "*** MISMATCH ***"}',
              flush=True)

    except MemoryError:
        # Worth recording rather than hiding: the pipeline holds whole
        # archives in memory, so this is a real ceiling, not a fluke.
        rec['ok'] = False
        rec['error'] = 'MemoryError'
        print('  FAILED: out of memory', flush=True)
    except Exception as exc:
        rec['ok'] = False
        rec['error'] = f'{exc.__class__.__name__}: {exc}'
        print(f'  FAILED: {rec["error"]}', flush=True)
    finally:
        shutil.rmtree(case, ignore_errors=True)
        gc.collect()

    return rec


def main():
    sizes = [float(a) for a in sys.argv[1:]] or DEFAULT_SIZES
    os.makedirs(WORK, exist_ok=True)

    results = []
    if os.path.exists(RESULTS):
        try:
            results = json.load(open(RESULTS))['runs']
        except Exception:
            results = []

    print(f'Work dir {WORK} ({free_gb(WORK):.0f} GB free)')
    print(f'Sizes: {", ".join(human(int(s * 1e6)) for s in sizes)}')

    # One unrecorded pass first. Otherwise the smallest size pays for the
    # hardware-encoder probe and the galois JIT on everyone's behalf, and the
    # scaling curve shows a fixed cost that only the first payload ever meets.
    print('\nWarming up (not recorded)...', flush=True)
    warm = run_one(2)
    print(f'  warmup {"ok" if warm.get("ok") else "FAILED: " + warm.get("error", "?")}',
          flush=True)

    for mb in sizes:
        rec = run_one(mb)
        results = [r for r in results if r['mb'] != mb] + [rec]
        results.sort(key=lambda r: r['mb'])
        # Written after every size, so a run that dies at 5 GB still leaves
        # everything below it usable.
        with open(RESULTS, 'w') as f:
            json.dump({'runs': results,
                       'note': 'local compute only; no network transfer',
                       'machine': {'cores': os.cpu_count()}}, f, indent=2)

    print(f'\nWrote {RESULTS}')
    print(f'{"size":>10} {"encode":>10} {"decode":>10} {"enc MB/s":>10} '
          f'{"dec MB/s":>10} {"expansion":>10}  ok')
    for r in results:
        if not r.get('ok'):
            print(f'{human(r["payload_bytes"]):>10} '
                  f'{r.get("error", "failed"):>44}  no')
            continue
        print(f'{human(r["payload_bytes"]):>10} '
              f'{r["t_encode_total"]:>9.1f}s {r["t_decode_total"]:>9.1f}s '
              f'{r["payload_bytes"] / r["t_encode_total"] / 1e6:>10.1f} '
              f'{r["payload_bytes"] / r["t_decode_total"] / 1e6:>10.1f} '
              f'{r["expansion"]:>9.2f}x  yes')


if __name__ == '__main__':
    main()

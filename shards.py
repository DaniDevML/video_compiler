"""
shards.py — split one archive across several videos, encoded and decoded in
parallel.

Two reasons this exists:

  Ceiling. A single video is bounded by YouTube's per-video duration limit --
  15 minutes for an unverified account, about 1.5 GB at the default profile.
  Sharding is the only way past that.

  Throughput. Upload dominates end-to-end time at any real size (a 1 GB payload
  is ~2 minutes of encoding against ~24 minutes of uploading), and a single
  HTTP stream does not necessarily saturate the link. Several concurrent
  uploads might. Whether they actually do is measured, not assumed --
  see bench/bench_shards.py.

Layout: the archive is built once, then cut into contiguous slices. Each slice
is Reed-Solomon encoded and written as an independent video with its own header
describing that slice. A manifest, carried in every shard's description, records
the whole-archive length and CRC plus this shard's offset, so any single shard
identifies the set it belongs to and the decoder can verify the reassembly.

Shards are deliberately *not* self-describing about where the other shards
live: video ids are only known after upload, and rewriting descriptions
afterwards would need a broader OAuth scope than uploading does. The caller
keeps the URLs.
"""

import base64
import concurrent.futures as _fut
import io
import os
import struct
import tarfile
import zlib

import video_codec as vc
import video_encoder as ve
import video_decoder as vd

MANIFEST_PREFIX = 'vidcompiler-manifest:'
MANIFEST_MAGIC  = b'VIDSHRD1'
# magic(8) total_size(8) crc32(4) n_shards(2) index(2) offset(8) length(8)
MANIFEST_FORMAT = '<8sQIHHQQ'
MANIFEST_SIZE   = struct.calcsize(MANIFEST_FORMAT)


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------

def pack_manifest(total_size, crc32, n_shards, index, offset, length) -> str:
    raw = struct.pack(MANIFEST_FORMAT, MANIFEST_MAGIC, total_size, crc32,
                      n_shards, index, offset, length)
    return MANIFEST_PREFIX + base64.b64encode(raw).decode('ascii')


def parse_manifest(text: str):
    """Recover a manifest from description text, or None.

    Never raises: descriptions are free text and a failure just means this
    video is not part of a shard set.
    """
    if not text:
        return None
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith(MANIFEST_PREFIX):
            continue
        try:
            raw = base64.b64decode(line[len(MANIFEST_PREFIX):], validate=True)
            if len(raw) < MANIFEST_SIZE:
                continue
            magic, total, crc, n, idx, off, ln = struct.unpack(
                MANIFEST_FORMAT, raw[:MANIFEST_SIZE])
            if magic != MANIFEST_MAGIC:
                continue
            return dict(total_size=total, crc32=crc, n_shards=n,
                        index=idx, offset=off, length=ln)
        except Exception:
            continue
    return None


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------

# Frames a single video may hold before it risks the per-video duration limit.
# 15 minutes at 30 fps, with a little slack.
MAX_FRAMES_PER_VIDEO = 15 * 60 * vc.FPS - 60


def max_bytes_per_video(profile=None) -> int:
    p = profile or vc.DEFAULT_PROFILE
    # allow for the RS expansion on the way in
    return int(MAX_FRAMES_PER_VIDEO * p.bytes * vc.CHUNK_IN / 255)


# How many videos to split into when the caller does not say.
#
# Two separate reasons to shard, with different thresholds. Above
# max_bytes_per_video it is mandatory -- one video cannot hold the archive.
# Below that it is an optimisation: concurrent uploads measured 2.14x the
# throughput of a single stream, which is worth having on a large archive but
# not on a small one, where the extra videos cost more in clutter (and in
# YouTube's daily upload allowance) than the seconds they save.
SHARD_MIN_BYTES   = 64 * 1024 * 1024     # below this, one video
SHARD_TARGET_BYTES = 256 * 1024 * 1024   # aim for roughly this much per shard
SHARD_MAX          = 4                   # more streams stop helping


def suggest_shard_count(archive_size: int, profile=None) -> int:
    """A sensible number of videos for an archive of this size."""
    required = max(1, -(-archive_size // max_bytes_per_video(profile)))
    if archive_size < SHARD_MIN_BYTES:
        return required
    wanted = round(archive_size / SHARD_TARGET_BYTES) or 1
    return max(required, min(SHARD_MAX, max(2, wanted)))


def plan_shards(archive_size: int, n_shards: int = None,
                profile=None) -> list:
    """Contiguous (offset, length) slices covering the archive."""
    cap = max_bytes_per_video(profile)
    need = max(1, -(-archive_size // cap)) if archive_size else 1
    n = max(need, n_shards or 1)

    base = -(-archive_size // n) if archive_size else 0
    out, off = [], 0
    for _ in range(n):
        ln = min(base, archive_size - off)
        out.append((off, ln))
        off += ln
        if off >= archive_size:
            break
    while len(out) < n:              # keep the requested count even if empty
        out.append((archive_size, 0))
    return out


# ---------------------------------------------------------------------------
# Encode
# ---------------------------------------------------------------------------

def encode_files_to_shards(file_paths: list, out_dir: str, n_shards: int = None,
                           progress=None, max_workers: int = None) -> list:
    """Archive `file_paths` and write it across several videos.

    Returns a list of dicts: path, index, offset, length, manifest.
    """
    def log(msg):
        if progress:
            progress(msg)

    os.makedirs(out_dir, exist_ok=True)
    log('Creating archive...')
    archive = ve.create_archive(file_paths)
    return encode_bytes_to_shards(archive, out_dir, n_shards=n_shards,
                                  progress=progress, max_workers=max_workers)


def encode_bytes_to_shards(archive: bytes, out_dir: str, n_shards: int = None,
                           progress=None, max_workers: int = None) -> list:
    def log(msg):
        if progress:
            progress(msg)

    os.makedirs(out_dir, exist_ok=True)
    total = len(archive)
    crc = zlib.crc32(archive) & 0xFFFFFFFF
    slices = plan_shards(total, n_shards)
    n = len(slices)
    log(f'Archive {total:,} bytes -> {n} shard(s)')

    jobs = []
    for i, (off, ln) in enumerate(slices):
        manifest = pack_manifest(total, crc, n, i, off, ln)
        jobs.append(dict(index=i, offset=off, length=ln, manifest=manifest,
                         path=os.path.join(out_dir, f'shard{i:03d}.mp4'),
                         data=archive[off:off + ln]))

    workers = max_workers or min(n, _default_workers())

    def run(job):
        ve.encode_bytes_to_video(job['data'], job['path'],
                                 extra_sidecar=job['manifest'])
        job.pop('data')
        job['bytes'] = os.path.getsize(job['path'])
        return job

    if workers <= 1 or n == 1:
        out = [run(j) for j in jobs]
    else:
        # Threads, not processes: the heavy stages all release the GIL. Pixel
        # modulation and Reed-Solomon run in the C library, and ffmpeg is a
        # separate process per shard.
        with _fut.ThreadPoolExecutor(max_workers=workers) as ex:
            out = list(ex.map(run, jobs))

    for j in out:
        log(f'  shard {j["index"]}: {j["length"]:,} B -> '
            f'{j["bytes"]:,} B video')
    return out


def _default_workers() -> int:
    return max(1, min(4, (os.cpu_count() or 4) // 2))


# ---------------------------------------------------------------------------
# Decode
# ---------------------------------------------------------------------------

def decode_shards_to_files(video_paths: list, output_dir: str,
                           descriptions: list = None, progress=None,
                           max_workers: int = None) -> list:
    """Decode a set of shard videos and extract the reassembled archive."""
    def log(msg):
        if progress:
            progress(msg)

    archive = decode_shards_to_bytes(video_paths, descriptions,
                                     progress=progress,
                                     max_workers=max_workers)
    return vd._extract_archive(archive, output_dir, log)


def decode_shards_to_bytes(video_paths: list, descriptions: list = None,
                           progress=None, max_workers: int = None) -> bytes:
    def log(msg):
        if progress:
            progress(msg)

    descs = list(descriptions or [''] * len(video_paths))
    while len(descs) < len(video_paths):
        descs.append('')

    n = len(video_paths)
    workers = max_workers or min(n, _default_workers())

    def run(i):
        data = vd.decode_video_to_bytes(video_paths[i], description=descs[i])
        return i, data, parse_manifest(descs[i])

    if workers <= 1 or n == 1:
        results = [run(i) for i in range(n)]
    else:
        with _fut.ThreadPoolExecutor(max_workers=workers) as ex:
            results = list(ex.map(run, range(n)))

    manifests = [m for _, _, m in results if m]
    if not manifests:
        # No manifest anywhere: fall back to concatenating in the order given.
        log('No shard manifest found; joining in the order supplied.')
        return b''.join(d for _, d, _ in sorted(results))

    ref = manifests[0]
    if len(results) < ref['n_shards']:
        raise RuntimeError(
            f'This archive is split across {ref["n_shards"]} videos but only '
            f'{len(results)} were supplied. All of them are needed.')

    parts = {}
    for i, data, man in results:
        if man is None:
            raise RuntimeError(
                f'Video {i + 1} carries no shard manifest, so its position in '
                'the set is unknown. Supply its description, or re-supply the '
                'videos in order.')
        if man['crc32'] != ref['crc32'] or man['total_size'] != ref['total_size']:
            raise RuntimeError(
                'These videos are shards of different archives -- their '
                'manifests disagree on the total size or checksum.')
        parts[man['index']] = (man['offset'], data)

    missing = [i for i in range(ref['n_shards']) if i not in parts]
    if missing:
        raise RuntimeError(
            f'Missing shard(s) {missing} of {ref["n_shards"]}.')

    buf = bytearray(ref['total_size'])
    for idx in range(ref['n_shards']):
        off, data = parts[idx]
        buf[off:off + len(data)] = data
    archive = bytes(buf)

    got = zlib.crc32(archive) & 0xFFFFFFFF
    if got != ref['crc32']:
        raise RuntimeError(
            'The reassembled archive failed its checksum. The shards decoded '
            'individually but do not fit together -- most likely one of them '
            'belongs to a different upload.')
    log(f'Reassembled {ref["n_shards"]} shards, {len(archive):,} bytes, CRC OK.')
    return archive

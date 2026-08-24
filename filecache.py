"""A local cache for files that have been fetched back from YouTube.

Fetching a file means downloading a video, decoding every frame, repairing it
with Reed-Solomon and decrypting -- minutes for a large file. Doing that again
because the user scrolled a PDF to page two would be indefensible, so the
recovered bytes are kept.

The cache is disposable by construction. Every entry can be rebuilt from
YouTube, so eviction is never data loss, and the cap can be small.

Encrypted files land here as **plaintext**, which is the honest trade for
being able to preview them at all. They are evicted like anything else, and
`purge()` clears the lot -- exposed in the UI, because someone who encrypted a
file may well want the decrypted copy gone when they are done with it.
"""

import os
import re
import shutil

import paths

# Two gigabytes by default: enough to keep a working set of documents and a
# video or two, small enough not to quietly fill a disk.
DEFAULT_LIMIT = int(os.environ.get('VIDCOMPILER_CACHE_MB', '2048')) * 1024 * 1024

_SAFE = re.compile(r'[^A-Za-z0-9._-]')


def cache_dir() -> str:
    d = paths.data('.cache')
    os.makedirs(d, exist_ok=True)
    return d


def path_for(entry_id: int, name: str) -> str:
    """One file per entry, keeping the extension so mimetype sniffing works."""
    ext = os.path.splitext(name)[1][:16]
    return os.path.join(cache_dir(), f'{int(entry_id)}{_SAFE.sub("_", ext)}')


def get(entry_id: int, name: str) -> str | None:
    p = path_for(entry_id, name)
    if os.path.exists(p):
        # Touch it so eviction sees recent use, not just recent arrival.
        try:
            os.utime(p, None)
        except OSError:
            pass
        return p
    return None


def put(entry_id: int, name: str, src_path: str, limit: int = None) -> str:
    dest = path_for(entry_id, name)
    if os.path.abspath(src_path) != os.path.abspath(dest):
        shutil.move(src_path, dest)
    evict(limit)
    return dest


def drop(entry_id: int, name: str) -> None:
    p = path_for(entry_id, name)
    try:
        os.unlink(p)
    except OSError:
        pass


def size() -> int:
    total = 0
    for f in os.scandir(cache_dir()):
        if f.is_file():
            try:
                total += f.stat().st_size
            except OSError:
                pass
    return total


def evict(limit: int = None) -> int:
    """Drop least-recently-used entries until the cache fits. Returns bytes freed."""
    limit = DEFAULT_LIMIT if limit is None else limit
    files = []
    for f in os.scandir(cache_dir()):
        if f.is_file():
            try:
                st = f.stat()
                files.append((st.st_mtime, st.st_size, f.path))
            except OSError:
                pass

    total = sum(s for _, s, _ in files)
    freed = 0
    files.sort()                       # oldest first
    for _mtime, fsize, fpath in files:
        if total - freed <= limit:
            break
        try:
            os.unlink(fpath)
            freed += fsize
        except OSError:
            pass
    return freed


def purge() -> int:
    """Empty the cache. Returns bytes freed."""
    return evict(0)

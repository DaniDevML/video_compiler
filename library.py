"""The index behind the file explorer.

This is the whole trick of the `file_explorer` branch: it looks like cloud
storage, but no file content is ever stored locally. What the database holds
is a name, a size, a type, and the YouTube link the bytes actually live
behind. A terabyte of files is a few hundred kilobytes of index.

Folders are equally imaginary -- rows with a parent pointer. Nothing on disk
mirrors the tree, so moving a file between folders is an UPDATE, not a copy of
a gigabyte.

Concurrency note: SQLite connections cannot cross threads, and this runs under
a threaded Flask server with background workers, so every call opens its own
connection. The index is tiny and writes are rare, which makes the cost
irrelevant and the alternative -- a shared connection with a lock -- easy to
get wrong.
"""

import json
import os
import sqlite3
import threading
import time

import paths

SCHEMA = """
CREATE TABLE IF NOT EXISTS entries (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    parent_id   INTEGER,
    name        TEXT    NOT NULL,
    kind        TEXT    NOT NULL CHECK (kind IN ('folder', 'file')),
    size        INTEGER NOT NULL DEFAULT 0,
    mime        TEXT    NOT NULL DEFAULT '',
    sha256      TEXT    NOT NULL DEFAULT '',
    encrypted   INTEGER NOT NULL DEFAULT 0,
    verify_salt TEXT    NOT NULL DEFAULT '',
    verifier    TEXT    NOT NULL DEFAULT '',
    url         TEXT    NOT NULL DEFAULT '',
    playlist    TEXT    NOT NULL DEFAULT '',
    urls        TEXT    NOT NULL DEFAULT '[]',
    video_ids   TEXT    NOT NULL DEFAULT '[]',
    shards      INTEGER NOT NULL DEFAULT 1,
    stored_size INTEGER NOT NULL DEFAULT 0,
    created_at  REAL    NOT NULL,
    FOREIGN KEY (parent_id) REFERENCES entries (id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_parent ON entries (parent_id);
CREATE INDEX IF NOT EXISTS idx_name   ON entries (name);
"""

_init_lock = threading.Lock()
_initialised = False


def db_path() -> str:
    return paths.data('library.db')


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(db_path(), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys = ON')
    # WAL keeps a browsing read from blocking behind a worker's write, which
    # matters here because a write happens at the end of a job that may have
    # been running for minutes.
    conn.execute('PRAGMA journal_mode = WAL')
    return conn


def init() -> None:
    global _initialised
    with _init_lock:
        if _initialised:
            return
        conn = _connect()
        try:
            conn.executescript(SCHEMA)
            conn.commit()
        finally:
            conn.close()
        _initialised = True


def _row_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    d['urls'] = json.loads(d.get('urls') or '[]')
    d['video_ids'] = json.loads(d.get('video_ids') or '[]')
    d['encrypted'] = bool(d['encrypted'])
    return d


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------

def get(entry_id: int) -> dict | None:
    init()
    conn = _connect()
    try:
        row = conn.execute('SELECT * FROM entries WHERE id = ?',
                           (entry_id,)).fetchone()
        return _row_to_dict(row) if row else None
    finally:
        conn.close()


def list_dir(parent_id: int | None) -> list:
    """Folders first, then files, each alphabetically -- what a file manager does."""
    init()
    conn = _connect()
    try:
        if parent_id in (None, 0):
            rows = conn.execute(
                'SELECT * FROM entries WHERE parent_id IS NULL '
                'ORDER BY kind DESC, name COLLATE NOCASE').fetchall()
        else:
            rows = conn.execute(
                'SELECT * FROM entries WHERE parent_id = ? '
                'ORDER BY kind DESC, name COLLATE NOCASE',
                (parent_id,)).fetchall()
        return [_row_to_dict(r) for r in rows]
    finally:
        conn.close()


def breadcrumbs(parent_id: int | None) -> list:
    """The chain from the root down to `parent_id`, for the path bar."""
    init()
    trail = []
    conn = _connect()
    try:
        cur = parent_id
        seen = set()
        while cur not in (None, 0) and cur not in seen:
            seen.add(cur)
            row = conn.execute(
                'SELECT id, name, parent_id FROM entries WHERE id = ?',
                (cur,)).fetchone()
            if not row:
                break
            trail.append({'id': row['id'], 'name': row['name']})
            cur = row['parent_id']
    finally:
        conn.close()
    trail.reverse()
    return trail


def search(term: str, limit: int = 200) -> list:
    init()
    conn = _connect()
    try:
        rows = conn.execute(
            'SELECT * FROM entries WHERE name LIKE ? '
            'ORDER BY kind DESC, name COLLATE NOCASE LIMIT ?',
            (f'%{term}%', limit)).fetchall()
        return [_row_to_dict(r) for r in rows]
    finally:
        conn.close()


def stats() -> dict:
    init()
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS files, "
            "COALESCE(SUM(size), 0) AS bytes, "
            "COALESCE(SUM(stored_size), 0) AS stored, "
            "COALESCE(SUM(encrypted), 0) AS encrypted "
            "FROM entries WHERE kind = 'file'").fetchone()
        folders = conn.execute(
            "SELECT COUNT(*) AS n FROM entries WHERE kind = 'folder'").fetchone()
    finally:
        conn.close()
    try:
        index_bytes = os.path.getsize(db_path())
    except OSError:
        index_bytes = 0
    return {'files': row['files'], 'folders': folders['n'],
            'bytes': row['bytes'], 'stored': row['stored'],
            'encrypted': row['encrypted'], 'index_bytes': index_bytes}


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------

def _unique_name(conn, parent_id, name: str) -> str:
    """Resolve a collision the way a desktop does: "report (2).pdf"."""
    base, ext = os.path.splitext(name)
    candidate = name
    n = 1
    while True:
        if parent_id in (None, 0):
            hit = conn.execute(
                'SELECT 1 FROM entries WHERE parent_id IS NULL AND name = ?',
                (candidate,)).fetchone()
        else:
            hit = conn.execute(
                'SELECT 1 FROM entries WHERE parent_id = ? AND name = ?',
                (parent_id, candidate)).fetchone()
        if not hit:
            return candidate
        n += 1
        candidate = f'{base} ({n}){ext}'


def mkdir(parent_id: int | None, name: str) -> dict:
    init()
    name = (name or '').strip() or 'New folder'
    conn = _connect()
    try:
        name = _unique_name(conn, parent_id, name)
        cur = conn.execute(
            'INSERT INTO entries (parent_id, name, kind, created_at) '
            'VALUES (?, ?, ?, ?)',
            (parent_id or None, name, 'folder', time.time()))
        conn.commit()
        new_id = cur.lastrowid
    finally:
        conn.close()
    return get(new_id)


def add_file(parent_id: int | None, name: str, *, size: int, mime: str,
             sha256: str, encrypted: bool, verify_salt: str, verifier: str,
             url: str, playlist: str, urls: list, video_ids: list,
             shards: int, stored_size: int) -> dict:
    init()
    conn = _connect()
    try:
        name = _unique_name(conn, parent_id, name)
        cur = conn.execute(
            'INSERT INTO entries (parent_id, name, kind, size, mime, sha256, '
            'encrypted, verify_salt, verifier, url, playlist, urls, '
            'video_ids, shards, stored_size, created_at) '
            'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (parent_id or None, name, 'file', size, mime, sha256,
             1 if encrypted else 0, verify_salt, verifier, url, playlist,
             json.dumps(urls), json.dumps(video_ids), shards, stored_size,
             time.time()))
        conn.commit()
        new_id = cur.lastrowid
    finally:
        conn.close()
    return get(new_id)


def rename(entry_id: int, name: str) -> dict:
    init()
    name = (name or '').strip()
    if not name:
        raise ValueError('A name is required.')
    if '/' in name or '\\' in name:
        raise ValueError('A name cannot contain a slash.')
    conn = _connect()
    try:
        row = conn.execute('SELECT parent_id FROM entries WHERE id = ?',
                           (entry_id,)).fetchone()
        if not row:
            raise KeyError('No such item.')
        name = _unique_name(conn, row['parent_id'], name)
        conn.execute('UPDATE entries SET name = ? WHERE id = ?',
                     (name, entry_id))
        conn.commit()
    finally:
        conn.close()
    return get(entry_id)


def _descendants(conn, entry_id: int) -> set:
    """Every id under `entry_id`, so a move cannot put a folder inside itself."""
    found = set()
    frontier = [entry_id]
    while frontier:
        current = frontier.pop()
        for row in conn.execute('SELECT id FROM entries WHERE parent_id = ?',
                                (current,)).fetchall():
            if row['id'] not in found:
                found.add(row['id'])
                frontier.append(row['id'])
    return found


def move(entry_id: int, new_parent: int | None) -> dict:
    init()
    new_parent = new_parent or None
    conn = _connect()
    try:
        row = conn.execute('SELECT * FROM entries WHERE id = ?',
                           (entry_id,)).fetchone()
        if not row:
            raise KeyError('No such item.')
        if new_parent is not None:
            dest = conn.execute('SELECT kind FROM entries WHERE id = ?',
                                (new_parent,)).fetchone()
            if not dest:
                raise KeyError('No such destination folder.')
            if dest['kind'] != 'folder':
                raise ValueError('Files cannot contain other files.')
            if new_parent == entry_id or new_parent in _descendants(conn, entry_id):
                raise ValueError('A folder cannot be moved inside itself.')
        name = _unique_name(conn, new_parent, row['name'])
        conn.execute('UPDATE entries SET parent_id = ?, name = ? WHERE id = ?',
                     (new_parent, name, entry_id))
        conn.commit()
    finally:
        conn.close()
    return get(entry_id)


def delete(entry_id: int) -> list:
    """Remove an entry and everything under it. Returns the file rows removed.

    Only the index is touched. The videos stay on YouTube -- see the note in
    app.py about why deletion there is a separate, explicit act.
    """
    init()
    conn = _connect()
    try:
        row = conn.execute('SELECT * FROM entries WHERE id = ?',
                           (entry_id,)).fetchone()
        if not row:
            raise KeyError('No such item.')
        ids = {entry_id} | _descendants(conn, entry_id)
        placeholders = ','.join('?' * len(ids))
        removed = conn.execute(
            f"SELECT * FROM entries WHERE id IN ({placeholders}) "
            f"AND kind = 'file'", tuple(ids)).fetchall()
        removed = [_row_to_dict(r) for r in removed]
        conn.execute(f'DELETE FROM entries WHERE id IN ({placeholders})',
                     tuple(ids))
        conn.commit()
    finally:
        conn.close()
    return removed

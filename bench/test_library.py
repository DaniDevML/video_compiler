"""
The file-explorer index.

The index is the only thing standing between a user and a lost archive: if a
row is dropped, the YouTube link goes with it and the file is unrecoverable in
practice. So the cases here are the structural ones -- collisions, cycles,
cascading deletes -- rather than the happy path.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Point the index somewhere disposable before library resolves its path.
_tmp = tempfile.mkdtemp(prefix='libtest_')
os.environ['VIDCOMPILER_DATA'] = _tmp

import library                                                  # noqa: E402

fails = 0


def check(name, cond):
    global fails
    print(f'  {"PASS" if cond else "FAIL"}  {name}')
    if not cond:
        fails += 1


def rejects(name, fn, *expected):
    try:
        fn()
    except expected:
        check(name, True)
        return
    except Exception as exc:
        check(f'{name} (raised {exc.__class__.__name__})', False)
        return
    check(f'{name} (was allowed!)', False)


def add(parent, name, **kw):
    if isinstance(parent, dict):
        parent = parent['id']
    opts = dict(size=1234, mime='text/plain', sha256='ab' * 32,
                encrypted=False, verify_salt='', verifier='',
                url='https://youtu.be/x', playlist='',
                urls=['https://youtu.be/x'], video_ids=['x'],
                shards=1, stored_size=4096)
    opts.update(kw)
    return library.add_file(parent, name, **opts)


print(f'Index at {library.db_path()}')
library.init()

print('\nBasics')
root_files = library.list_dir(None)
check('starts empty', root_files == [])

docs = library.mkdir(None, 'Documents')
photos = library.mkdir(None, 'Photos')
notes = add(docs, 'notes.txt')
check('folder created', docs['kind'] == 'folder' and docs['name'] == 'Documents')
check('file lands in its folder', library.list_dir(docs['id'])[0]['id'] == notes['id'])
check('root lists both folders', len(library.list_dir(None)) == 2)
check('folders sort before files',
      [e['kind'] for e in library.list_dir(None)] == ['folder', 'folder'])

print('\nNo file content is stored')
check('the row has no content column', 'content' not in notes and 'data' not in notes)
check('the row does carry a link', notes['url'].startswith('https://'))
size = os.path.getsize(library.db_path())
check(f'index is small ({size:,} bytes for 3 entries)', size < 200_000)

print('\nName collisions')
a = add(docs, 'report.pdf')
b = add(docs, 'report.pdf')
c = add(docs, 'report.pdf')
check('a colliding name is suffixed, not overwritten',
      [a['name'], b['name'], c['name']] == ['report.pdf', 'report (2).pdf', 'report (3).pdf'])
check('all three survive', len(library.list_dir(docs['id'])) == 4)

print('\nRename and move')
renamed = library.rename(a['id'], 'final.pdf')
check('rename works', renamed['name'] == 'final.pdf')
rejects('a slash in a name is rejected',
        lambda: library.rename(a['id'], 'a/b'), ValueError)
rejects('an empty name is rejected',
        lambda: library.rename(a['id'], '   '), ValueError)

moved = library.move(a['id'], photos['id'])
check('move re-parents', moved['parent_id'] == photos['id'])
check('the source folder loses it',
      all(e['id'] != a['id'] for e in library.list_dir(docs['id'])))

inner = library.mkdir(docs['id'], 'Inner')
deep = library.mkdir(inner['id'], 'Deeper')
rejects('a folder cannot move into itself',
        lambda: library.move(docs['id'], docs['id']), ValueError)
rejects('a folder cannot move into its own descendant',
        lambda: library.move(docs['id'], deep['id']), ValueError)
rejects('a file cannot become a parent',
        lambda: library.move(notes['id'], notes['id']), ValueError)
rejects('moving to a missing folder fails',
        lambda: library.move(notes['id'], 999999), KeyError)

print('\nBreadcrumbs')
trail = library.breadcrumbs(deep['id'])
check('trail runs root -> leaf',
      [t['name'] for t in trail] == ['Documents', 'Inner', 'Deeper'])
check('root has an empty trail', library.breadcrumbs(None) == [])

print('\nSearch')
add(deep['id'], 'buried-treasure.txt')
hits = library.search('treasure')
check('search finds a file several levels down', len(hits) == 1)
check('search is scoped by name', library.search('nothing-matches-this') == [])

print('\nDelete cascades')
removed = library.delete(docs['id'])
names = sorted(r['name'] for r in removed)
check('delete returns every file it removed, for their links',
      names == ['buried-treasure.txt', 'notes.txt', 'report (2).pdf', 'report (3).pdf'])
check('the folder is gone', library.get(docs['id']) is None)
check('descendants are gone', library.get(deep['id']) is None)
check('unrelated files survive', library.get(a['id']) is not None)
rejects('deleting a missing item fails',
        lambda: library.delete(999999), KeyError)

print('\nStats')
s = library.stats()
check('counts only files', s['files'] == 1)
check('folders counted separately', s['folders'] == 1)
check('reports the index size', s['index_bytes'] > 0)

print('\nEncrypted entries')
sec = add(None, 'secret.bin', encrypted=True, verify_salt='00' * 16,
          verifier='de' * 32)
check('encryption flag round-trips', library.get(sec['id'])['encrypted'] is True)
check('verifier round-trips', library.get(sec['id'])['verifier'] == 'de' * 32)
check('no passphrase is stored anywhere',
      'passphrase' not in library.get(sec['id']))

print()
if fails:
    print(f'{fails} check(s) FAILED')
    sys.exit(1)
print('All library checks passed.')

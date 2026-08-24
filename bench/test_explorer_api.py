"""
The explorer's HTTP surface.

Everything here runs against Flask's test client with a throwaway data
directory, so it exercises the real routes without a server, without
credentials and without touching YouTube. The two routes that do reach the
network -- upload and fetch -- are covered up to the point where they hand off
to a background worker.

The checks worth having are the refusals: a route that returns 200 for a
missing folder, or serves a file that was never fetched, is how an explorer
starts lying about what it holds.

Usage: python bench/test_explorer_api.py
"""
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_tmp = tempfile.mkdtemp(prefix='apitest_')
os.environ['VIDCOMPILER_DATA'] = _tmp
os.environ['VIDCOMPILER_SCRATCH'] = os.path.join(_tmp, 'scratch')

import app as webapp                                             # noqa: E402
import filecache                                                 # noqa: E402
import library                                                   # noqa: E402

fails = 0
client = webapp.app.test_client()


def check(name, cond):
    global fails
    print(f'  {"PASS" if cond else "FAIL"}  {name}')
    if not cond:
        fails += 1


def post(path, payload):
    return client.post(path, data=json.dumps(payload),
                       content_type='application/json')


print(f'Data dir {_tmp}')

print('\nPages')
r = client.get('/')
check('/ serves the file explorer',
      r.status_code == 200 and b'VidCompiler' in r.data and b'/api/fs/list' in r.data)
r = client.get('/codec')
check('/codec still serves the raw tool',
      r.status_code == 200 and b'Decode' in r.data)

print('\nListing')
r = client.get('/api/fs/list')
check('empty root lists nothing',
      r.status_code == 200 and r.get_json()['entries'] == [])
check('root has no breadcrumbs', r.get_json()['trail'] == [])
check('a missing folder is a 404',
      client.get('/api/fs/list?parent=4242').status_code == 404)

print('\nFolders')
r = post('/api/fs/folder', {'name': 'Work'})
work = r.get_json()['entry']
check('folder created', r.status_code == 200 and work['name'] == 'Work')
sub = post('/api/fs/folder', {'parent': work['id'], 'name': 'Reports'}).get_json()['entry']
r = client.get(f'/api/fs/list?parent={work["id"]}')
check('subfolder is listed inside it',
      [e['id'] for e in r.get_json()['entries']] == [sub['id']])
check('breadcrumbs name the parent',
      [t['name'] for t in r.get_json()['trail']] == ['Work'])

r = client.get('/api/fs/folders')
paths = [f['path'] for f in r.get_json()['folders']]
check('the move picker lists full paths',
      paths == ['/', '/Work', '/Work/Reports'])

print('\nFiles (index rows, no upload)')
entry = library.add_file(
    sub['id'], 'quarterly.pdf', size=5_000_000, mime='application/pdf',
    sha256='cd' * 32, encrypted=False, verify_salt='', verifier='',
    url='https://youtu.be/abc', playlist='', urls=['https://youtu.be/abc'],
    video_ids=['abc'], shards=1, stored_size=16_000_000)
r = client.get(f'/api/fs/list?parent={sub["id"]}')
listed = r.get_json()['entries'][0]
check('the file is listed', listed['name'] == 'quarterly.pdf')
check('the link is exposed for copying', listed['url'] == 'https://youtu.be/abc')

print('\nServing before fetching')
r = client.get(f'/api/fs/download/{entry["id"]}')
check('download of an unfetched file is refused, not 500',
      r.status_code == 409 and r.get_json().get('need_fetch') is True)
r = client.get(f'/api/fs/preview/{entry["id"]}')
check('preview of an unfetched file is refused', r.status_code == 409)
check('download of a missing id is a 404',
      client.get('/api/fs/download/999999').status_code == 404)
check('preview of a folder is a 404',
      client.get(f'/api/fs/preview/{work["id"]}').status_code == 404)

print('\nServing from the cache')
cached = filecache.path_for(entry['id'], entry['name'])
with open(cached, 'wb') as f:
    f.write(b'%PDF-1.4 pretend\n' * 100)
r = client.get(f'/api/fs/download/{entry["id"]}')
check('a cached file downloads', r.status_code == 200 and r.data.startswith(b'%PDF'))
check('it downloads under its own name',
      'quarterly.pdf' in r.headers.get('Content-Disposition', ''))
r = client.get(f'/api/fs/preview/{entry["id"]}')
check('preview serves inline with the right type',
      r.status_code == 200 and r.headers['Content-Type'].startswith('application/pdf'))
check('preview is not an attachment',
      'attachment' not in r.headers.get('Content-Disposition', ''))
r = client.get(f'/api/fs/preview/{entry["id"]}', headers={'Range': 'bytes=0-9'})
check('byte ranges are honoured, so media can seek',
      r.status_code == 206 and len(r.data) == 10)

print('\nEncrypted files refuse to start work without a key')
import crypto_box as cb                                          # noqa: E402
salt = cb.new_salt()
sec = library.add_file(
    None, 'sealed.bin', size=1000, mime='application/octet-stream',
    sha256='ef' * 32, encrypted=True, verify_salt=salt.hex(),
    verifier=cb.verifier('the real passphrase', salt),
    url='https://youtu.be/zzz', playlist='', urls=['https://youtu.be/zzz'],
    video_ids=['zzz'], shards=1, stored_size=4000)

r = post(f'/api/fs/fetch/{sec["id"]}', {})
check('no passphrase is a 401, not a job',
      r.status_code == 401 and r.get_json().get('need_passphrase') is True)
r = post(f'/api/fs/fetch/{sec["id"]}', {'passphrase': 'wrong one'})
check('the wrong passphrase is caught before any download starts',
      r.status_code == 401 and 'job_id' not in r.get_json())

print('\nRename, move, delete')
r = post('/api/fs/rename', {'id': entry['id'], 'name': 'annual.pdf'})
check('rename returns the updated row',
      r.status_code == 200 and r.get_json()['entry']['name'] == 'annual.pdf')
check('renaming a missing id is a 404',
      post('/api/fs/rename', {'id': 999999, 'name': 'x'}).status_code == 404)
check('an invalid name is a 400',
      post('/api/fs/rename', {'id': entry['id'], 'name': 'a/b'}).status_code == 400)

r = post('/api/fs/move', {'id': entry['id'], 'parent': None})
check('move to root works', r.get_json()['entry']['parent_id'] is None)
check('a cycle is a 400',
      post('/api/fs/move', {'id': work['id'], 'parent': sub['id']}).status_code == 400)

r = post('/api/fs/delete', {'id': entry['id']})
body = r.get_json()
check('delete reports what it removed', r.status_code == 200 and body['removed'] == 1)
check('delete hands back the links, so it is reversible',
      body['links'] == ['https://youtu.be/abc'])
check('the cached copy is dropped too', filecache.get(entry['id'], 'annual.pdf') is None)
check('deleting a missing id is a 404',
      post('/api/fs/delete', {'id': 999999}).status_code == 404)

r = post('/api/fs/delete', {'id': work['id']})
check('deleting a folder cascades', r.status_code == 200)
check('its subfolder is gone', library.get(sub['id']) is None)

print('\nStats and cache')
r = client.get('/api/fs/stats')
s = r.get_json()
check('stats report the index size', s['index_bytes'] > 0)
check('stats count the remaining file', s['files'] == 1)
r = client.delete('/api/fs/cache')
check('the cache can be purged', r.status_code == 200 and 'freed' in r.get_json())

print('\nUpload validation')
check('upload with no file is a 400',
      client.post('/api/fs/upload', data={}).status_code == 400)
check('upload into a missing folder is a 404',
      client.post('/api/fs/upload',
                  data={'parent': '4242', 'file': (open(__file__, 'rb'), 'x.py')},
                  content_type='multipart/form-data').status_code == 404)

print('\nPath traversal on the codec route')
# The guard here compared a normalised path against an unnormalised prefix,
# so whether it worked depended on how the scratch path happened to be
# spelled -- with forward slashes on Windows it rejected every upload.
for bad in ('../escape.txt', '..\\escape.txt', '/abs.txt', 'a/../../b.txt'):
    r = client.post('/encode',
                    data={'files': (open(__file__, 'rb'), bad)},
                    content_type='multipart/form-data')
    check(f'{bad!r} is refused', r.status_code == 400)

r = client.post('/encode',
                data={'files': (open(__file__, 'rb'), 'nested/dir/ok.py')},
                content_type='multipart/form-data')
check('a legitimate nested path is accepted',
      r.status_code == 200 and 'job_id' in r.get_json())

shutil.rmtree(_tmp, ignore_errors=True)

print()
if fails:
    print(f'{fails} check(s) FAILED')
    sys.exit(1)
print('All explorer API checks passed.')

"""Run workflows through the actual JSON HTTP service against disposable files."""

import json
import sqlite3
import sys
import time
from contextlib import closing
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from keelsplice.api import create_server

WORKFLOWS = ('catalog', 'recipe', 'retire', 'preview', 'capture', 'trace', 'build', 'certify', 'compare',
             'download', 'watch')


def seed(file):
    with closing(sqlite3.connect(file)) as conn:
        conn.executescript(Path(__file__).with_name('microscope.sql').read_text())


def call(base, route, body=None, status=200):
    headers = {'Content-Type': 'application/json'}
    if body is not None:
        body = json.dumps(body).encode('utf-8')
    try:
        response = urlopen(Request(base + route, body, headers), timeout=5)
    except HTTPError as exc:
        response = exc
    with response:
        data = response.read()
        assert response.status == status, (route, response.status, data[:500])
        if response.headers.get_content_type() == 'application/vnd.sqlite3':
            return data, response.headers['ETag'].strip('"')
        return json.loads(data)


def walkthrough(base, selected, source, root):
    def request(route, body=None, status=200):
        return call(base, route, body, status)

    def observe(name, result):
        if selected in (name, 'all'):
            print(name, json.dumps(result, sort_keys=True))
        return selected == name

    catalog = request('/sources/microscope/catalog')
    edge = next(edge for edge in catalog['schema']['edges'] if edge['child'] == 'assay' and edge['parent'] == 'substrate')
    assert edge['parent_cols'] == ['region','code']
    assert request('/sources/absent/catalog', status=404)['error'] == 'unknown_source'
    if observe('catalog', edge):
        return

    body = {'source': 'microscope', 'roots': [{'table': 'SUBSTRATE', 'key': ['east', 'MICA']}],
            'follow_children': [{'child': 'assay', 'id': edge['id']}], 'row_limit': 20}
    assert request('/recipes/swatch/versions', body | {'roots': [{'table': 'assay', 'key': []}]}, 422)['error'] == 'invalid_key'
    version = request('/recipes/swatch/versions', body, 201)
    assert version['version'] == 1 and version['recipe']['roots'][0]['table'] == 'substrate'
    assert request('/recipes/swatch/versions/1') == version
    if observe('recipe', version):
        return

    lookup = {'name': 'swatch', 'version': 1}
    request('/recipes/swatch/versions', body | {'row_limit': 1}, 201)
    if selected in ('retire', 'all'):
        retired = request('/recipes/swatch/versions/2/retire', {})
        assert retired['active'] is False and request('/preview', lookup | {'version': 2}, 409)['error'] == 'retired_recipe'
        if observe('retire', {'version': retired['version'], 'active': retired['active']}):
            return
    else:
        assert request('/preview', lookup | {'version': 2}, 422)['error'] == 'row_budget'

    preview = request('/preview', lookup)
    assert preview['summary']['counts'] == {'substrate': 1, 'cassette': 2, 'assay': 2, 'frame': 0}
    request('/recipes/tight/versions', body | {'row_limit': 1}, 201)
    assert request('/preview', {'name': 'tight','version': 1}, 422)['error'] == 'row_budget'
    if observe('preview', preview['summary']):
        return

    cap = request('/captures', lookup, 201)
    endpoint = '/captures/' + cap['id']
    assert request(endpoint) == cap and cap['state'] == 'sealed'
    assert cap['roots'] == [['substrate', ['East', 'Mica']]]
    assert request(endpoint + '/database', status=409)['error'] == 'fixture_not_certified'
    with closing(sqlite3.connect(source)) as conn, conn:
        conn.execute('UPDATE assay SET length_um=4.75 WHERE mark=?', ('S-01',))
    newer = request('/captures', lookup, 201)
    if observe('capture', cap['summary'] | {'state':cap['state']}):
        return

    traced = request(endpoint + '/trace', {'table': 'substrate','key':['East','Mica']})
    assert traced['roots'] == [0] and len(traced['causes']) == 2
    assert request(endpoint + '/trace', {'table': 'substrate','key':['east','MICA']}, 404)['error'] == 'row_not_captured'
    if observe('trace', traced):
        return

    assert request(endpoint + '/certify', {}, 409)['error'] == 'fixture_not_built'
    rebuilt = request(endpoint + '/build', {})
    assert rebuilt['state'] == 'built' and request(endpoint + '/build', {}) == rebuilt
    assert request(endpoint + '/database', status=409)['error'] == 'fixture_not_certified'
    if observe('build', {'state':rebuilt['state']}):
        return

    certified = request(endpoint + '/certify', {})
    certificate = certified['certificate']
    assert certified['state'] == 'certified' and certificate['counts'] == cap['summary']['counts']
    assert request(endpoint + '/certify', {}) == certified
    if observe('certify', certificate):
        return

    diff = request('/compare', {'left': cap['id'], 'right': newer['id']})
    assert diff['changes']['assay'] == {'added': [], 'removed': [], 'modified': [['S-01']]}
    with closing(sqlite3.connect(source)) as conn, conn:
        conn.execute('ALTER TABLE frame ADD COLUMN caption TEXT')
    assert request('/captures', lookup, 409)['error'] == 'schema_conflict'
    request('/recipes/swatch/versions', body, 201)
    changed = request('/captures', lookup | {'version': 3}, 201)
    assert request('/compare', {'left': cap['id'], 'right': changed['id']}, 409)['error'] == 'schema_conflict'
    if observe('compare', diff['changes']):
        return

    data, etag = request(endpoint + '/database')
    assert sha256(data).hexdigest() == etag == certificate['sha256']
    fixture = root / 'handoff.sqlite'
    fixture.write_bytes(data)
    with closing(sqlite3.connect(fixture.as_uri() + '?mode=ro', uri=True)) as conn:
        assert conn.execute('PRAGMA foreign_key_check').fetchall() == []
        row = conn.execute("SELECT length_um,doubled FROM assay WHERE mark='S-01'").fetchone()
        assert row == (3.25, 6.5)
        assert conn.execute('SELECT texture FROM substrate').fetchone()[0] == b'\x00\x00\xff'
        assert conn.execute('SELECT count(*) FROM frame').fetchone()[0] == 0
    observe('download', {'sha256': etag, 'source_changes_isolated': True, 'length_and_generated': row})
    watch(base, selected, source, root)


def recreate_assay(conn, with_foreign_key):
    """Rebuild the assay table, optionally without its substrate foreign key."""
    if with_foreign_key:
        conn.execute('''CREATE TABLE assay_new (
            mark TEXT PRIMARY KEY NOT NULL, region TEXT, code TEXT, cassette INTEGER NOT NULL REFERENCES cassette,
            length_um REAL NOT NULL, doubled REAL GENERATED ALWAYS AS (length_um * 2) STORED,
            FOREIGN KEY (region, code) REFERENCES substrate)''')
    else:
        conn.execute('''CREATE TABLE assay_new (
            mark TEXT PRIMARY KEY NOT NULL, region TEXT, code TEXT, cassette INTEGER NOT NULL REFERENCES cassette,
            length_um REAL NOT NULL, doubled REAL GENERATED ALWAYS AS (length_um * 2) STORED)''')
    conn.execute('INSERT INTO assay_new(mark,region,code,cassette,length_um) '
                 'SELECT mark,region,code,cassette,length_um FROM assay')
    conn.execute('DROP TABLE assay')
    conn.execute('ALTER TABLE assay_new RENAME TO assay')


def watch(base, selected, source, root):
    catalog = call(base, '/sources/microscope/catalog')
    edge = next(edge for edge in catalog['schema']['edges'] if edge['child'] == 'assay' and edge['parent'] == 'substrate')
    body = {'source': 'microscope', 'roots': [{'table': 'substrate', 'key': ['east', 'MICA']}],
            'follow_children': [{'child': 'assay', 'id': edge['id']}], 'row_limit': 20}
    call(base, '/recipes/edge-user/versions', body, 201)

    # Without a watch, catalog reads leave no check evidence.
    assert call(base, '/sources/microscope/check', {}, status=409)['error'] == 'no_watch'

    watch_body = {'source': 'microscope', 'kind': 'edge', 'child': 'assay', 'id': edge['id'],
                  'continuity_seconds': 1}
    created = _call(base, '/watches', watch_body, 'PUT', 201)
    watch_id = created['watch']['id']
    assert created['latest_state'] == 'verified'
    assert _call(base, '/watches', watch_body, 'PUT', 409)['error'] == 'watch_exists'

    # An unrelated index does not change the edge fingerprint.
    with closing(sqlite3.connect(source)) as conn, conn:
        conn.execute('CREATE INDEX assay_cassette_extra ON assay(cassette)')
    indexed = call(base, '/sources/microscope/check', {})
    detail = call(base, f'/watches/{watch_id}')
    assert [entry['state'] for entry in detail['timeline']] == ['verified', 'verified']
    assert indexed['notifications'] == []

    # A gap beyond the continuity window with the same structure reopens the range.
    time.sleep(1.05)
    call(base, '/sources/microscope/check', {})
    detail = call(base, f'/watches/{watch_id}')
    assert detail['timeline'][-1]['state'] == 'reopened'
    assert {segment['kind'] for segment in detail['segments']} >= {'verified', 'unobserved'}

    # Dropping the selected relationship is an observed change and notifies the recipe.
    # SQLite renumbers the surviving cassette foreign key to id 0, so the watched id
    # either disappears or resolves to a different column mapping.
    before = len(call(base, '/notifications')['notifications'])
    with closing(sqlite3.connect(source)) as conn, conn:
        recreate_assay(conn, with_foreign_key=False)
    dropped = call(base, '/sources/microscope/check', {})
    detail = call(base, f'/watches/{watch_id}')
    assert detail['timeline'][-1]['state'] in ('changed', 'missing')
    assert dropped['notifications'] and dropped['notifications'][0]['kind'] == 'changed'
    notifications = call(base, '/notifications')['notifications']
    assert len(notifications) == before + len(dropped['notifications'])
    changed = next(item for item in notifications if item['kind'] == 'changed')
    assert {'name': 'edge-user', 'version': 1, 'active': True, 'usage': ['follow_children']} in \
        changed['recipients']
    assert len(changed['evidence']) >= len(detail['timeline']) - 1

    # Restoring the edge is another observed change back to a real mapping.
    with closing(sqlite3.connect(source)) as conn, conn:
        recreate_assay(conn, with_foreign_key=True)
    call(base, '/sources/microscope/check', {})
    detail = call(base, f'/watches/{watch_id}')
    assert detail['timeline'][-1]['state'] == 'changed'

    # Hours without a readable source are explicit unreachable checks.
    moved = source.with_name('microscope-offline.sqlite')
    source.rename(moved)
    unreadable = call(base, '/sources/microscope/check', {})
    assert unreadable['check']['outcome'] == 'unreachable'
    first_unreachable = unreadable['notifications']
    assert first_unreachable and first_unreachable[0]['kind'] == 'unreachable'
    call(base, '/sources/microscope/check', {})
    assert len([n for n in call(base, '/notifications')['notifications'] if n['kind'] == 'unreachable']) == 1

    # Reopening the old notification still shows every catalog conclusion at that time.
    notice = call(base, f'/notifications/{first_unreachable[0]["id"]}')
    assert notice['check']['outcome'] == 'unreachable'
    assert [entry['state'] for entry in notice['evidence']][-1] == 'unreachable'
    call(base, f'/notifications/{first_unreachable[0]["id"]}/acknowledge', {})
    assert call(base, f'/notifications/{first_unreachable[0]["id"]}')['acknowledged'] is True

    # Once the source is back, an unobserved interval separates the conclusions;
    # the state is reopened when the watched mapping matches, or changed if SQLite
    # renumbered the foreign keys while the file was being rebuilt.
    moved.rename(source)
    call(base, '/sources/microscope/check', {})
    detail = call(base, f'/watches/{watch_id}')
    assert detail['timeline'][-1]['state'] in ('reopened', 'changed')

    # A primary-key watch catches a changed key definition on another table.
    pk_watch = _call(base, '/watches', {'source': 'microscope', 'kind': 'pk', 'table': 'frame'}, 'PUT', 201)
    with closing(sqlite3.connect(source)) as conn, conn:
        conn.execute('CREATE TABLE frame_new (frame_id INTEGER, mark TEXT REFERENCES assay, note TEXT NOT NULL, '
                     'PRIMARY KEY(frame_id, mark))')
        conn.execute('INSERT INTO frame_new(frame_id, mark, note) SELECT frame_id, mark, note FROM frame')
        conn.execute('DROP TABLE frame')
        conn.execute('ALTER TABLE frame_new RENAME TO frame')
    call(base, '/sources/microscope/check', {})
    pk_detail = call(base, f'/watches/{pk_watch["watch"]["id"]}')
    assert pk_detail['timeline'][-1]['state'] == 'changed'
    print('watch', json.dumps(
        {'edge_states': [entry['state'] for entry in detail['timeline']],
         'notifications': len(call(base, '/notifications')['notifications'])}, sort_keys=True))


def _call(base, route, body, method, status=200):
    headers = {'Content-Type': 'application/json'}
    data = json.dumps(body).encode('utf-8') if body is not None else None
    try:
        response = urlopen(Request(base + route, data, headers, method=method), timeout=5)
    except HTTPError as exc:
        response = exc
    with response:
        payload = response.read()
        assert response.status == status, (route, method, response.status, payload[:400])
        return json.loads(payload)


def main():
    selected = sys.argv[1] if len(sys.argv) == 2 else 'all'
    if selected not in (*WORKFLOWS, 'all'):
        raise SystemExit('Choose a workflow: ' + ', '.join(WORKFLOWS))
    with TemporaryDirectory(prefix='keelsplice-example-') as directory:
        root = Path(directory)
        source = root / 'microscope.sqlite'
        seed(source)
        with create_server(('127.0.0.1', 0), root / 'repo.sqlite', {'microscope': source}) as server:
            thread = Thread(target=server.serve_forever, kwargs={'poll_interval': 0.05})
            thread.start()
            try:
                walkthrough(f'http://127.0.0.1:{server.server_port}', selected, source, root)
            finally:
                server.shutdown()
                thread.join()


if __name__ == '__main__':
    main()

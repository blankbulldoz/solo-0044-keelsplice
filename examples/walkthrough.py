"""Run workflows through the actual JSON HTTP service against disposable files."""

import json
import sqlite3
import sys
from contextlib import closing
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from keelsplice.api import create_server

WORKFLOWS = ('catalog', 'recipe', 'retire', 'preview', 'capture', 'trace', 'build', 'certify',
             'compare', 'download', 'layouts')


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
    if observe('download', {'sha256': etag, 'source_changes_isolated': True, 'length_and_generated': row}):
        return
    layouts(base, request, cap['id'], data, certificate, root)


def contents(data, directory, name):
    """Canonical schema and typed row content of a handoff file."""
    file = directory / name
    file.write_bytes(data)
    with closing(sqlite3.connect(file.as_uri() + '?mode=ro', uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        ddl = [tuple(row) for row in conn.execute(
            'SELECT type,name,tbl_name,sql FROM sqlite_schema WHERE sql IS NOT NULL ORDER BY type,name')]
        tables = {}
        for table in conn.execute(
                "SELECT name FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"):
            tables[table[0]] = [tuple(row) for row in conn.execute(f'SELECT * FROM "{table[0]}" ORDER BY 1')]
    return ddl, tables


def layouts(base, request, capture_id, default_data, default_certificate, root):
    """Deliver a second authenticated layout without disturbing the first."""
    from keelsplice import bundle

    endpoint = '/captures/' + capture_id
    status = request(endpoint)
    assert status['layouts']['default']['state'] == 'certified'
    assert status['layouts']['wide']['state'] == 'pending'
    assert request(endpoint + '/database')[0] == default_data

    original = bundle.build

    def broken(sealed, layout='default'):
        if layout == 'wide':
            raise sqlite3.OperationalError('injected second-layout failure')
        return original(sealed, layout)

    bundle.build = broken
    try:
        failed = request(endpoint + '/build', {'layout': 'wide'}, 422)
        assert failed['error'] == 'artifact_build'
    finally:
        bundle.build = original
    after = request(endpoint)
    assert after['layouts']['default']['state'] == 'certified'
    assert after['layouts']['default']['certificate'] == default_certificate
    assert after['layouts']['wide']['state'] == 'pending'
    assert request(endpoint + '/database') == (default_data, default_certificate['sha256'])

    built = request(endpoint + '/build', {'layout': 'wide'})
    assert built['layouts']['wide']['state'] == 'built'
    assert built['state'] == 'certified' and built['certificate'] == default_certificate
    assert request('/captures/' + capture_id + '/database')[0] == default_data

    certified = request(endpoint + '/certify', {'layout': 'wide'})
    wide = certified['layouts']['wide']['certificate']
    assert certified['layouts']['wide']['state'] == 'certified'
    assert wide['layout'] == 'wide' and wide['page_size'] == 8192 and wide['free_pages'] == 8
    assert certified['layouts']['default']['certificate'] == default_certificate

    data, etag = request(endpoint + '/layouts/wide/database')
    assert sha256(data).hexdigest() == etag == wide['sha256'] and data != default_data
    assert request(endpoint + '/database') == (default_data, default_certificate['sha256'])

    wide_file = root / 'handoff-wide.sqlite'
    wide_file.write_bytes(data)
    with closing(sqlite3.connect(wide_file.as_uri() + '?mode=ro', uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        assert conn.execute('PRAGMA page_size').fetchone()[0] == 8192
        assert conn.execute('PRAGMA freelist_count').fetchone()[0] == 8
        assert conn.execute('PRAGMA integrity_check').fetchall()[0][0] == 'ok'
        assert conn.execute('PRAGMA foreign_key_check').fetchall() == []
        counted = {row['name']: row['n'] for row in conn.execute(
            'SELECT "substrate" AS name, count(*) AS n FROM substrate UNION ALL '
            'SELECT "assay", count(*) FROM assay UNION ALL '
            'SELECT "cassette", count(*) FROM cassette UNION ALL '
            'SELECT "frame", count(*) FROM frame')}
        assert counted == default_certificate['counts']
        assert conn.execute("SELECT length_um,doubled FROM assay WHERE mark='S-01'").fetchone()[:] == (3.25, 6.5)
        assert conn.execute('SELECT texture FROM substrate').fetchone()[0] == b'\x00\x00\xff'
        assert conn.execute('SELECT serial,prior FROM cassette ORDER BY serial').fetchall()[1][:] == (12, 11)
    assert wide['sha256'] != default_certificate['sha256']
    default_contents = contents(default_data, root, 'handoff-default.sqlite')
    assert contents(data, root, 'handoff-wide.sqlite') == default_contents
    assert default_contents[1]['assay'][0][1:5] == ('east', 'MICA', 11, 3.25)
    assert request(endpoint + '/layouts/default/database') == (default_data, default_certificate['sha256'])
    assert request(endpoint + '/build', {'layout': 'narrow'}, 404)['error'] == 'unknown_layout'
    print('layouts', json.dumps({'default': default_certificate['sha256'], 'wide': wide['sha256'],
                                 'equivalent': True}, sort_keys=True))


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

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

WORKFLOWS = ('catalog', 'recipe', 'retire', 'preview', 'capture', 'trace', 'build', 'certify', 'compare',
             'download', 'acceptance')


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

    if selected not in ('acceptance', 'all'):
        return
    fresh = request('/recipes/swatch/versions', body, 201)
    first = request('/captures', {'name': 'swatch', 'version': fresh['version']}, 201)
    request('/captures/' + first['id'] + '/build', {})
    request('/captures/' + first['id'] + '/certify', {})
    clauses = [
        {'id': 'ordinary', 'kind': 'single', 'requirements': [
            {'kind': 'row', 'table': 'substrate', 'key': ['east', 'MICA']}]},
        {'id': 'joint', 'kind': 'joint', 'requirements': [
            {'kind': 'row', 'table': 'substrate', 'key': ['East', 'Mica']},
            {'kind': 'link', 'edge': ['assay', edge['id']],
             'origin': {'table': 'assay', 'key': ['S-01']},
             'target': {'table': 'substrate', 'key': ['east', 'mica']}}]},
    ]
    session = request('/acceptance', {'name': 'swatch', 'version': fresh['version'],
                                      'groups': ['光谱组', '结构组'], 'clauses': clauses}, 201)
    session_id = session['id']
    assert session['schema_digest'] == fresh['schema_digest']
    assert request('/acceptance/' + session_id) == session
    assert session['clauses'][0]['requirements'][0]['key'] == ['East', 'Mica']
    assert request('/acceptance/' + session_id + '/cite',
                   {'clause': 'ordinary', 'capture': first['id'], 'group': '旁观组'}, 422)['error'] == 'invalid_group'
    request('/acceptance/' + session_id + '/cite',
            {'clause': 'ordinary', 'capture': first['id'], 'group': '光谱组'})
    judged = request('/acceptance/' + session_id)
    assert judged['clauses'][0]['status'] == 'satisfied'
    assert judged['clauses'][0]['combination']['reason'] == 'same_file'
    assert judged['clauses'][1]['reason'] == 'awaiting_citation'

    # A different snapshot of the same active version: S-01 now points at West/Basalt.
    with closing(sqlite3.connect(source)) as conn, conn:
        conn.execute("UPDATE assay SET region='West', code='Basalt' WHERE mark='S-01'")
    second = request('/captures', {'name': 'swatch', 'version': fresh['version']}, 201)
    request('/captures/' + second['id'] + '/build', {})
    request('/captures/' + second['id'] + '/certify', {})

    # Each snapshot holds one of the two links; separately certified, they do not make one file.
    link_body = {'source': 'microscope', 'roots': [{'table': 'assay', 'key': ['S-01']}], 'row_limit': 20}
    link_version = request('/recipes/link/versions', link_body, 201)
    def link_snapshot():
        sealed = request('/captures', {'name': 'link', 'version': link_version['version']}, 201)
        request('/captures/' + sealed['id'] + '/build', {})
        request('/captures/' + sealed['id'] + '/certify', {})
        return sealed['id']
    west = link_snapshot()  # S-01 → West/Basalt
    with closing(sqlite3.connect(source)) as conn, conn:
        conn.execute("UPDATE assay SET region='East', code='Mica' WHERE mark='S-01'")
    east = link_snapshot()  # S-01 → East/Mica
    split = request('/acceptance', {'name': 'link', 'version': link_version['version'],
                                    'groups': ['光谱组'], 'clauses': [
        {'id': 'split', 'kind': 'joint', 'requirements': [
            {'kind': 'link', 'edge': ['assay', edge['id']],
             'origin': {'table': 'assay', 'key': ['S-01']},
             'target': {'table': 'substrate', 'key': ['West', 'Basalt']}},
            {'kind': 'link', 'edge': ['assay', edge['id']],
             'origin': {'table': 'assay', 'key': ['S-01']},
             'target': {'table': 'substrate', 'key': ['East', 'Mica']}}]}]}, 201)
    split_id = split['id']
    request('/acceptance/' + split_id + '/cite', {'clause': 'split', 'capture': west, 'group': '光谱组'})
    combined = request('/acceptance/' + split_id + '/cite',
                       {'clause': 'split', 'capture': east, 'group': '光谱组'})
    verdict = combined['clauses'][0]
    assert verdict['status'] == 'gap' and verdict['combination']['reason'] == 'separate_files'
    assert len({piece['sha256'] for piece in verdict['combination']['pieces']}) == 2
    assert verdict['needed_sample']['action'] == 'seal'

    # A certificate from another version cannot be cited into the frozen session.
    older = request('/recipes/swatch/versions', body, 201)
    stale = request('/captures', {'name': 'swatch', 'version': older['version']}, 201)
    request('/captures/' + stale['id'] + '/build', {})
    request('/captures/' + stale['id'] + '/certify', {})
    refusal = request('/acceptance/' + session_id + '/cite',
                      {'clause': 'ordinary', 'capture': stale['id'], 'group': '光谱组'}, 409)
    assert refusal['error'] == 'citation_version'
    assert request('/acceptance/' + session_id + '/cite',
                   {'clause': 'joint', 'capture': 'absent', 'group': '光谱组'}, 404)['error'] == 'unknown_capture'
    observe('acceptance', {'session': session_id, 'satisfied': judged['summary']['satisfied'],
                           'split': verdict['combination']['reason'], 'older': stale['id']})


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

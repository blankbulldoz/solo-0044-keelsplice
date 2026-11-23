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

WORKFLOWS = ('catalog', 'recipe', 'retire', 'preview', 'capture', 'trace', 'build', 'certify', 'compare', 'download',
             'paths')


def seed(file):
    seed_with(file, 'microscope.sql')


def seed_with(file, script):
    with closing(sqlite3.connect(file)) as conn:
        conn.executescript(Path(__file__).with_name(script).read_text())


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

    if selected in ('paths', 'all'):
        board = paths(base, request)
        observe('paths', {'outcome': board['outcome'], 'shared_count': board['shared_count']})


def paths(base, request):
    def catalog_of(alias):
        return request(f'/sources/{alias}/catalog')

    def edge_list(schema):
        return [{'child': edge['child'], 'id': edge['id']} for edge in schema['edges']]

    crossroad = catalog_of('crossroad_a')['schema']
    tether = next(edge for edge in crossroad['edges'] if edge['child'] == 'tether')
    assert tether['parent'] == 'tether'
    allowed = edge_list(crossroad)
    body = {'start': 'hub', 'end': 'probe',
            'left': {'source': 'crossroad_a', 'edges': allowed},
            'right': {'source': 'crossroad_b', 'edges': allowed}}

    verdict = request('/paths', body)
    assert verdict['outcome'] == 'compatible' and verdict['shared_count'] == 1
    solution = verdict['shared'][0]
    assert solution['signature'] == [{'origin': 'hub', 'target': 'probe',
                                      'origin_cols': ['zone', 'code'], 'target_cols': ['zone', 'code']}]
    hub_edge = next(edge for edge in crossroad['edges'] if edge['child'] == 'probe' and edge['parent'] == 'hub')
    step = solution['left']['steps'][0]
    assert step['direction'] == 'child' and step['edge'] == {'child': 'probe', 'id': hub_edge['id']}
    assert verdict['left']['endpoints'] == {'start': 'hub', 'end': 'probe'}

    loop_only = [{'child': 'tether', 'id': tether['id']}]
    stuck = request('/paths', body | {'left': {'source': 'crossroad_a', 'edges': loop_only},
                                      'right': {'source': 'crossroad_b', 'edges': loop_only}})
    assert stuck['outcome'] == 'no_path' and stuck['left']['paths'] == 0

    duplicate = request('/paths', body | {'start': 'probe', 'end': 'spoke'})
    assert duplicate['outcome'] == 'ambiguous' and duplicate['shared_count'] == 2
    chosen = {tuple(sol['signature'][0]['origin_cols']) for sol in duplicate['shared']}
    assert chosen == {('spoke_id',), ('backup_spoke',)}

    microscope = catalog_of('microscope')['schema']
    drift = catalog_of('drift')['schema']
    drift_edge = next(edge for edge in drift['edges'] if edge['child'] == 'assay' and edge['parent'] == 'substrate')
    assert drift_edge['child_cols'] == ['area', 'code'] and drift_edge['parent_cols'] == ['region', 'code']
    mismatch = request('/paths', {'start': 'frame', 'end': 'substrate',
                                  'left': {'source': 'microscope', 'edges': edge_list(microscope)},
                                  'right': {'source': 'drift', 'edges': edge_list(drift)}})
    assert mismatch['outcome'] == 'mapping_mismatch' and mismatch['shared_count'] == 0
    assert mismatch['left']['paths'] == 1 and mismatch['right']['paths'] == 1
    assert mismatch['left']['sample'][0][1]['origin_cols'] == ['region', 'code']
    assert mismatch['right']['sample'][0][1]['origin_cols'] == ['area', 'code']

    ghost = request('/paths', body | {'start': 'ghost'})
    assert ghost['outcome'] == 'no_path' and ghost['left']['endpoints']['start'] is None

    assert request('/paths', body | {'end': 'HUB'}, 422)['error'] == 'invalid_endpoints'
    assert request('/paths', body | {'max_steps': 0}, 422)['error'] == 'invalid_steps'
    bad_edge = {'left': {'source': 'crossroad_a', 'edges': [{'child': 'probe', 'id': 99}]}}
    assert request('/paths', body | bad_edge, 422)['error'] == 'invalid_edge'
    bad_source = {'left': {'source': 'ghost', 'edges': []}}
    assert request('/paths', body | bad_source, 404)['error'] == 'unknown_source'
    return verdict


def main():
    selected = sys.argv[1] if len(sys.argv) == 2 else 'all'
    if selected not in (*WORKFLOWS, 'all'):
        raise SystemExit('Choose a workflow: ' + ', '.join(WORKFLOWS))
    with TemporaryDirectory(prefix='keelsplice-example-') as directory:
        root = Path(directory)
        source = root / 'microscope.sqlite'
        seed(source)
        seed_with(root / 'crossroad_a.sqlite', 'crossroad.sql')
        seed_with(root / 'crossroad_b.sqlite', 'crossroad.sql')
        seed_with(root / 'drift.sqlite', 'microscope_drift.sql')
        sources = {'microscope': source, 'drift': root / 'drift.sqlite',
                   'crossroad_a': root / 'crossroad_a.sqlite', 'crossroad_b': root / 'crossroad_b.sqlite'}
        with create_server(('127.0.0.1', 0), root / 'repo.sqlite', sources) as server:
            thread = Thread(target=server.serve_forever, kwargs={'poll_interval': 0.05})
            thread.start()
            try:
                walkthrough(f'http://127.0.0.1:{server.server_port}', selected, source, root)
            finally:
                server.shutdown()
                thread.join()


if __name__ == '__main__':
    main()

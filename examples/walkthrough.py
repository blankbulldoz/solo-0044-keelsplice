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

WORKFLOWS = ('catalog', 'recipe', 'retire', 'preview', 'capture', 'trace', 'build', 'certify', 'compare', 'ledger', 'ledger-kinds', 'download')


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
    if observe('compare', diff['changes']):
        return

    ledger = request('/ledgers', {'name': 'assay-review', 'baseline': cap['id']}, 201)
    assert ledger['ledger']['baseline'] == cap['id']
    assert request('/ledgers', {'name': 'assay-review', 'baseline': cap['id']}, 409)['error'] == 'ledger_exists'
    first = request('/ledgers/assay-review/comparisons', {'candidate': newer['id']}, 201)
    assert [item['kind'] for item in first['items']] == ['modified']
    item = first['items'][0]
    assert item['baseline_side'] and item['candidate_side'] and item['changed'] == ['length_um', 'doubled']
    reviewed = request('/comparisons/' + first['id'] + '/reviews',
                       {'item': item['token'], 'verdict': 'confirmed', 'note': '已核对长度变化'}, 201)
    assert reviewed['reviews'][item['token']]['verdict'] == 'confirmed'
    assert request('/comparisons/' + first['id'] + '/reviews',
                   {'item': 'nope', 'verdict': 'confirmed'}, 404)['error'] == 'unknown_item'
    assert request('/ledgers/assay-review/comparisons', {'candidate': cap['id']}, 201)['items'] == []
    repeat = request('/ledgers/assay-review/comparisons', {'candidate': newer['id']}, 201)
    carried = request('/ledgers/assay-review')
    assert carried['current']['items'][0]['state'] == 'carried'
    assert carried['current']['items'][0]['prior_reviews'][0]['comparison'] == first['id']
    assert request('/ledgers/assay-review/rebase', {'baseline': newer['id']})
    if observe('ledger', {'item': item['kind'], 'carried': carried['current']['items'][0]['state']}):
        return

    with closing(sqlite3.connect(source)) as conn, conn:
        conn.execute("UPDATE assay SET region='West', code='Basalt' WHERE mark='S-03'")
    kinds_body = {'source': 'microscope',
                  'roots': [{'table': 'substrate', 'key': ['east', 'MICA']},
                            {'table': 'substrate', 'key': ['west', 'BASALT']}],
                  'follow_children': [{'child': 'assay', 'id': edge['id']}, {'child': 'frame', 'id': 0}],
                  'row_limit': 60}
    request('/recipes/kinds/versions', kinds_body, 201)
    kind_base = request('/captures', {'name': 'kinds', 'version': 1}, 201)
    request('/ledgers', {'name': 'kind-review', 'baseline': kind_base['id']}, 201)
    with closing(sqlite3.connect(source)) as conn, conn:
        conn.execute("UPDATE assay SET region='East', code='Mica' WHERE mark='S-03'")
        conn.execute("UPDATE assay SET length_um=8.5 WHERE mark='S-02'")
        conn.execute("INSERT INTO frame VALUES (300,'S-01','added')")
        conn.execute("DELETE FROM frame WHERE frame_id=101")
    kind_next = request('/captures', {'name': 'kinds', 'version': 1}, 201)
    covered = request('/ledgers/kind-review/comparisons', {'candidate': kind_next['id']}, 201)
    kinds = {entry['kind'] for entry in covered['items']}
    assert kinds == {'added', 'removed', 'modified', 'retarget'}, kinds
    retarget = next(entry for entry in covered['items'] if entry['kind'] == 'retarget')
    relation = retarget['relations'][0]
    assert relation['before'] == ['West', 'Basalt'] and relation['after'] == ['East', 'Mica']
    assert relation['parent_baseline'] and relation['parent_candidate']
    added = next(entry for entry in covered['items'] if entry['kind'] == 'added')
    removed = next(entry for entry in covered['items'] if entry['kind'] == 'removed')
    assert added['baseline_side'] is False and added['candidate_side'] is True
    assert removed['baseline_side'] is True and removed['candidate_side'] is False

    with closing(sqlite3.connect(source)) as conn, conn:
        conn.execute("UPDATE assay SET region='West', code='Basalt' WHERE mark='S-03'")
        conn.execute("UPDATE assay SET length_um=5.5 WHERE mark='S-02'")
        conn.execute("INSERT INTO frame VALUES (101,'S-01','front')")
        conn.execute("DELETE FROM frame WHERE frame_id=300")
    restored = request('/captures', {'name': 'kinds', 'version': 1}, 201)
    request('/ledgers/kind-review/comparisons', {'candidate': restored['id']}, 201)
    settled = request('/ledgers/kind-review')
    assert settled['current']['items'] == []
    assert {entry['kind'] for entry in settled['withdrawn']} == {'added', 'removed', 'modified', 'retarget'}
    assert request('/ledgers/kind-review/rebase', {'baseline': restored['id']})
    if observe('ledger-kinds', {'kinds': sorted(kinds), 'withdrawn': len(settled['withdrawn'])}):
        return

    with closing(sqlite3.connect(source)) as conn, conn:
        conn.execute('ALTER TABLE frame ADD COLUMN caption TEXT')
    assert request('/captures', lookup, 409)['error'] == 'schema_conflict'
    request('/recipes/swatch/versions', body, 201)
    changed = request('/captures', lookup | {'version': 3}, 201)
    assert request('/compare', {'left': cap['id'], 'right': changed['id']}, 409)['error'] == 'schema_conflict'
    assert request('/ledgers/assay-review/comparisons', {'candidate': changed['id']}, 409)['error'] == 'schema_conflict'
    assert repeat['ordinal'] == 3

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

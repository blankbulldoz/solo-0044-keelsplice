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
             'deliver')


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
    if selected == 'download':
        return

    batch = request('/batches', {'label': '2026-W47 周交付'}, 201)
    batch_id = batch['id']
    assert batch['state'] == 'open' and batch['entries'] == []
    assert request('/batches', {'label': 'overlap'}, 409)['error'] == 'batch_still_open'
    deliveries = f'/batches/{batch_id}/deliveries'
    request(deliveries, {'capture': cap['id'], 'recipient': '甲组'}, 201)
    request(deliveries, {'capture': cap['id'], 'recipient': '乙组'}, 201)
    added = request(deliveries, {'capture': newer['id'], 'recipient': '甲组'}, 201)
    assert [entry['kind'] for entry in added['entries']] == ['initial'] * 3
    assert request(deliveries, {'capture': cap['id'], 'recipient': '甲组'}, 409)['error'] == 'duplicate_delivery'
    assert request(deliveries, {'capture': 'missing', 'recipient': '甲组'}, 404)['error'] == 'unknown_capture'
    assert request(f'/batches/{batch_id}/close', {}, 409)['error'] == 'batch_entries_pending'
    entries = {(entry['capture'], entry['recipient']): entry['id'] for entry in added['entries']}

    def decide(key, body, status=200):
        return request(f'{deliveries}/{entries[key]}/decide', body, status)

    settled, late, held = (cap['id'], '甲组'), (cap['id'], '乙组'), (newer['id'], '甲组')
    assert decide(held, {'action': 'settle'}, 409)['error'] == 'deliver_not_certified'
    assert decide(held, {'action': 'hold'}, 422)['error'] == 'hold_needs_reason'
    assert decide(settled, {'action': 'review'}, 422)['error'] == 'invalid_action'
    decide(settled, {'action': 'settle', 'note': '回执 R-101'})
    decide(late, {'action': 'continue'})
    decide(held, {'action': 'hold', 'note': '子库未认证，下周再交'})
    closed = request(f'/batches/{batch_id}/close', {})
    assert closed['state'] == 'closed'
    outcome = {(entry['capture'], entry['recipient']): entry for entry in closed['entries']}
    assert outcome[settled]['outcome'] == 'delivered'
    assert outcome[settled]['sent'] == {'sha256': certificate['sha256'], 'bytes': certificate['bytes']}
    assert outcome[late]['outcome'] == 'reissue' and '回执' in outcome[late]['outcome_reason']
    assert outcome[held]['outcome'] == 'held' and outcome[held]['outcome_reason'] == '子库未认证，下周再交'
    assert request(deliveries, {'capture': cap['id'], 'recipient': '丙组'}, 409)['error'] == 'batch_closed'
    assert request(f'/batches/{batch_id}/close', {}, 409)['error'] == 'batch_closed'
    again, again_etag = request(endpoint + '/database')
    assert again_etag == certificate['sha256'] and sha256(again).hexdigest() == certificate['sha256']

    follow = request('/batches', {'label': '2026-W48 周交付'}, 201)
    assert len(follow['entries']) == 1
    carried = follow['entries'][0]
    assert carried['capture'] == cap['id'] and carried['recipient'] == '乙组' and carried['kind'] == 'reissue'
    follow_id = follow['id']
    request(f'/batches/{follow_id}/deliveries/{carried["id"]}/decide', {'action': 'settle'})
    done = request(f'/batches/{follow_id}/close', {})
    assert done['entries'][0]['outcome'] == 'delivered' and done['entries'][0]['sent']['sha256'] == certificate['sha256']
    review = request(f'/batches/{batch_id}')
    assert review['state'] == 'closed' and review['entries'][0]['sent']['sha256'] == certificate['sha256']
    listing = request('/batches')
    assert [item['label'] for item in listing['batches']][:2] == ['2026-W48 周交付', '2026-W47 周交付']
    observe('deliver', {'settled': outcome[settled]['sent'], 'reissue': outcome[late]['outcome_reason'],
                        'carried': carried['kind'], 'independent_claim': again_etag})


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

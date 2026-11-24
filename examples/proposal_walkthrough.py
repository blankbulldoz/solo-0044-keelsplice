"""Verify draft foreign-key evaluation with a hand-built onboarding database.

The database in ``examples/onboarding.sql`` declares no foreign keys on purpose.
Each candidate pair is a constructed positive or negative case, so every verdict
and every catalog/row finding can be re-checked against the SQL file itself.
"""

import sqlite3
import sys
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from urllib.request import Request, urlopen
import json

from keelsplice.api import create_server

SCENARIOS = ('ready', 'catalog_block', 'data_block', 'nocase', 'null_exempt', 'invalid')


def seed(file):
    with closing(sqlite3.connect(file)) as conn:
        conn.executescript(Path(__file__).with_name('onboarding.sql').read_text())


def call(base, body, status=200):
    request = Request(base + '/proposals/evaluate', json.dumps(body).encode('utf-8'),
                      {'Content-Type': 'application/json'})
    try:
        response = urlopen(request, timeout=5)
    except Exception as exc:  # urllib raises HTTPError for non-2xx
        response = exc
    with response:
        data = json.loads(response.read())
        assert response.status == status, (body, response.status, data)
        return data


def codes(result, scope):
    return [item['code'] for item in result[f'{scope}_findings']]


def blocking(result, scope):
    return sorted(result[f'{scope}_blocking'])


def evaluate(base):
    common = dict(source='onboarding')

    # Positive: declared PK parent, all complete children resolve once.
    ready = call(base, common | dict(child='reading', parent='device',
                                     child_cols=['device_serial'], parent_cols=['serial']))
    assert ready['verdict'] == 'ready', ready
    assert 'parent_key_guaranteed' in codes(ready, 'catalog')
    assert 'parent_key_not_guaranteed' not in codes(ready, 'catalog')
    assert blocking(ready, 'catalog') == [] and blocking(ready, 'row') == []
    assert ready['metrics']['null_component_rows'] == 1
    assert 'null_component_exempt' in codes(ready, 'row')
    print('ready', json.dumps(ready['metrics'], sort_keys=True))
    if sys.argv[1] == 'ready':
        return

    # Catalog block: NOCASE asset key unique-ish in sample but no unique constraint.
    catalog_block = call(base, common | dict(child='ticket', parent='asset',
                                             child_cols=['asset_tag'], parent_cols=['tag']))
    assert catalog_block['verdict'] == 'blocked_catalog', catalog_block
    assert 'parent_key_not_guaranteed' in blocking(catalog_block, 'catalog')
    # Duplicate NOCASE tag and ambiguous child are still reported as row evidence.
    assert 'duplicate_parent_keys' in codes(catalog_block, 'row')
    assert 'ambiguous_child_keys' in codes(catalog_block, 'row')
    print('catalog_block', catalog_block['catalog_blocking'], catalog_block['row_blocking'])
    if sys.argv[1] == 'catalog_block':
        return

    # Data block: ledger code has no constraint (catalog) and payment P-9 is an orphan.
    data_block = call(base, common | dict(child='payment', parent='ledger',
                                          child_cols=['account_code'], parent_cols=['account_code']))
    assert data_block['verdict'] == 'blocked_catalog', data_block
    assert 'orphan_child_keys' in codes(data_block, 'row')
    orphan = next(item for item in data_block['row_findings'] if item['code'] == 'orphan_child_keys')
    assert orphan['evidence']['count'] == 1 and orphan['evidence']['witnesses'] == [['P-9']]
    print('data_block', orphan['evidence'])
    if sys.argv[1] == 'data_block':
        return

    # NOCASE-only matches are visible when the parent key actually has a guarantee.
    # Build a guaranteed NOCASE parent in a fresh view via a unique-indexed table.
    with closing(sqlite3.connect(base_source)) as conn, conn:
        conn.executescript("""
            CREATE TABLE host(host_id INTEGER PRIMARY KEY, name TEXT COLLATE NOCASE NOT NULL, detail TEXT);
            CREATE UNIQUE INDEX host_name_key ON host(name);
            INSERT INTO host VALUES (1, 'Alpha','one');
            CREATE TABLE mount(mount_id INTEGER PRIMARY KEY, host_name TEXT);
            INSERT INTO mount VALUES (1, 'alpha'), (2, NULL);
        """)
    nocase = call(base, common | dict(child='mount', parent='host',
                                      child_cols=['host_name'], parent_cols=['name']))
    assert nocase['verdict'] == 'ready', nocase
    assert 'nocase_only_matches' in codes(nocase, 'row')
    note = next(item for item in nocase['row_findings'] if item['code'] == 'nocase_only_matches')
    assert note['evidence']['witnesses'] == [['alpha']]
    assert nocase['metrics']['semantic_matches'] == 1 and nocase['metrics']['binary_matches'] == 0
    print('nocase', json.dumps(nocase['metrics'], sort_keys=True))
    if sys.argv[1] == 'nocase':
        return

    # NULL composite components are exempt rather than counted as missing parents.
    with closing(sqlite3.connect(base_source)) as conn, conn:
        conn.executescript("""
            CREATE TABLE hub(region TEXT NOT NULL, code TEXT NOT NULL, PRIMARY KEY(region, code)) WITHOUT ROWID;
            INSERT INTO hub VALUES ('east', 'mica');
            CREATE TABLE link(link_id INTEGER PRIMARY KEY, region TEXT, code TEXT,
                              FOREIGN KEY(region, code) REFERENCES hub(region, code));
            INSERT INTO link VALUES (1, 'east', 'mica'), (2, 'west', NULL);
        """)
    null_case = call(base, common | dict(child='link', parent='hub',
                                         child_cols=['region', 'code'],
                                         parent_cols=['region', 'code']))
    assert null_case['verdict'] == 'ready', null_case
    metrics = null_case['metrics']
    assert metrics['child_rows'] == 2 and metrics['null_component_rows'] == 1
    assert metrics['complete_child_keys'] == 1 and metrics['orphans'] == 0
    assert 'null_component_exempt' in codes(null_case, 'row')
    print('null_exempt', json.dumps(metrics, sort_keys=True))
    if sys.argv[1] == 'null_exempt':
        return

    # Malformed requests are rejected without touching a capture.
    bad = call(base, dict(source='onboarding', child='payment'), status=400)
    assert bad['error'] == 'invalid_request'
    unknown = call(base, common | dict(child='nope', parent='ledger',
                                       child_cols=['account_code'], parent_cols=['account_code']), status=422)
    assert unknown['error'] == 'unknown_identifier'
    dimension = call(base, common | dict(child='payment', parent='ledger',
                                         child_cols=['account_code', 'amount_cents'],
                                         parent_cols=['account_code']))
    assert 'dimension_mismatch' in blocking(dimension, 'catalog')
    print('invalid', bad['error'], unknown['error'], dimension['catalog_blocking'])


def main():
    selected = sys.argv[1] if len(sys.argv) == 2 else 'all'
    if selected not in (*SCENARIOS, 'all'):
        raise SystemExit('Choose a scenario: ' + ', '.join(SCENARIOS))
    global base_source
    with TemporaryDirectory(prefix='keelsplice-onboarding-') as directory:
        root = Path(directory)
        base_source = str(root / 'onboarding.sqlite')
        seed(base_source)
        with create_server(('127.0.0.1', 0), root / 'state.sqlite',
                           {'onboarding': Path(base_source)}) as server:
            thread = Thread(target=server.serve_forever, kwargs={'poll_interval': 0.05})
            thread.start()
            try:
                evaluate(f'http://127.0.0.1:{server.server_port}')
            finally:
                server.shutdown()
                thread.join()


if __name__ == '__main__':
    main()

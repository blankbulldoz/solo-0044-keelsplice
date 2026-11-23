"""Receiving-device registration and physical trial delivery over real HTTP.

The walkthrough seeds the authenticated ``handoff_lab`` source, builds and
certifies a capture, registers two receiving devices with genuinely different
64-bit integer / real / BLOB representations, imports the relay payload each
device can actually consume, and reopens the resulting database through an
independent read-only SQLite connection.

Success is never inferred from row counts. The checks assert primary-key
distinguishability, composite foreign-key parent resolution, and negative-zero
/ NULL fidelity, and name the relationships damaged whenever a representation
cannot carry a value.
"""

import base64
import json
import math
import sqlite3
from contextlib import closing
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from keelsplice.api import create_server

FIXTURE = Path(__file__).with_name('handoff_lab.sql')


def seed(file):
    with closing(sqlite3.connect(file)) as conn:
        conn.executescript(FIXTURE.read_text())


def call(base, route, body=None, status=200):
    headers = {'Content-Type': 'application/json'}
    data = None if body is None else json.dumps(body).encode('utf-8')
    try:
        response = urlopen(Request(base + route, data, headers), timeout=10)
    except HTTPError as exc:
        response = exc
    with response:
        payload = response.read()
        assert response.status == status, (route, response.status, payload[:500])
        kind = response.headers.get_content_type()
        if kind == 'application/vnd.sqlite3':
            return payload, response.headers['ETag'].strip('"')
        return payload if kind != 'application/json' else json.loads(payload)


def encode_key(value):
    if isinstance(value, float):
        return {'real': value.hex()}
    if isinstance(value, bytes):
        return {'blob': base64.b64encode(value).decode('ascii')}
    return value


def independent_reopen_checks(database, sealed_expectations):
    """Reopen with a brand new read-only connection and assert each invariant."""
    with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        assert [row[0] for row in conn.execute('PRAGMA main.integrity_check')] == ['ok']
        # 1. Primary keys remain distinguishable after reopening.
        slots = conn.execute('SELECT slot, typeof(slot) FROM token').fetchall()
        classes = {row[1] for row in slots}
        return slots, classes, conn.execute('PRAGMA main.foreign_key_check').fetchall()


def run(base, root, source):
    # --- Device registry: builtins are present and a malformed profile is refused.
    registry = call(base, '/receivers')['receivers']
    names = {item['name'] for item in registry}
    assert names == {'typed-relay', 'text-relay'}, names
    typed = next(item['profile'] for item in registry if item['profile']['relay'] == 'typed')
    text_relay = next(item['profile'] for item in registry if item['profile']['relay'] == 'text')
    assert {'integer', 'real', 'text', 'blob', 'null'} <= set(typed['storage_classes'])
    assert set(text_relay['storage_classes']) == {'text', 'null'}
    bad = call(base, '/receivers', {'name': 'broken', 'relay': 'text', 'storage_classes': ['integer']}, 422)
    assert bad['error'] == 'invalid_receiver'
    custom = call(base, '/receivers', {
        'name': 'hex-blob-relay', 'relay': 'text',
        'storage_classes': ['text', 'null', 'blob'], 'blob_carrier': 'hex',
        'null_sentinel': 'literal_null', 'notes': '文本通道但用十六进制承载 BLOB'}, 201)
    assert custom['builtin'] is False
    duplicate = call(base, '/receivers', {
        'name': 'hex-blob-relay', 'relay': 'text', 'storage_classes': ['text', 'null']}, 409)
    assert duplicate['error'] == 'receiver_exists'

    # --- Build a real, certified capture from the authenticated source.
    with closing(sqlite3.connect(source)) as conn:
        token_keys = [encode_key(row[0]) for row in conn.execute('SELECT slot FROM token')]
        origin_keys = [list(row) for row in conn.execute('SELECT region, code FROM token_origin')]
        node_ids = [row[0] for row in conn.execute('SELECT node_id FROM node')]
    catalog = call(base, '/sources/lab/catalog')
    edges = [{'child': edge['child'], 'id': edge['id']} for edge in catalog['schema']['edges']]
    roots = [{'table': 'token', 'key': [value]} for value in token_keys]
    roots += [{'table': 'token_origin', 'key': value} for value in origin_keys]
    roots += [{'table': 'node', 'key': [value]} for value in node_ids]
    # p-05 has only NULL foreign keys, so it is not reachable through closure and
    # is selected explicitly to test NULL fidelity end to end.
    roots += [{'table': 'probe', 'key': ['p-05']}]
    version = call(base, '/recipes/lab/versions',
                   {'source': 'lab', 'roots': roots, 'follow_children': edges, 'row_limit': 200}, 201)
    assert version['version'] == 1
    capture = call(base, '/captures', {'name': 'lab', 'version': 1}, 201)
    capture_id = capture['id']
    call(base, f'/captures/{capture_id}/build', {}, 200)
    certified = call(base, f'/captures/{capture_id}/certify', {}, 200)
    assert certified['state'] == 'certified'

    # --- Physical trial delivery to the typed relay: must be fully faithful.
    typed_trial = call(base, f'/captures/{capture_id}/trials', {'device': 'typed-relay'}, 201)
    typed_id, typed_report = typed_trial['id'], typed_trial['report']
    assert typed_report['verdict'] == 'passed', json.dumps(typed_report, ensure_ascii=False)
    assert typed_report['row_counts_match'] is True
    assert typed_report['rejected_rows'] == []
    assert typed_report['primary_key_findings'] == []
    assert typed_report['relationship_findings'] == []
    assert typed_report['foreign_key_violations'] == []
    assert all(not typed_report['value_findings'][key]
               for key in ('null', 'negative_zero', 'blob', 'int64', 'text', 'real'))

    typed_db = root / 'typed-relay.sqlite'
    typed_db.write_bytes(call(base, f'/captures/{capture_id}/database')[0])
    # Reopen independently: all six token identities survive with native classes.
    slots, classes, fk = independent_reopen_checks(typed_db, None)
    assert len(slots) == 6 and {'integer', 'real', 'text', 'blob'} <= classes, (slots, classes)
    assert fk == []
    with closing(sqlite3.connect(typed_db.as_uri() + '?mode=ro', uri=True)) as conn:
        # Negative zero keeps its IEEE sign bit only in the no-affinity column;
        # a REAL-affinity column normalizes -0.0 to +0.0 by SQLite's own rules.
        raw = conn.execute("SELECT raw_reading, typeof(raw_reading) FROM probe WHERE probe_id='p-01'").fetchone()
        assert raw[1] == 'real' and math.copysign(1.0, raw[0]) < 0, raw
        reading = conn.execute("SELECT reading, typeof(reading) FROM probe WHERE probe_id='p-01'").fetchone()
        assert reading[1] == 'real' and math.copysign(1.0, reading[0]) > 0, reading
        # NULL stays NULL, distinct from a genuine empty string.
        null_reading = conn.execute("SELECT reading IS NULL FROM probe WHERE probe_id='p-05'").fetchone()[0]
        assert null_reading == 1
        empty_note = conn.execute("SELECT typeof(note) FROM probe WHERE probe_id='p-07'").fetchone()[0]
        assert empty_note == 'text'
        # int64 extremes survive as integers.
        extremes = conn.execute('SELECT node_id, typeof(node_id) FROM node ORDER BY node_id').fetchall()
        assert (-9223372036854775808, 'integer') in [tuple(row) for row in extremes]
        assert (9223372036854775807, 'integer') in [tuple(row) for row in extremes]
        # The integer 9007199254740993 (beyond exact double precision) stays an
        # integer and remains a distinct no-affinity primary key from text '1'.
        precision_row = conn.execute(
            "SELECT typeof(slot) FROM token WHERE slot = 9007199254740993").fetchone()
        assert precision_row == ('integer',), precision_row
        distinct = conn.execute(
            "SELECT count(*) FROM token WHERE slot IN (1, '1')").fetchone()[0]
        assert distinct == 2, distinct
        # Composite NOCASE FK still resolves to the original parent rows through
        # an actual join, not just PRAGMA foreign_key_check.
        joined = conn.execute("""
            SELECT p.probe_id, o.region, o.code
            FROM probe p JOIN token_origin o
              ON o.region = p.region AND o.code = p.code
            WHERE p.probe_id = 'p-02'
        """).fetchone()
        assert tuple(joined) == ('p-02', 'East', 'Mica'), joined
        # A child whose composite component is NULL has no required parent and
        # is still present after reopening.
        nullable = conn.execute(
            "SELECT region, code FROM probe WHERE probe_id='p-03'").fetchone()
        assert tuple(nullable) == ('West', None), nullable
        assert conn.execute('PRAGMA foreign_key_check').fetchall() == []

    # --- The typed relay payload itself is importable JSON with typed carriers.
    from urllib.request import urlopen as _urlopen
    with _urlopen(Request(base + f'/trials/{typed_id}/payload'), timeout=10) as response:
        payload_bytes = response.read()
        payload_etag = response.headers['ETag'].strip('"')
    assert sha256(payload_bytes).hexdigest() == typed_report['payload_sha256'] == payload_etag
    payload = json.loads(payload_bytes)
    assert any(isinstance(cell, dict) and 'real' in cell for row in payload['tables']['probe'] for cell in row)

    # --- Physical trial delivery to the text relay: representation gaps must surface.
    text_trial = call(base, f'/captures/{capture_id}/trials', {'device': 'text-relay'}, 201)
    text_id, text_report = text_trial['id'], text_trial['report']
    assert text_report['verdict'] == 'failed'
    # Row counts alone cannot hide the damage.
    assert text_report['row_counts_match'] is False
    # The no-affinity token PK collapses: integer 1 and text '1' collide after
    # affinity-free text storage, and the blob token cannot be carried.
    token_pk = next(item for item in text_report['primary_key_findings'] if item['table'] == 'token')
    assert token_pk['lost_primary_keys'], token_pk
    # Composite FK damage is attributed to the exact edge and child rows.
    damaged_edges = {tuple(item['edge']) for item in text_report['relationship_findings']}
    assert ('probe', 1) in damaged_edges and ('probe', 0) in damaged_edges
    # The value-level report names NULL, negative-zero, BLOB and int64 losses.
    vf = text_report['value_findings']
    assert vf['null'], 'NULL sentinel collision must be reported'
    assert vf['negative_zero'], 'negative zero text degradation must be reported'
    assert vf['blob'], 'BLOB carrier gap must be reported'
    assert vf['int64'], 'int64 identity loss must be reported'
    assert any(col['foreign_key_columns'] == ['slot'] for col in vf['relationships'])
    assert text_report['unsupported_cells'], 'BLOB cells must be flagged as unsupported'

    # Reopen the text-relay database independently and confirm the claims.
    text_db = root / 'text-relay.sqlite'
    text_db.write_bytes(call(base, f'/trials/{text_id}/database')[0])
    with closing(sqlite3.connect(text_db.as_uri() + '?mode=ro', uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        # INTEGER PRIMARY KEY rejects non-integral numeric text outright.
        node_count = conn.execute('SELECT count(*) FROM node').fetchone()[0]
        assert node_count == 0, node_count
        # The token table loses the 1 vs '1' distinction and the blob identity.
        token_count = conn.execute('SELECT count(*) FROM token').fetchone()[0]
        assert token_count < 6
        # NULL became the empty-string sentinel in the no-affinity column.
        row = conn.execute("SELECT raw_reading, typeof(raw_reading) FROM probe WHERE probe_id='p-04'").fetchone()
        assert row is not None and row[1] == 'text' and row[0] == ''
        # Negative zero cannot survive the text relay in the no-affinity column.
        raw = conn.execute("SELECT raw_reading, typeof(raw_reading) FROM probe WHERE probe_id='p-01'").fetchone()
        assert raw[1] == 'text' and raw[0] == '-0'

    # --- A third, BLOB-capable text device shows the capability gap is device-specific.
    hex_trial = call(base, f'/captures/{capture_id}/trials', {'device': 'hex-blob-relay'}, 201)
    hex_report = hex_trial['report']
    assert hex_report['verdict'] == 'failed'  # numeric/NULL gaps remain
    assert all(cell['storage_class'] != 'blob' for cell in hex_report['unsupported_cells'])

    print('接收试交付检查通过：设备登记、双方可导入载荷、独立重开复核、')
    print('主键可区分性、复合外键父行解析、负零与空值保真、关系影响均已逐设备判定。')
    print(json.dumps({'typed_relay': typed_report['verdict'],
                      'text_relay': text_report['verdict'],
                      'text_relay_rejected_rows': len(text_report['rejected_rows']),
                      'text_relay_value_findings': {k: len(v) for k, v in vf.items()}},
                     ensure_ascii=False))


def main():
    with TemporaryDirectory(prefix='keelsplice-receiver-') as directory:
        root = Path(directory)
        source = root / 'handoff_lab.sqlite'
        seed(source)
        with create_server(('127.0.0.1', 0), root / 'state.sqlite', {'lab': source}) as server:
            thread = Thread(target=server.serve_forever, kwargs={'poll_interval': 0.05})
            thread.start()
            try:
                run(f'http://127.0.0.1:{server.server_port}', root, source)
            finally:
                server.shutdown()
                thread.join()


if __name__ == '__main__':
    main()

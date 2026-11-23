"""Receiving-device capability profiles, relay representations and trial delivery.

Two concrete relay kinds model the receivers on the bench:

* ``typed`` relays every SQLite storage class distinctly, so a sealed row can be
  reloaded with its integer/real/text/BLOB/NULL identity intact.
* ``text`` relays only text scalars. Numeric text is re-interpreted by the target
  column's declared SQLite affinity, BLOBs have no native carrier, and NULL is
  mapped to a configurable sentinel.

A trial never treats equal row counts as success: the produced database is
reopened through a separate read-only connection and checked for primary-key
distinguishability, composite foreign-key parent resolution, negative-zero and
NULL fidelity, and full value round trips.
"""

import base64
import math
import sqlite3
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

from .catalog import inspect
from .codec import Fault, decode, digest, dumps, fingerprint, key, quote, row_values

RELAY_KINDS = ('typed', 'text')
STORAGE_CLASSES = ('integer', 'real', 'text', 'blob', 'null')
REAL_FORMATS = ('exact', 'decimal15')
NULL_SENTINELS = ('empty_string', 'literal_null')
BLOB_CARRIERS = ('base64', 'hex', 'none')
MAX_PAYLOAD_BYTES = 8 * 1024 * 1024


def validate_profile(body):
    if not isinstance(body, dict):
        raise Fault('invalid_receiver', 'Receiving device profile must be an object', 422)
    allowed = {'name', 'relay', 'storage_classes', 'real_format', 'null_sentinel', 'blob_carrier', 'notes'}
    if not set(body) <= allowed:
        raise Fault('invalid_receiver', f'Allowed profile fields are {sorted(allowed)}', 422)
    name = body.get('name')
    if not isinstance(name, str) or not name or len(name) > 64:
        raise Fault('invalid_receiver', 'Device name is required and must stay within 64 characters', 422)
    relay = body.get('relay')
    if relay not in RELAY_KINDS:
        raise Fault('invalid_receiver', f'relay must be one of {list(RELAY_KINDS)}', 422)
    classes = body.get('storage_classes')
    if not isinstance(classes, list) or not classes or any(
            item not in STORAGE_CLASSES for item in classes) or len(set(classes)) != len(classes):
        raise Fault('invalid_receiver', f'storage_classes must be a non-empty subset of {list(STORAGE_CLASSES)}', 422)
    if relay == 'typed':
        defaults = dict(real_format='exact', null_sentinel='literal_null', blob_carrier='base64')
        required = set(STORAGE_CLASSES)
    else:
        defaults = dict(real_format='exact', null_sentinel='empty_string', blob_carrier='hex')
        required = {'text', 'null'}
    if not required <= set(classes):
        raise Fault('invalid_receiver', f'A {relay} relay must declare {sorted(required)} support', 422)
    real_format = body.get('real_format', defaults['real_format'])
    if real_format not in REAL_FORMATS:
        raise Fault('invalid_receiver', f'real_format must be one of {list(REAL_FORMATS)}', 422)
    null_sentinel = body.get('null_sentinel', defaults['null_sentinel'])
    if null_sentinel not in NULL_SENTINELS:
        raise Fault('invalid_receiver', f'null_sentinel must be one of {list(NULL_SENTINELS)}', 422)
    blob_carrier = body.get('blob_carrier', defaults['blob_carrier'])
    if blob_carrier not in BLOB_CARRIERS:
        raise Fault('invalid_receiver', f'blob_carrier must be one of {list(BLOB_CARRIERS)}', 422)
    if blob_carrier == 'none' and 'blob' in classes:
        raise Fault('invalid_receiver', 'A BLOB-capable device needs an actual blob_carrier', 422)
    notes = body.get('notes', '')
    if not isinstance(notes, str) or len(notes) > 512:
        raise Fault('invalid_receiver', 'notes must be text of at most 512 characters', 422)
    return dict(name=name, relay=relay, storage_classes=list(classes), real_format=real_format,
                null_sentinel=null_sentinel, blob_carrier=blob_carrier, notes=notes)


BUILTINS = (
    dict(name='typed-relay', relay='typed', storage_classes=list(STORAGE_CLASSES),
         real_format='exact', null_sentinel='literal_null', blob_carrier='base64',
         notes='五类 SQLite 存储值各自独立的 JSON 载体，实数用 hex 保位，BLOB 用 base64。'),
    dict(name='text-relay', relay='text', storage_classes=['text', 'null'],
         real_format='decimal15', null_sentinel='empty_string', blob_carrier='none',
         notes='只接收纯文本；数字文本按目标列亲和性重新解释，NULL 映射为空串，没有 BLOB 载体。'),
)


# --------------------------------------------------------------------------- #
# Relay serialization
# --------------------------------------------------------------------------- #

def render_real(value, real_format):
    if real_format == 'exact':
        return value.hex()
    return format(value, '.15g')


def serialize_cell(value, profile):
    """Return the wire cell plus the carrier choice for the audit trail."""
    if value is None:
        if 'null' not in profile['storage_classes']:
            raise Fault('receiver_capability', f"Device {profile['name']} cannot carry NULL")
        if profile['relay'] == 'typed':
            return None, 'null'
        if profile['null_sentinel'] == 'empty_string':
            return '', 'null_sentinel'
        return 'NULL', 'null_sentinel'
    kind = type(value)
    if profile['relay'] == 'typed':
        if kind is int and 'integer' in profile['storage_classes']:
            return value, 'integer'
        if kind is float and 'real' in profile['storage_classes']:
            return {'real': value.hex()}, 'real'
        if isinstance(value, bytes) and 'blob' in profile['storage_classes']:
            return {'blob': base64.b64encode(value).decode('ascii')}, 'blob'
        if kind is str and 'text' in profile['storage_classes']:
            return value, 'text'
        raise Fault('receiver_capability',
                    f"Device {profile['name']} has no carrier for {value.__class__.__name__} values")
    if kind is str:
        return value, 'text'
    if kind is int:
        return str(value), 'numeric_text'
    if kind is float:
        return render_real(value, profile['real_format']), 'numeric_text'
    if isinstance(value, bytes):
        if profile['blob_carrier'] == 'none':
            raise Fault('receiver_capability', f"Device {profile['name']} cannot carry BLOB values")
        if profile['blob_carrier'] == 'hex':
            return value.hex().upper(), 'blob_text'
        return base64.b64encode(value).decode('ascii'), 'blob_text'
    raise Fault('receiver_capability', f'Cannot relay value of type {value.__class__.__name__}')


def decode_typed_cell(cell):
    if cell is None or type(cell) in (int, str):
        return cell
    if isinstance(cell, dict) and len(cell) == 1:
        if 'blob' in cell:
            return base64.b64decode(cell['blob'], validate=True)
        if 'real' in cell:
            return float.fromhex(cell['real'])
    raise Fault('invalid_relay_payload', 'Typed relay payload is not a valid encoded SQLite value')


def bind_text_cell(cell, profile):
    """Decode a typed-relay cell; text-relay cells are bound as-is so the target
    column's declared SQLite affinity performs the re-interpretation."""
    if cell is None:
        return None
    if profile['relay'] == 'typed':
        return decode_typed_cell(cell)
    return cell


# --------------------------------------------------------------------------- #
# Payload construction and physical import
# --------------------------------------------------------------------------- #

def build_payload(sealed, profile):
    schema = sealed['schema']
    unsupported = []
    tables = {}
    for name, meta in schema['tables'].items():
        visible = [col for col in meta['columns'] if col['hidden'] == 0]
        key_columns = set(meta['pk'])
        for edge in schema['edges']:
            if edge['child'] == name:
                key_columns.update(edge['child_cols'])
        rows = []
        for ordinal, stored in enumerate(sealed['result']['rows'][name].values()):
            cells = []
            for index, col in enumerate(visible):
                value = decode(stored[index])
                try:
                    cell, carrier = serialize_cell(value, profile)
                except Fault:
                    cell, carrier = None, 'unsupported'
                    unsupported.append({
                        'table': name, 'row_index': ordinal, 'column': col['name'],
                        'storage_class': _storage(value), 'value_repr': repr(value),
                        'key_component': col['name'] in key_columns})
                cells.append(cell)
            rows.append(cells)
        tables[name] = rows
    payload = {'profile': profile, 'schema_digest': sealed['schema_digest'],
               'capture_digest': fingerprint(sealed), 'tables': tables,
               'unsupported_cells': unsupported}
    encoded = dumps(payload).encode('ascii')
    if len(encoded) > MAX_PAYLOAD_BYTES:
        raise Fault('payload_budget', 'Relay payload exceeds the 8 MiB trial budget')
    return payload, encoded


def _insert_sql(table, meta):
    visible = [col for col in meta['columns'] if col['hidden'] == 0]
    cols = ','.join(quote(col['name']) for col in visible)
    slots = ','.join('?' for _ in visible)
    return visible, f'INSERT INTO {quote(table)} ({cols}) VALUES ({slots})'


def _insert_rows(conn, table, meta, rows, profile):
    visible, sql = _insert_sql(table, meta)
    rejected = []
    accepted = 0
    for ordinal, row in enumerate(rows):
        params = [bind_text_cell(cell, profile) for cell in row]
        try:
            conn.execute('SAVEPOINT cell')
            conn.execute(sql, params)
            conn.execute('RELEASE cell')
            accepted += 1
        except sqlite3.Error as exc:
            conn.execute('ROLLBACK TO cell')
            rejected.append({'table': table, 'row_index': ordinal,
                            'primary_key_requested': [cell for cell in row[:len(meta['pk'])]],
                            'reason': str(exc)})
    return accepted, rejected


def _table_order(schema):
    """Parent-before-child ordering; cycles are broken by stable name order."""
    names = sorted(schema['tables'])
    dependencies = {name: set() for name in names}
    for edge in schema['edges']:
        if edge['parent'] != edge['child']:
            dependencies[edge['child']].add(edge['parent'])
    ordered, resolved = [], set()
    while names:
        progressed = False
        for name in list(names):
            if dependencies[name] <= resolved:
                ordered.append(name)
                resolved.add(name)
                names.remove(name)
                progressed = True
        if not progressed:
            ordered.extend(names)
            break
    return ordered


def _deferred_insert(conn, schema, tables, profile):
    """Insert every row under deferred FKs, then identify offenders row by row."""
    conn.execute('PRAGMA defer_foreign_keys=ON')
    rejected = []
    for name in _table_order(schema):
        meta = schema['tables'][name]
        visible, sql = _insert_sql(name, meta)
        for ordinal, row in enumerate(tables[name]):
            params = [bind_text_cell(cell, profile) for cell in row]
            try:
                conn.execute(sql, params)
            except sqlite3.Error as exc:
                rejected.append({'table': name, 'row_index': ordinal,
                                 'primary_key_requested': [cell for cell in row[:len(meta['pk'])]],
                                 'reason': str(exc)})
    return rejected


def import_payload(payload, sealed):
    schema = sealed['schema']
    profile = payload['profile']
    with TemporaryDirectory(prefix='keelsplice-trial-') as directory:
        file = Path(directory) / 'trial.sqlite'
        with closing(sqlite3.connect(file)) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute('PRAGMA trusted_schema=OFF')
            conn.execute('PRAGMA foreign_keys=ON')
            with conn:
                conn.execute('BEGIN')
                for table in schema['tables'].values():
                    conn.execute(table['ddl'])
                for index in schema['indexes']:
                    conn.execute(index['sql'])
                if profile['relay'] == 'typed':
                    # Exact round trip: deferred FKs let cyclic and out-of-order
                    # valid references load before the commit-time constraint check.
                    rejected = _deferred_insert(conn, schema, payload['tables'], profile)
                else:
                    rejected = []
                    for name in _table_order(schema):
                        _, failures = _insert_rows(conn, name, schema['tables'][name],
                                                   payload['tables'][name], profile)
                        rejected.extend(failures)
        return file.read_bytes(), rejected


# --------------------------------------------------------------------------- #
# Independent reopening verification
# --------------------------------------------------------------------------- #

def _native_identity(value):
    if isinstance(value, bytes):
        return ('blob', value)
    if type(value) is int:
        return ('integer', value)
    if type(value) is float:
        return ('real', value)
    if value is None:
        return ('null', None)
    return ('text', value)


def _check_primary_keys(conn, schema, sealed):
    findings = []
    for name, meta in schema['tables'].items():
        pk = meta['pk']
        # Group by the logical key value to expose storage-class collapses that
        # a token-only comparison would hide (e.g. integer 1 and text '1' both
        # becoming text '1' after a text-only relay).
        grouped = {}
        for row in conn.execute(f'SELECT * FROM {quote(name)}'):
            native = tuple(_native_identity(row[col]) for col in pk)
            grouped.setdefault(key(row_values(row, pk)), set()).add(native)
        expected = set(sealed['result']['rows'][name])
        reopened = set(grouped)
        lost = [loads_token(token) for token in sorted(expected - reopened)]
        collisions = [
            {'table': name, 'primary_key': loads_token(token),
             'storage_classes': [[item[0] for item in kinds] for kinds in grouped[token]]}
            for token in sorted(reopened) if len(grouped[token]) > 1
        ]
        if lost or collisions:
            findings.append({'table': name, 'lost_primary_keys': lost, 'collapsed_identities': collisions})
    return findings


def loads_token(token):
    from .codec import loads
    return loads(token)


def _check_relationships(conn, schema, sealed, profile):
    findings = []
    expected_rows = sealed['result']['rows']
    for edge in schema['edges']:
        child, parent = edge['child'], edge['parent']
        child_meta = schema['tables'][child]
        for token, stored in expected_rows[child].items():
            expected = decode_row(stored, child_meta)
            pk_condition = ' AND '.join(f'{quote(col)} IS ?' for col in child_meta['pk'])
            child_row = conn.execute(
                f'SELECT * FROM {quote(child)} WHERE {pk_condition}',
                [expected[col] for col in child_meta['pk']]).fetchone()
            if child_row is None:
                findings.append({'edge': [child, edge['id']], 'child_primary_key': loads_token(token),
                                 'effect': 'child_row_rejected',
                                 'detail': '子行未被接收库接受，关系无法建立'})
                continue
            expected_components = [expected[col] for col in edge['child_cols']]
            actual_components = [child_row[col] for col in edge['child_cols']]
            if any(component is None for component in expected_components):
                continue
            if any(component is None for component in actual_components):
                findings.append({'edge': [child, edge['id']], 'child_primary_key': loads_token(token),
                                 'effect': 'foreign_key_became_null',
                                 'detail': f'外键分量 {edge["child_cols"]} 被中继改写为 NULL，原父行不再可达'})
                continue
            if [_native_identity(value) for value in actual_components] != \
                    [_native_identity(value) for value in expected_components]:
                findings.append({'edge': [child, edge['id']], 'child_primary_key': loads_token(token),
                                 'effect': 'foreign_key_reinterpreted',
                                 'detail': f'外键分量 {edge["child_cols"]} 的存储类型被亲和性改写'})
            parent_condition = ' AND '.join(f'{quote(col)} IS ?' for col in edge['parent_cols'])
            matches = conn.execute(
                f'SELECT * FROM {quote(parent)} WHERE {parent_condition} LIMIT 2',
                actual_components).fetchall()
            if len(matches) != 1:
                findings.append({'edge': [child, edge['id']], 'child_primary_key': loads_token(token),
                                 'effect': 'parent_not_resolved',
                                 'detail': f'复合外键分量 {edge["child_cols"]} 重新打开后找不到唯一父行'})
    return findings


def decode_row(stored, meta):
    visible = [col for col in meta['columns'] if col['hidden'] == 0]
    return {col['name']: decode(value) for col, value in zip(visible, stored)}


def _declared_affinity(column):
    declared = (column['type'] or '').upper()
    if 'INT' in declared:
        return 'INTEGER'
    if any(token in declared for token in ('CHAR', 'CLOB', 'TEXT')):
        return 'TEXT'
    if 'BLOB' in declared or not declared:
        return 'NONE'
    if any(token in declared for token in ('REAL', 'FLOA', 'DOUB')):
        return 'REAL'
    return 'NUMERIC'


def _check_values(conn, schema, sealed, profile):
    (null_losses, negzero_losses, blob_losses, int64_losses,
     text_losses, real_losses, platform_normalization) = ([] for _ in range(7))
    for name, meta in schema['tables'].items():
        visible = [col for col in meta['columns'] if col['hidden'] == 0]
        expected_rows = sealed['result']['rows'][name]
        for token, stored in expected_rows.items():
            expected = decode_row(stored, meta)
            condition = ' AND '.join(f'{quote(col)} IS ?' for col in meta['pk'])
            row = conn.execute(
                f'SELECT * FROM {quote(name)} WHERE {condition}',
                [expected[col] for col in meta['pk']]).fetchone()
            if row is None:
                continue
            for col in visible:
                column, wanted = col['name'], expected[col['name']]
                actual = row[column]
                if wanted is None:
                    if actual is not None:
                        null_losses.append(_cell(name, token, column, wanted, actual, 'NULL 被改写为非空值'))
                    continue
                if type(wanted) is int:
                    if type(actual) is not int or actual != wanted:
                        int64_losses.append(_cell(name, token, column, wanted, actual,
                                                  'int64 身份或精度未保真'))
                    continue
                if type(wanted) is float:
                    if type(actual) is not float:
                        bucket = negzero_losses if math.copysign(1.0, wanted) < 0 else real_losses
                        bucket.append(_cell(name, token, column, wanted, actual,
                                            '实数退化为文本或其他类型，值身份未保真'))
                    elif actual != wanted:
                        real_losses.append(_cell(name, token, column, wanted, actual, '实数数值未保真'))
                    elif math.copysign(1.0, wanted) != math.copysign(1.0, actual):
                        cell = _cell(name, token, column, wanted, actual, '负零符号未保真')
                        if _declared_affinity(col) in ('REAL', 'NUMERIC'):
                            # SQLite normalizes -0.0 to +0.0 under REAL/NUMERIC
                            # affinity regardless of relay; report, do not blame.
                            cell['detail'] = 'REAL/NUMERIC 亲和列按 SQLite 规则归一负零，非中继失真'
                            platform_normalization.append(cell)
                        else:
                            negzero_losses.append(cell)
                    continue
                if isinstance(wanted, bytes):
                    if not isinstance(actual, bytes) or actual != wanted:
                        blob_losses.append(_cell(name, token, column, wanted, actual, 'BLOB 字节未保真'))
                    continue
                if type(wanted) is str and actual != wanted:
                    text_losses.append(_cell(name, token, column, wanted, actual, '文本内容未保真'))
    return dict(null=null_losses, negative_zero=negzero_losses, blob=blob_losses,
                int64=int64_losses, text=text_losses, real=real_losses,
                platform_normalization=platform_normalization)


def _cell(table, token, column, wanted, actual, detail):
    return {'table': table, 'primary_key': loads_token(token), 'column': column,
            'expected': repr(wanted), 'actual_repr': repr(actual),
            'actual_storage': _storage(actual), 'detail': detail}


def _storage(value):
    if value is None:
        return 'null'
    if type(value) is int:
        return 'integer'
    if type(value) is float:
        return 'real'
    if isinstance(value, bytes):
        return 'blob'
    return 'text'


def _impacted_relationships(value_findings, schema):
    # Platform normalization (REAL/NUMERIC negative-zero handling) is not a relay
    # defect and must not be reported as a damaged relationship.
    defect_groups = {key: items for key, items in value_findings.items()
                     if key not in ('relationships', 'platform_normalization')}
    affected_columns = {(item['table'], item['column']) for group in defect_groups.values() for item in group}
    edges = []
    for edge in schema['edges']:
        touched = [col for col in edge['child_cols'] if (edge['child'], col) in affected_columns]
        if touched:
            edges.append({'edge': [edge['child'], edge['id']], 'foreign_key_columns': touched,
                          'detail': '外键分量的表示改变，父行解析可能失效或被错误命中'})
    return edges


def verify(data, sealed, profile, rejected):
    schema = sealed['schema']
    with TemporaryDirectory(prefix='keelsplice-trial-read-') as directory:
        file = Path(directory) / 'trial.sqlite'
        file.write_bytes(data)
        with closing(sqlite3.connect(file.as_uri() + '?mode=ro', uri=True)) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute('PRAGMA trusted_schema=OFF')
            integrity = [row[0] for row in conn.execute('PRAGMA main.integrity_check')]
            reference_violations = [
                {'table': row[0], 'rowid': row[1], 'parent': row[2], 'foreign_key': row[3]}
                for row in conn.execute('PRAGMA main.foreign_key_check')]
            reopened = inspect(conn)
            schema_match = fingerprint(reopened) == sealed['schema_digest']
            pk_findings = _check_primary_keys(conn, schema, sealed)
            relationship_findings = _check_relationships(conn, schema, sealed, profile)
            value_findings = _check_values(conn, schema, sealed, profile)
            counts = {name: conn.execute(f'SELECT count(*) FROM {quote(name)}').fetchone()[0]
                      for name in schema['tables']}
    expected_counts = {name: len(rows) for name, rows in sealed['result']['rows'].items()}
    value_findings['relationships'] = _impacted_relationships(value_findings, schema)
    hard_failures = bool(rejected or reference_violations or pk_findings or relationship_findings
                         or value_findings['null'] or value_findings['int64'] or value_findings['blob'])
    soft_losses = any(value_findings[key] for key in ('negative_zero', 'text', 'real'))
    verdict = 'failed' if hard_failures else ('degraded' if soft_losses else 'passed')
    return {
        'verdict': verdict,
        'device': profile['name'],
        'relay': profile['relay'],
        'sha256': digest(data),
        'bytes': len(data),
        'integrity_ok': integrity == ['ok'],
        'schema_match': schema_match,
        'expected_counts': expected_counts,
        'reopened_counts': counts,
        'row_counts_match': expected_counts == counts,
        'rejected_rows': rejected,
        'foreign_key_violations': reference_violations,
        'primary_key_findings': pk_findings,
        'relationship_findings': relationship_findings,
        'value_findings': value_findings,
    }


def run_trial(sealed, profile):
    payload, encoded = build_payload(sealed, profile)
    data, rejected = import_payload(payload, sealed)
    report = verify(data, sealed, profile, rejected)
    report['payload_sha256'] = digest(encoded)
    report['unsupported_cells'] = payload['unsupported_cells']
    # A carrier gap on a primary-key or foreign-key component breaks identity or
    # relationship resolution and is a hard failure; gaps confined to ordinary
    # data columns degrade the delivery but leave the relational core intact.
    if any(cell['key_component'] for cell in payload['unsupported_cells']):
        report['verdict'] = 'failed'
    elif payload['unsupported_cells'] and report['verdict'] == 'passed':
        report['verdict'] = 'degraded'
    return report, data, payload, encoded

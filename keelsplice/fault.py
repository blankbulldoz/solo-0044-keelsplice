"""Chart conditional boundary rows of a sealed sample against a later read.

The operator picks one sealed capture, one table and one condition on a plain or
generated column.  A fault map reports the boundary rows that satisfy the
condition and, for every declared edge of that table, whether the neighbouring
row sits inside or outside the sealed sample and which way the edge points.

Two clocks are kept apart throughout:

* the **sealed time** comes only from the capture payload - the rows and edge
  witnesses that were frozen together;
* the **current read** comes from a separate read transaction against the source.

A neighbour seen only in the current read is labelled as an observation made
after the seal.  It is never called a row created after the seal, because an
uncaptured row cannot be dated by the capture.  The sealed sample is replayed in
a private in-memory database using the sealed DDL so affinity, collation and
generated expressions behave exactly as they did in the source.
"""

import sqlite3
from collections import defaultdict
from contextlib import closing
from . import capture
from .catalog import resolve
from .codec import Fault, decode, dumps, encode, key, quote, row_values
from .recipe import fields

MAX_BOUNDARY_ROWS = 200
MAX_NEIGHBOURS = 200
MAX_CYCLE_STEPS = 16

COMPARISONS = {'=': '=', '==': '=', '!=': '!=', '<>': '!=', '<': '<', '<=': '<=', '>': '>', '>=': '>='}
NULL_TESTS = {
    'is null': 'IS NULL', 'is_null': 'IS NULL', 'null': 'IS NULL',
    'is not null': 'IS NOT NULL', 'is_not_null': 'IS NOT NULL',
    'not_null': 'IS NOT NULL', 'not null': 'IS NOT NULL',
}
KINDS = {0: 'plain', 2: 'virtual', 3: 'stored'}
KIND_LABELS = {'plain': '普通列', 'virtual': '虚拟生成列', 'stored': '存储生成列'}


def kind(column):
    return KINDS.get(column['hidden'], 'plain')


def normalize(value):
    """Accept wire-encoded values and ordinary JSON operands for a condition.

    Keys must always use the canonical encoding documented for recipes, but a
    condition value is a comparison operand, so a JSON string, finite number or
    real hex literal is canonicalized here instead of rejected.
    """
    try:
        return encode(decode(value))
    except Fault:
        if type(value) in (str, int, float) and not (type(value) is float and value != value):
            return encode(value)
        if isinstance(value, dict) and len(value) == 1 and isinstance(value.get('real'), str):
            try:
                return encode(float.fromhex(value['real']))
            except (ValueError, OverflowError):
                pass
        raise


def visible(meta):
    """Columns returned by ``SELECT *``; only hidden==1 is suppressed there."""
    return [col for col in meta['columns'] if col['hidden'] != 1]


def condition(schema, body):
    """Validate an operator condition against the sealed schema."""
    fields(body, ('table', 'column', 'operator'), ('value',))
    table = resolve(schema['tables'], body['table'])
    columns = schema['tables'][table]['columns']
    column = resolve((col['name'] for col in columns), body['column'])
    operator = body['operator']
    if not isinstance(operator, str):
        raise Fault('invalid_condition', 'Condition operator must be text')
    spelling = ' '.join(operator.lower().split())
    if spelling in NULL_TESTS:
        if 'value' in body:
            raise Fault('invalid_condition', 'NULL tests do not take a value')
        value, given = None, False
        operator = NULL_TESTS[spelling]
    elif spelling in COMPARISONS:
        if 'value' not in body:
            raise Fault('invalid_condition', 'Comparison conditions need a value')
        value, given = normalize(body['value']), True
        operator = COMPARISONS[spelling]
    else:
        raise Fault('invalid_condition', 'Use =, ==, !=, <>, <, <=, >, >=, or IS [NOT] NULL')
    column_kind = kind(next(col for col in columns if col['name'] == column))
    return {'table': table, 'column': column, 'operator': operator, 'value': value,
            'value_given': given, 'column_kind': column_kind,
            'text': f'{quote(column)} {operator}' + (f' {dumps(value)}' if given else '')}


def select(conn, schema, the_condition, limit):
    """Rows satisfying the condition, ordered by typed primary key."""
    meta = schema['tables'][the_condition['table']]
    sql = (f'SELECT * FROM {quote(the_condition["table"])} '
           f'WHERE {quote(the_condition["column"])} {the_condition["operator"]}')
    params = []
    if the_condition['value_given']:
        sql += ' ?'
        params.append(decode(the_condition['value']))
    sql += ' ORDER BY ' + ','.join(quote(col) for col in meta['pk']) + f' LIMIT {limit + 1}'
    rows = conn.execute(sql, params).fetchall()
    return rows[:limit], len(rows) > limit


def replay(sealed):
    """Rebuild the sealed rows in memory by executing the sealed DDL verbatim.

    Foreign-key enforcement stays off and no constraints are added, so a
    condition can be evaluated over exactly the rows the capture selected.
    Generated columns are recomputed by SQLite from the captured plain columns,
    which is why the DDL is used instead of a hand-built table.
    """
    conn = sqlite3.connect(':memory:')
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA trusted_schema=OFF')
    try:
        for meta in sealed['schema']['tables'].values():
            conn.execute(meta['ddl'])
        for table, meta in sealed['schema']['tables'].items():
            offsets = [i for i, col in enumerate(meta['columns']) if col['hidden'] == 0]
            names = ','.join(quote(meta['columns'][i]['name']) for i in offsets)
            slots = ','.join('?' for _ in offsets)
            values = ([decode(row[i]) for i in offsets]
                      for row in sealed['result']['rows'][table].values())
            conn.executemany(f'INSERT INTO {quote(table)} ({names}) VALUES ({slots})', values)
        conn.commit()
    except sqlite3.Error as exc:
        conn.close()
        raise Fault('fault_replay', 'SQLite could not replay the sealed rows for this condition') from exc
    return conn


def identity(schema, table, row):
    return [encode(row[column]) for column in schema['tables'][table]['pk']]


def fetch(conn, schema, table, encoded):
    meta = schema['tables'][table]
    matches = capture.find(conn, table, meta['pk'], [decode(value) for value in encoded])
    return matches[0] if len(matches) == 1 else None


def holds(conn, schema, the_condition, encoded):
    """Re-test the condition for one typed key, using SQLite's own semantics."""
    table = the_condition['table']
    meta = schema['tables'][table]
    where = ' AND '.join(f'{quote(col)} = ?' for col in meta['pk'])
    sql = (f'SELECT 1 FROM {quote(table)} WHERE {where} '
           f'AND {quote(the_condition["column"])} {the_condition["operator"]}')
    params = [decode(value) for value in encoded]
    if the_condition['value_given']:
        sql += ' ?'
        params.append(decode(the_condition['value']))
    return conn.execute(sql, params).fetchone() is not None


def parent_rows(conn, edge, row):
    values = [row[column] for column in edge['child_cols']]
    if any(value is None for value in values):
        return [], 'null'
    matches = capture.find(conn, edge['parent'], edge['parent_cols'], values)
    if not matches:
        return [], 'missing'
    if len(matches) > 1:
        return matches, 'ambiguous'
    return matches, 'ok'


def child_rows(conn, schema, edge, row):
    rows = capture.children(conn, schema, edge, row).fetchmany(MAX_NEIGHBOURS + 1)
    return rows[:MAX_NEIGHBOURS], len(rows) > MAX_NEIGHBOURS


def cycle(conn, schema, edge, encoded, steps=MAX_CYCLE_STEPS):
    """True when repeatedly following this self-edge revisits a primary key.

    Only an edge whose child and parent are the same table can loop.  The walk
    uses that edge's own column mapping for a bounded number of steps so a long
    acyclic chain is not mistaken for a loop.
    """
    if edge['child'] != edge['parent']:
        return False
    table = edge['parent']
    seen = set()
    current = encoded
    for _ in range(steps):
        token = key(current)
        if token in seen:
            return True
        seen.add(token)
        row = fetch(conn, schema, table, current)
        if row is None:
            return False
        values = [row[column] for column in edge['child_cols']]
        if any(value is None for value in values):
            return False
        matches = capture.find(conn, table, edge['parent_cols'], values)
        if len(matches) != 1:
            return False
        current = identity(schema, table, matches[0])
    return False


def arc_index(sealed):
    index = defaultdict(list)
    for arc in sealed['result']['arcs']:
        index[(arc['edge'][0], arc['edge'][1], arc['direction'],
               arc['origin'][0], key(arc['origin'][1]))].append(arc['target'])
    return index


def add_row(entry, table, encoded, *, sample, before=False, now=False, looping=False):
    token = key(encoded)
    row = entry['rows'].get(token)
    if row is None:
        row = entry['rows'][token] = {'table': table, 'key': encoded, 'sample': sample,
                                      'sealed': False, 'current': False, 'cycle': looping}
    row['sealed'] = row['sealed'] or before
    row['current'] = row['current'] or now
    row['cycle'] = row['cycle'] or looping
    row['sample'] = 'inside' if row['sample'] in ('inside', True) else 'outside'
    return row


def map(sealed, the_condition, conn=None, source_schema=None, observed_at=None, reason=None):
    """Build the two-time fault map for one sealed capture and condition.

    ``conn`` is an optional read connection to the current source.  When it is
    absent the map reports the sealed time only and names why the later
    observation was skipped.
    """
    sealed_schema = sealed['schema']
    table = the_condition['table']
    sample = {name: set(rows) for name, rows in sealed['result']['rows'].items()}
    arcs = arc_index(sealed)
    followed = {(item['child'], item['id']) for item in sealed['recipe']['follow_children']}
    with closing(replay(sealed)) as held:
        sealed_rows, sealed_cut = select(held, sealed_schema, the_condition, MAX_BOUNDARY_ROWS)
        current_list, current_cut = [], False
        if conn is not None:
            current_list, current_cut = select(conn, source_schema, the_condition, MAX_BOUNDARY_ROWS)
        items = []
        seen = set()
        for before in sealed_rows:
            encoded = identity(sealed_schema, table, before)
            token = key(encoded)
            seen.add(token)
            now, matches = None, False
            if conn is not None:
                now = fetch(conn, source_schema, table, encoded)
                matches = now is not None and holds(conn, source_schema, the_condition, encoded)
            items.append(_row(sealed, the_condition, sealed_schema, source_schema, held, conn,
                              arcs, followed, sample, table, before, now, matches))
        for now in current_list:
            token = key(identity(sealed_schema, table, now))
            if token not in seen:
                seen.add(token)
                items.append(_row(sealed, the_condition, sealed_schema, source_schema, held, conn,
                                  arcs, followed, sample, table, None, now, True))
    summary = index(items)
    summary['truncated'] = summary['truncated'] or sealed_cut or current_cut
    observation = ({'state': 'current', 'at': observed_at, 'reason': None} if conn is not None
                   else {'state': 'unavailable', 'at': None, 'reason': reason})
    return {'sealed': {'schema_digest': sealed['schema_digest'], 'sealed_at': sealed.get('sealed_at')},
            'observation': observation, 'condition': the_condition,
            'boundary': {'sealed_truncated': sealed_cut, 'current_truncated': current_cut},
            'summary': summary, 'rows': items}


def index(items):
    summary = {'rows': len(items), 'inside': 0, 'outside': 0, 'changed': 0,
               'sealed_neighbours': 0, 'sealed_only': 0, 'current_only': 0,
               'null_references': 0, 'not_followed': 0, 'cycles': 0, 'truncated': False}
    for item in items:
        summary['inside' if item['sample'] == 'inside' else 'outside'] += 1
        summary['changed'] += bool(item['changed'])
        for entry in item['neighbours']:
            references = {side['reference'] for state in ('sealed', 'current')
                          for side in (entry[state],) if side is not None}
            summary['null_references'] += 'null' in references
            summary['not_followed'] += 'not-followed' in references
            summary['truncated'] = summary['truncated'] or (entry['current'] or {}).get('truncated', False)
            summary['cycles'] += any(row['cycle'] for row in entry['rows'])
            for row in entry['rows']:
                summary['sealed_neighbours'] += row['sealed']
                summary['sealed_only'] += row['sealed'] and not row['current']
                summary['current_only'] += row['current'] and not row['sealed']
    return summary


def _row(sealed, the_condition, sealed_schema, source_schema, held, conn, arcs, followed,
         sample, table, before, now, matches_now):
    encoded = identity(sealed_schema, table, before) if before is not None else \
        identity(sealed_schema, table, now)
    token = key(encoded)
    item = {'key': encoded, 'sample': 'inside' if token in sample[table] else 'outside',
            'sealed': None, 'current': None, 'changed': False, 'neighbours': []}
    if before is not None:
        item['sealed'] = {'column_value': encode(before[the_condition['column']])}
    if conn is not None:
        if now is None:
            item['current'] = {'present': False, 'matches_condition': False, 'column_value': None}
        else:
            item['current'] = {'present': True, 'matches_condition': matches_now,
                               'column_value': encode(now[the_condition['column']])}
            if before is not None:
                columns = [col['name'] for col in visible(sealed_schema['tables'][table])]
                item['changed'] = row_values(before, columns) != row_values(now, columns)
    for edge in sealed_schema['edges']:
        if table == edge['child']:
            direction = 'parent'
        elif table == edge['parent']:
            direction = 'child'
        else:
            continue
        targets = arcs.get((edge['child'], edge['id'], direction, table, token), [])
        entry = {'edge': [edge['child'], edge['id']], 'direction': direction,
                 'child_table': edge['child'], 'parent_table': edge['parent'],
                 'child_cols': edge['child_cols'], 'parent_cols': edge['parent_cols'],
                 'nullable': any(not col['notnull'] for col in sealed_schema['tables'][edge['child']]['columns']
                                 if col['name'] in edge['child_cols']),
                 'followed': (edge['child'], edge['id']) in followed if direction == 'child' else True,
                 'sealed': None, 'current': None, 'rows': {}}
        if before is not None:
            if direction == 'parent':
                reference = ('null' if any(before[column] is None for column in edge['child_cols'])
                             else ('ok' if targets else 'missing'))
            else:
                reference = 'ok' if (edge['child'], edge['id']) in followed else 'not-followed'
            entry['sealed'] = {'reference': reference, 'count': len(targets), 'truncated': False}
        if conn is not None:
            if now is None:
                entry['current'] = {'reference': 'absent-row', 'count': 0, 'truncated': False}
            elif direction == 'parent':
                parents, reference = parent_rows(conn, edge, now)
                entry['current'] = {'reference': reference, 'count': len(parents), 'truncated': False}
                for parent in parents:
                    neighbour = identity(source_schema, edge['parent'], parent)
                    add_row(entry, edge['parent'], neighbour, now=True,
                            sample=key(neighbour) in sample[edge['parent']],
                            looping=cycle(conn, source_schema, edge, neighbour))
            else:
                children, cut = child_rows(conn, source_schema, edge, now)
                entry['current'] = {'reference': 'ok', 'count': len(children), 'truncated': cut}
                for child in children:
                    neighbour = identity(source_schema, edge['child'], child)
                    add_row(entry, edge['child'], neighbour, now=True,
                            sample=key(neighbour) in sample[edge['child']],
                            looping=cycle(conn, source_schema, edge, neighbour))
        for target, key_values in targets:
            add_row(entry, target, key_values, before=True,
                    sample=key(key_values) in sample[target],
                    looping=cycle(held, sealed_schema, edge, key_values))
        entry['rows'] = sorted(entry['rows'].values(), key=lambda row: (row['table'], dumps(row['key'])))
        item['neighbours'].append(entry)
    return item

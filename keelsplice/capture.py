"""Build a closed row set and retain direct selection witnesses."""

import sqlite3
from collections import defaultdict, deque

from .catalog import resolve
from .codec import Fault, decode, dumps, key, loads, quote, row_values
from .recipe import MAX_ARCS, MAX_BYTES, identity

DEFAULT_PAGE = 50
MAX_PAGE = 200


def condition(columns, alias):
    return ' AND '.join(f'{alias}.{quote(column)} = ?' for column in columns)


def find(conn, table, columns, vals):
    return conn.execute(f'SELECT t.* FROM {quote(table)} t WHERE {condition(columns, "t")} LIMIT 2', vals).fetchall()


def children(conn, schema, edge, parent_row):
    parent = edge['parent']
    pk = schema['tables'][parent]['pk']
    # Unary plus removes child affinity so comparison follows the parent key.
    same = ' AND '.join(f'p.{quote(p)} = +c.{quote(c)}' for p, c in zip(edge['parent_cols'], edge['child_cols']))
    sql = (f'SELECT c.* FROM {quote(parent)} p JOIN {quote(edge["child"])} c ON {same} '
           f'WHERE {condition(pk, "p")}')
    return conn.execute(sql, [parent_row[col] for col in pk])


def parents(conn, edge, row):
    vals = [row[c] for c in edge['child_cols']]
    if any(val is None for val in vals):
        return ()
    matches = find(conn, edge['parent'], edge['parent_cols'], vals)
    if len(matches) != 1:
        raise Fault('invalid_reference', f'Expected exactly one parent for {edge["child"]} key {edge["id"]}')
    return matches


def collect(conn, schema, recipe):
    rows = {name: {} for name in schema['tables']}
    queue = deque()
    arcs = []
    root_rows = []
    size = len(dumps(schema))
    count = 0
    towards_parent = defaultdict(list)
    towards_child = defaultdict(list)
    selected = {(e['child'], e['id']) for e in recipe['follow_children']}
    for edge in schema['edges']:
        towards_parent[edge['child']].append(edge)
        if (edge['child'], edge['id']) in selected:
            towards_child[edge['parent']].append(edge)

    def account(value):
        nonlocal size
        size += len(dumps(value))
        if size > MAX_BYTES:
            raise Fault('payload_budget', 'Captured values and witnesses exceed the byte budget')

    def add(table, row):
        nonlocal count
        meta = schema['tables'][table]
        values = row_values(row, meta['pk'])
        if None in values:
            raise Fault('null_identity', f'Selected row in {table} has a NULL primary-key component')
        token = key(values)
        address = [table, values]
        if token not in rows[table]:
            if count >= recipe['row_limit']:
                raise Fault('row_budget', f'Relationship closure exceeds {recipe["row_limit"]} rows')
            stored = row_values(row, [col['name'] for col in meta['columns']])
            account([address, stored])
            rows[table][token] = stored
            count += 1
            queue.append((address, row))
        return address

    def witness(origin, dest, edge, direction):
        if len(arcs) >= MAX_ARCS:
            raise Fault('edge_budget', 'Relationship witnesses exceed the edge budget')
        arc = dict(origin=origin, target=dest, edge=[edge['child'], edge['id']], direction=direction)
        account(arc)
        arcs.append(arc)

    for root in recipe['roots']:
        table = root['table']
        matches = find(conn, table, schema['tables'][table]['pk'], [decode(v) for v in root['key']])
        if len(matches) != 1:
            raise Fault('missing_root', f'Expected one matching row in {table}')
        address = add(table, matches[0])
        account(address)
        root_rows.append(address)
    while queue:
        address, row = queue.popleft()
        table = address[0]
        for edge in towards_parent[table]:
            for parent in parents(conn, edge, row):
                witness(address, add(edge['parent'], parent), edge, 'parent')
        for edge in towards_child[table]:
            for child in children(conn, schema, edge, row):
                witness(address, add(edge['child'], child), edge, 'child')
    return {'rows': rows, 'arcs': sorted(arcs, key=dumps), 'roots': root_rows}


def summarize(result):
    counts = {table: len(rows) for table, rows in result['rows'].items()}
    return {'counts': counts, 'rows': sum(counts.values()), 'witnesses': len(result['arcs']),
            'root_occurrences': len(result['roots'])}


def trace(sealed, address):
    lookup = identity(sealed['schema'], address)
    target = [lookup['table'], lookup['key']]
    result = sealed['result']
    if key(target[1]) not in result['rows'][target[0]]:
        raise Fault('row_not_captured', 'Use the actual typed key stored in the capture', 404)
    return {'row': target, 'roots': [i for i, root in enumerate(result['roots']) if root == target],
            'causes': [arc for arc in result['arcs'] if arc['target'] == target],
            'dependencies': [arc for arc in result['arcs'] if arc['origin'] == target and arc['direction'] == 'parent']}


def differences(left, right, table):
    """Classify one table's rows by typed key, independent of JSON text order."""
    before = left['result']['rows'][table]
    after = right['result']['rows'][table]
    found = {'added': [], 'removed': [], 'modified': []}
    for token in after.keys() - before.keys():
        found['added'].append(token)
    for token in before.keys() - after.keys():
        found['removed'].append(token)
    for token in before.keys() & after.keys():
        if before[token] != after[token]:
            found['modified'].append(token)
    return found


def ordering(schema, table):
    """Give the sealed primary-key order so rows are never sorted as JSON text."""
    meta = schema['tables'][table]
    return meta.get('key_order') or [
        dict(column=column, collation='BINARY', descending=False) for column in meta['pk']]


def affinity(declared):
    """Map a declared SQLite type to its column affinity for the scratch table."""
    name = (declared or '').upper()
    if 'INT' in name:
        return 'INTEGER'
    if any(part in name for part in ('CHAR', 'CLOB', 'TEXT')):
        return 'TEXT'
    if 'BLOB' in name or not name:
        return 'BLOB'
    if any(part in name for part in ('REAL', 'FLOA', 'DOUB')):
        return 'REAL'
    return 'NUMERIC'


def order_clause(schema, table):
    return ', '.join(f'k{index} ' + ('DESC' if part['descending'] else 'ASC')
                     for index, part in enumerate(ordering(schema, table)))


def key_page(schema, table, tokens, *, boundary=None, inclusive=True, limit=None):
    """Return typed keys in the sealed primary-key order.

    ``boundary`` anchors the page at a real typed key: ``inclusive`` starts at
    that key, otherwise it resumes strictly after it.  The scratch table carries
    the sealed affinity, collation and direction, so numbers such as 10 and 2 and
    NOCASE-composed keys compare exactly as they do in the source database.
    """
    order = ordering(schema, table)
    types = {column['name']: column['type'] for column in schema['tables'][table]['columns']}
    columns = ', '.join('k{} {} COLLATE {}'.format(
        index, affinity(types[part['column']]), quote(part['collation']))
        for index, part in enumerate(order))
    ranking = order_clause(schema, table) + ', __seq'
    connection = sqlite3.connect(':memory:')
    try:
        connection.execute(f'CREATE TEMP TABLE entries (__seq INTEGER PRIMARY KEY, __token TEXT, {columns})')
        width = len(order)
        slots = ', '.join('?' for _ in range(2 + width))
        for seq, token in enumerate(tokens):
            connection.execute(f'INSERT INTO entries VALUES ({slots})',
                               (seq, token, *[decode(part) for part in loads(token)]))
        if boundary is not None:
            # The anchor sorts before equal keys for an inclusive start, after them to resume.
            anchor = -1 if inclusive else len(tokens)
            connection.execute(f'INSERT INTO entries VALUES ({slots})',
                               (anchor, None, *[decode(part) for part in boundary]))
        ranked = f'(SELECT __token, ROW_NUMBER() OVER (ORDER BY {ranking}) AS __rank FROM entries)'
        sql = f'SELECT __token FROM {ranked} WHERE __token IS NOT NULL'
        params = []
        if boundary is not None:
            sql += f' AND __rank > (SELECT __rank FROM {ranked} WHERE __token IS NULL)'
        sql += ' ORDER BY __rank'
        if limit is not None:
            sql += ' LIMIT ?'
            params.append(limit)
        return [row[0] for row in connection.execute(sql, params)]
    finally:
        connection.close()


def _stream(left, right, schema, changed, start, after, limit):
    found = {name: differences(left, right, name) for name in changed}
    kind_of = {name: {token: kind for kind, tokens in found[name].items() for token in tokens}
               for name in changed}
    ordered = {name: key_page(schema, name, list(kind_of[name])) for name in changed}
    summary = [{'table': name, 'pk': list(schema['tables'][name]['pk']),
                'order': ordering(schema, name),
                'counts': {kind: len(tokens) for kind, tokens in found[name].items()},
                'total': len(ordered[name])}
               for name in changed]
    cursor = start if start is not None else after
    lanes = changed
    if cursor is not None:
        anchored = resolve(schema['tables'], cursor['table'])
        lanes = [name for name in changed if name >= anchored]
    items, more = [], False
    for name in lanes:
        boundary = cursor['key'] if cursor is not None and name == cursor['table'] else None
        page = key_page(schema, name, ordered[name], boundary=boundary,
                        inclusive=start is not None, limit=limit - len(items) + 1)
        for token in page:
            if len(items) >= limit:
                more = True
                break
            kind = kind_of[name][token]
            items.append({'change': kind, 'table': name, 'key': loads(token),
                          'left': kind != 'added', 'right': kind != 'removed'})
        if more:
            break
    changes = {}
    for name in changed:
        changes[name] = {'added': [], 'removed': [], 'modified': []}
        for token in ordered[name]:
            changes[name][kind_of[name][token]].append(loads(token))
    resume = {'table': items[-1]['table'], 'key': items[-1]['key']} if more and items else None
    return {'tables': summary, 'changes': changes,
            'page': {'items': items, 'next': resume, 'done': not more, 'limit': limit}}


def compare(left, right, table=None, start=None, after=None, limit=DEFAULT_PAGE):
    """Diff two sealed captures as an ordered, resumable stream of typed keys."""
    if left['schema_digest'] != right['schema_digest']:
        raise Fault('schema_conflict', 'Captures have different source schemas', 409)
    schema = left['schema']
    if type(limit) is not int or not 1 <= limit <= MAX_PAGE:
        raise Fault('invalid_limit', f'Compare page limit must be 1 to {MAX_PAGE}')
    if start is not None and after is not None:
        raise Fault('invalid_cursor', 'Use either start or after, not both')
    selected = resolve(schema['tables'], table) if table is not None else None
    changed = [name for name in sorted(schema['tables'])
               if left['result']['rows'][name] != right['result']['rows'][name]]
    if selected is not None:
        if selected not in changed:
            raise Fault('unchanged_table', f'{selected} has identical rows in both captures', 409)
        changed = [selected]
    normalized = {}
    for value, label in ((start, 'start'), (after, 'after')):
        if value is None:
            continue
        cursor = identity(schema, value)
        normalized[label] = cursor
        if selected is not None and cursor['table'] != selected:
            raise Fault('invalid_cursor', 'The key boundary must belong to the selected table')
        token = key(cursor['key'])
        if token not in left['result']['rows'][cursor['table']] and \
                token not in right['result']['rows'][cursor['table']]:
            raise Fault('invalid_cursor', 'The key boundary must be a typed key captured on one side')
    stream = _stream(left, right, schema, changed, normalized.get('start'),
                     normalized.get('after'), limit)
    for name in schema['tables']:
        stream['changes'].setdefault(name, {'added': [], 'removed': [], 'modified': []})
    stream['changes'] = {name: stream['changes'][name] for name in sorted(stream['changes'])}
    return stream

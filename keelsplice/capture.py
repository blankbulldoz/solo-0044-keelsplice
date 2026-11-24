"""Build a closed row set and retain direct selection witnesses."""

from collections import defaultdict, deque

from .codec import Fault, decode, dumps, key, loads, quote, row_values
from .recipe import MAX_ARCS, MAX_BYTES, identity


def condition(columns, alias):
    return ' AND '.join(f'{alias}.{quote(column)} = ?' for column in columns)


def source(table, alias, hint=None):
    return f'{quote(table)} {alias}' if hint is None else f'{quote(table)} {alias} {hint}'


def find_sql(table, columns, hint=None):
    return f'SELECT t.* FROM {source(table, "t", hint)} WHERE {condition(columns, "t")} LIMIT 2'


def children_sql(parent, child, parent_cols, child_cols, pk, hint=None):
    # Unary plus removes child affinity so comparison follows the parent key.
    same = ' AND '.join(f'p.{quote(p)} = +c.{quote(c)}' for p, c in zip(parent_cols, child_cols))
    return (f'SELECT c.* FROM {quote(parent)} p JOIN {source(child, "c", hint)} ON {same} '
            f'WHERE {condition(pk, "p")}')


def find(conn, table, columns, vals):
    return conn.execute(find_sql(table, columns), vals).fetchall()


def children(conn, schema, edge, parent_row):
    parent = edge['parent']
    pk = schema['tables'][parent]['pk']
    sql = children_sql(parent, edge['child'], edge['parent_cols'], edge['child_cols'], pk)
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


def compare(left, right):
    if left['schema_digest'] != right['schema_digest']:
        raise Fault('schema_conflict', 'Captures have different source schemas', 409)
    changes = {}
    for table, before in left['result']['rows'].items():
        after = right['result']['rows'][table]
        changes[table] = {
            'added': [loads(token) for token in sorted(after.keys() - before.keys())],
            'removed': [loads(token) for token in sorted(before.keys() - after.keys())],
            'modified': [loads(token) for token in sorted(before.keys() & after.keys()) if before[token] != after[token]],
        }
    return changes

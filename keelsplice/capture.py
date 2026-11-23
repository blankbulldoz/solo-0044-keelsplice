"""Build a closed row set and retain direct selection witnesses."""

from collections import defaultdict, deque

from .codec import Fault, decode, dumps, key, loads, quote, row_values
from .recipe import MAX_ARCS, MAX_BYTES, identity


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


def identity_set(result):
    """Typed (table, key) identities selected in one trial, each counted once."""
    return {(table, tuple(loads(token))) for table, rows in result['rows'].items() for token in rows}


def row_identities(result):
    """Per-table ordered actual typed primary keys for operator cross-checking."""
    return {table: [loads(token) for token in sorted(rows)] for table, rows in result['rows'].items()}


def _address_token(address):
    return address[0], tuple(address[1])


def attribution(result, edge_labels):
    """Direct boundary reason for every selected row identity.

    Root occurrences, required-parent closure and reverse-child adoption are kept
    separately so a shared parent reached through several entries is still one row.
    Parent closure is mandatory for every variant and never carries a candidate
    group label; only reverse-child witnesses inherit the adopting edge's group.
    """
    reasons = defaultdict(lambda: {'roots': [], 'parent_edges': [], 'child_edges': []})
    for index, address in enumerate(result['roots']):
        reasons[_address_token(address)]['roots'].append(index)
    for arc in result['arcs']:
        bucket = reasons[_address_token(arc['target'])]
        edge = f'{arc["edge"][0]}#{arc["edge"][1]}'
        reason = {'edge': edge, 'from': list(arc['origin'])}
        if arc['direction'] == 'parent':
            bucket['parent_edges'].append(reason)
        else:
            reason['group'] = edge_labels.get(tuple(arc['edge']), 'none')
            bucket['child_edges'].append(reason)
    rows = []
    for (table, values), bucket in sorted(reasons.items(), key=lambda item: (item[0][0], dumps(list(item[0][1])))):
        rows.append({'row': [table, list(values)], 'root_occurrences': bucket['roots'],
                     'parent_reasons': sorted(bucket['parent_edges'], key=dumps),
                     'child_reasons': sorted(bucket['child_edges'], key=dumps)})
    return rows


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

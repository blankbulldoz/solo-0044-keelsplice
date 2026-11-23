"""Build a closed row set and retain direct selection witnesses."""

from collections import defaultdict, deque

from .codec import Fault, decode, dumps, fingerprint, key, loads, quote, row_values
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


def parent_identity(edge, pk, values):
    """Map a child's foreign-key tuple to the parent key, or None if unknown.

    Only a mapping that covers the whole parent key yields a row identity the
    capture could actually hold and link to.
    """
    slot = {column: index for index, column in enumerate(edge['parent_cols'])}
    if not set(pk) <= set(slot):
        return None
    parent_values = [values[slot[column]] for column in pk]
    return None if None in parent_values else parent_values


def differences(left, right):
    """List typed-key changes with content tokens and the side each row exists on.

    A row identity that stays put but whose declared foreign-key columns now
    match another parent is reported as ``retarget`` rather than a plain value
    edit, so callers can show the old and new relationship evidence apart.
    ``baseline_side``/``candidate_side`` say which capture actually holds the
    row, and each relation notes whether the old or new parent row was itself
    captured, so evidence links never point at a side that lacks the row.
    """
    if left['schema_digest'] != right['schema_digest']:
        raise Fault('schema_conflict', 'Captures have different source schemas', 409)
    schema = left['schema']
    items = []

    def add(kind, table, key, token_payload, *, before_side, after_side, relations=None, changed=None):
        # The token describes the row's actual difference only. Evidence
        # availability depends on which parent rows each capture happened to
        # hold, so it must not make an otherwise identical change look new.
        token = fingerprint({'kind': kind, 'table': table, 'key': key} | token_payload)
        item = {'kind': kind, 'table': table, 'key': key, 'token': token,
                'baseline_side': before_side, 'candidate_side': after_side, 'changed': changed or []}
        if relations:
            item['relations'] = relations
        items.append(item)

    for table, baseline_rows in left['result']['rows'].items():
        candidate_rows = right['result']['rows'][table]
        columns = [column['name'] for column in schema['tables'][table]['columns']]
        edges = [edge for edge in schema['edges'] if edge['child'] == table]
        for token in sorted(candidate_rows.keys() - baseline_rows.keys()):
            add('added', table, loads(token), {'values': candidate_rows[token]},
                before_side=False, after_side=True)
        for token in sorted(baseline_rows.keys() - candidate_rows.keys()):
            add('removed', table, loads(token), {'values': baseline_rows[token]},
                before_side=True, after_side=False)
        for token in sorted(baseline_rows.keys() & candidate_rows.keys()):
            before, after = baseline_rows[token], candidate_rows[token]
            if before == after:
                continue
            relations = []
            for edge in edges:
                positions = [columns.index(name) for name in edge['child_cols']]
                old = [before[position] for position in positions]
                new = [after[position] for position in positions]
                if old == new:
                    continue
                pk = schema['tables'][edge['parent']]['pk']
                old_parent = parent_identity(edge, pk, old)
                new_parent = parent_identity(edge, pk, new)
                before_rows = left['result']['rows'][edge['parent']]
                after_rows = right['result']['rows'][edge['parent']]
                relations.append({
                    'child': edge['child'], 'id': edge['id'], 'parent': edge['parent'],
                    'columns': list(edge['child_cols']), 'before': old, 'after': new,
                    'parent_before': old_parent, 'parent_after': new_parent,
                    'parent_baseline': old_parent is not None and key(old_parent) in before_rows,
                    'parent_candidate': new_parent is not None and key(new_parent) in after_rows,
                })
            changed = [columns[position] for position in range(len(columns)) if before[position] != after[position]]
            # Identity comes from the edge and the two value tuples only; the
            # parent-row availability flags stay out of the token so a repeat
            # with identical values still counts as the same difference.
            proof = [{'child': r['child'], 'id': r['id'], 'before': r['before'], 'after': r['after']}
                     for r in relations]
            add('retarget' if relations else 'modified', table, loads(token),
                {'before': before, 'after': after, 'relations': proof},
                before_side=True, after_side=True, relations=relations or None, changed=changed)
    return items

"""Batch triage of a recipe draft against one live catalog snapshot.

A registration attempt is all-or-nothing, but an operator pasting a large batch
of roots needs to see every problem at once. Each root and each chosen edge is
classified independently:

* ``keep`` — the input is valid against the actual schema and, for roots, the
  row exists in the current snapshot;
* ``correct`` — the input itself is wrong (shape, value type, missing row) and
  can be fixed without re-reading the catalog;
* ``defer`` — no judgment is possible yet because the referenced table,
  relationship or schema digest is not confirmed in the current catalog.

Triage never mutates repository state and never partially saves a recipe.
"""

from .catalog import resolve
from .codec import Fault, decode, encode, key, quote
from .recipe import MAX_ROWS

KEEP = 'keep'
CORRECT = 'correct'
DEFER = 'defer'
STATUSES = (KEEP, CORRECT, DEFER)


def top_shape(body):
    """Report top-level body problems without stopping per-entry triage."""
    problems = []
    if not isinstance(body, dict):
        return [{'code': 'invalid_request', 'message': 'Expected a JSON object'}]
    allowed = {'source', 'roots', 'follow_children', 'row_limit', 'name'}
    unknown = sorted(set(body) - allowed)
    if unknown:
        problems.append({'code': 'invalid_request', 'message': f'Unexpected fields: {", ".join(unknown)}'})
    roots = body.get('roots')
    if not isinstance(roots, list) or not 1 <= len(roots) <= 256:
        problems.append({'code': 'invalid_roots', 'message': 'Specify 1 to 256 root tuples'})
    edges = body.get('follow_children', [])
    if not isinstance(edges, list) or len(edges) > 256:
        problems.append({'code': 'invalid_edges', 'message': 'Specify a list of up to 256 child edges'})
    limit = body.get('row_limit')
    if limit is not None and (type(limit) is not int or not 1 <= limit <= MAX_ROWS):
        problems.append({'code': 'invalid_limit', 'message': f'row_limit must be 1 to {MAX_ROWS}'})
    return problems


def _entry(status, index, code, message, **extra):
    return {'index': index, 'status': status, 'code': code, 'message': message, **extra}


def triage_roots(conn, schema, roots):
    results = []
    if not isinstance(roots, list):
        return results
    for index, raw in enumerate(roots):
        if not isinstance(raw, dict) or set(raw) != {'table', 'key'}:
            results.append(_entry(CORRECT, index, 'invalid_request',
                                  'Each root needs exactly table and key', input=raw))
            continue
        raw_table, raw_key = raw['table'], raw['key']
        try:
            table = resolve(schema['tables'], raw_table)
        except Fault as exc:
            results.append(_entry(DEFER, index, exc.code, str(exc), table=raw_table, key=raw_key))
            continue
        pk = schema['tables'][table]['pk']
        if not isinstance(raw_key, list) or len(raw_key) != len(pk) or None in raw_key:
            results.append(_entry(CORRECT, index, 'invalid_key',
                                  f'{table} needs a complete non-NULL primary key tuple of {len(pk)} values',
                                  table=table, key=raw_key))
            continue
        try:
            values = [encode(decode(v)) for v in raw_key]
        except Fault as exc:
            results.append(_entry(CORRECT, index, exc.code, str(exc), table=table, key=raw_key))
            continue
        # Existence is judged in the same snapshot that produced the schema.
        condition = ' AND '.join(f't.{quote(column)} = ?' for column in pk)
        matches = conn.execute(
            f'SELECT t.* FROM {quote(table)} t WHERE {condition} LIMIT 2',
            [decode(v) for v in values]).fetchall()
        if len(matches) != 1:
            results.append(_entry(CORRECT, index, 'missing_root',
                                  f'No single matching row in {table}', table=table, key=values))
            continue
        actual = [encode(matches[0][column]) for column in pk]
        results.append(_entry(KEEP, index, 'ok', 'Root resolves to an existing row',
                              table=table, key=values, actual=[table, actual],
                              respelled=key(actual) != key(values)))
    return results


def triage_edges(schema, edges):
    results = []
    if not isinstance(edges, list):
        return results
    catalog_edges = {(edge['child'], edge['id']): edge for edge in schema['edges']}
    for index, raw in enumerate(edges):
        if not isinstance(raw, dict) or not {'child', 'id'} <= set(raw) <= {'child', 'id', 'parent'}:
            results.append(_entry(CORRECT, index, 'invalid_request',
                                  'Each edge needs child and id, with an optional parent expectation',
                                  input=raw))
            continue
        child, number = raw['child'], raw['id']
        if type(number) is not int:
            results.append(_entry(CORRECT, index, 'invalid_edge',
                                  'Edge id must be an integer foreign-key number', input=raw))
            continue
        try:
            table = resolve(schema['tables'], child)
        except Fault as exc:
            results.append(_entry(DEFER, index, exc.code, str(exc), child=child, id=number))
            continue
        current = catalog_edges.get((table, number))
        if current is None:
            available = sorted(edge['id'] for edge in schema['edges'] if edge['child'] == table)
            results.append(_entry(DEFER, index, 'invalid_edge',
                                  f'Foreign key {number} is not declared on {table}',
                                  child=table, id=number, available=available))
            continue
        expected = raw.get('parent')
        if expected is not None:
            try:
                expected_table = resolve(schema['tables'], expected)
            except Fault as exc:
                results.append(_entry(DEFER, index, exc.code, str(exc),
                                      child=table, id=number, parent=current['parent']))
                continue
            if expected_table != current['parent']:
                results.append(_entry(DEFER, index, 'edge_reassigned',
                                      f'Key {number} on {table} now targets {current["parent"]}, '
                                      f'not the previously selected {expected_table}',
                                      child=table, id=number, parent=current['parent'],
                                      expected_parent=expected_table))
                continue
        results.append(_entry(KEEP, index, 'ok', 'Edge is confirmed by the current catalog',
                              child=table, id=number, parent=current['parent'],
                              child_cols=current['child_cols'], parent_cols=current['parent_cols']))
    return results


def triage(conn, schema, body):
    """Return the full report; never raises for per-entry problems."""
    roots = body.get('roots') if isinstance(body, dict) else None
    edges = body.get('follow_children', []) if isinstance(body, dict) else []
    root_results = triage_roots(conn, schema, roots if isinstance(roots, list) else [])
    edge_results = triage_edges(schema, edges if isinstance(edges, list) else [])
    entries = root_results + edge_results
    report = {
        'statuses': {status: sum(1 for item in entries if item['status'] == status) for status in STATUSES},
        'problems': top_shape(body),
        'roots': root_results,
        'edges': edge_results,
    }
    report['ready'] = (not report['problems']
                       and all(item['status'] == KEEP for item in root_results)
                       and all(item['status'] == KEEP for item in edge_results))
    return report

"""Retain operator-chosen preview checks and recheck two stored observations.

An observation is a bounded trace of one preview read. It keeps evidence only
for the check items the operator selected before that read, never a complete
copy of the production values, and it is not a sealed capture: actual delivery
still requires a fresh capture followed by build and certification.
"""

from collections import Counter

from .catalog import resolve
from .codec import Fault, dumps, fingerprint, loads
from .recipe import fields

MAX_CHECKS = 64
MAX_BYTES = 2 * 1024 * 1024
SIMPLE = ('counts', 'roots')
KINDS = SIMPLE + ('generated', 'relation')
ORDER = {kind: rank for rank, kind in enumerate(KINDS)}


def descriptor(value):
    """Validate the shape of one check selection without consulting a schema."""
    if not isinstance(value, dict):
        raise Fault('invalid_check', 'Each check item must be a JSON object')
    kind = value.get('kind')
    if kind in SIMPLE:
        fields(value, ('kind',))
        return {'kind': kind}
    if kind == 'generated':
        fields(value, ('kind', 'table', 'column'))
        if not all(isinstance(value[name], str) for name in ('table', 'column')):
            raise Fault('invalid_check', 'A generated check needs table and column names')
        return {'kind': kind, 'table': value['table'], 'column': value['column']}
    if kind == 'relation':
        fields(value, ('kind', 'child', 'id'))
        if not isinstance(value['child'], str) or type(value['id']) is not int:
            raise Fault('invalid_check', 'A relation check needs a child table and foreign-key number')
        return {'kind': kind, 'child': value['child'], 'id': value['id']}
    raise Fault('invalid_check', f'Unknown check kind: {kind!r}')


def canonical(schema, value):
    """Resolve a check against the schema and return its normalized descriptor."""
    check = descriptor(value)
    try:
        if check['kind'] == 'generated':
            table = resolve(schema['tables'], check['table'])
            meta = schema['tables'][table]
            column = resolve((item['name'] for item in meta['columns']), check['column'])
            source = next(item for item in meta['columns'] if item['name'] == column)
            if source['hidden'] not in (2, 3):
                raise Fault('invalid_check', f'{column} is not a generated column in {table}')
            return {'kind': 'generated', 'table': table, 'column': column}
        if check['kind'] == 'relation':
            child = resolve(schema['tables'], check['child'])
            if not any(edge['child'] == child and edge['id'] == check['id'] for edge in schema['edges']):
                raise Fault('invalid_check', f'{child} has no foreign key {check["id"]}')
            return {'kind': 'relation', 'child': child, 'id': check['id']}
    except Fault as exc:
        raise Fault('invalid_check', str(exc)) from exc
    return check


def key(check):
    return dumps(check)


def order(check):
    return (ORDER[check['kind']], check.get('table') or check.get('child') or '',
            check.get('column') or check.get('id') or 0)


def selections(schema, requested):
    """Normalize and de-duplicate the check items selected before a read."""
    if requested is None:
        requested = [{'kind': kind} for kind in SIMPLE]
    if not isinstance(requested, list) or not requested:
        raise Fault('invalid_check', 'Select at least one check item')
    if len(requested) > MAX_CHECKS:
        raise Fault('invalid_check', f'Select at most {MAX_CHECKS} check items')
    chosen = {}
    for value in requested:
        check = canonical(schema, value)
        chosen[key(check)] = check
    return sorted(chosen.values(), key=order)


def available(schema):
    """Every check item the operator could have selected before the read."""
    items = [{'kind': 'counts'}, {'kind': 'roots'}]
    for name, meta in schema['tables'].items():
        for column in meta['columns']:
            if column['hidden'] in (2, 3):
                items.append({'kind': 'generated', 'table': name, 'column': column['name']})
    for edge in schema['edges']:
        items.append({'kind': 'relation', 'child': edge['child'], 'id': edge['id']})
    return items


def evidence(sealed, check):
    """Compute bounded evidence for one check without copying production rows."""
    schema, result = sealed['schema'], sealed['result']
    kind = check['kind']
    if kind == 'counts':
        counts = {table: len(rows) for table, rows in result['rows'].items()}
        return {'counts': counts, 'rows': sum(counts.values())}
    if kind == 'roots':
        return {'roots': result['roots']}
    if kind == 'generated':
        table = check['table']
        meta = schema['tables'][table]
        index = next(i for i, item in enumerate(meta['columns']) if item['name'] == check['column'])
        values = {token: fingerprint(row[index]) for token, row in result['rows'][table].items()}
        return {'table': table, 'column': check['column'], 'values': values}
    child, number = check['child'], check['id']
    links = {}
    for arc in result['arcs']:
        if arc['edge'] != [child, number]:
            continue
        if arc['direction'] == 'parent':
            links[dumps(arc['origin'][1])] = dumps(arc['target'])
        else:
            links[dumps(arc['target'][1])] = dumps(arc['origin'])
    for token in result['rows'][child]:
        links.setdefault(token, None)
    return {'child': child, 'id': number, 'links': links}


def trace(sealed, checks):
    """Build the storable observation record from a completed preview read."""
    record = {'schema_digest': sealed['schema_digest'], 'checks': checks,
              'evidence': {key(check): evidence(sealed, check) for check in checks}}
    if len(dumps(record).encode('ascii')) > MAX_BYTES:
        raise Fault('observation_budget', 'Retained observation evidence exceeds the byte budget')
    return record


def summarize(record):
    """Describe what an observation kept, without exposing row values."""
    kinds = Counter(check['kind'] for check in record['checks'])
    rows = 0
    for check in record['checks']:
        item = record['evidence'][key(check)]
        if check['kind'] == 'generated':
            rows += len(item['values'])
        elif check['kind'] == 'relation':
            rows += len(item['links'])
    return {'schema_digest': record['schema_digest'],
            'checks': record['checks'], 'count': len(record['checks']),
            'kinds': dict(kinds), 'rows': rows}


def compare_counts(before, after, check):
    left, right = before['counts'], after['counts']
    differences = [{'table': table, 'left': left.get(table), 'right': right.get(table)}
                   for table in sorted(set(left) | set(right)) if left.get(table) != right.get(table)]
    return ('different', {'differences': differences}) if differences else ('consistent', {})


def compare_roots(before, after, check):
    left, right = before['roots'], after['roots']
    differences = []
    for index in range(max(len(left), len(right))):
        first = left[index] if index < len(left) else None
        second = right[index] if index < len(right) else None
        if first != second:
            differences.append({'index': index, 'left': first, 'right': second})
    return ('different', {'differences': differences}) if differences else ('consistent', {})


def compare_generated(before, after, check):
    left, right = before['values'], after['values']
    differences = [{'row': loads(token), 'left': left[token], 'right': right[token]}
                   for token in sorted(left.keys() & right.keys()) if left[token] != right[token]]
    unknown = [{'row': loads(token), 'reason': 'unretained'}
               for token in sorted(left.keys() ^ right.keys())]
    if differences:
        return 'different', {'differences': differences, 'unknown': unknown}
    if unknown:
        return 'unknown', {'differences': [], 'unknown': unknown}
    return 'consistent', {}


def compare_relation(before, after, check):
    left, right = before['links'], after['links']
    differences, unknown = [], []
    for token in sorted(left.keys() & right.keys()):
        if left[token] != right[token]:
            differences.append({'row': loads(token),
                                'left': loads(left[token]) if left[token] is not None else None,
                                'right': loads(right[token]) if right[token] is not None else None})
    for token in sorted(left.keys() ^ right.keys()):
        unknown.append({'row': loads(token), 'reason': 'unretained'})
    if differences:
        return 'different', {'differences': differences, 'unknown': unknown}
    if unknown:
        return 'unknown', {'differences': [], 'unknown': unknown}
    return 'consistent', {}


COMPARE = {'counts': compare_counts, 'roots': compare_roots,
           'generated': compare_generated, 'relation': compare_relation}


def recheck(left, right):
    """Compare two observations only on the checks that both retained."""
    if left['schema_digest'] != right['schema_digest']:
        raise Fault('schema_conflict', 'Observations have different source schemas', 409)
    checks = {}
    for record in (left, right):
        for check in record['checks']:
            checks[key(check)] = check
    results = []
    for token in sorted(checks, key=lambda item: order(checks[item])):
        check = checks[token]
        before, after = left['evidence'].get(token), right['evidence'].get(token)
        if before is None or after is None:
            results.append(check | {'status': 'unknown', 'reason': 'unretained'})
            continue
        status, detail = COMPARE[check['kind']](before, after, check)
        results.append(check | {'status': status} | detail)
    tally = Counter(item['status'] for item in results)
    verdict = ('different' if tally['different'] else
               'unknown' if tally['unknown'] else 'consistent')
    return {'schema_digest': left['schema_digest'], 'verdict': verdict,
            'summary': {name: tally[name] for name in ('consistent', 'different', 'unknown')},
            'checks': results}

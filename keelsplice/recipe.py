"""Recipe shape, bounded input, and catalog-aware normalization."""

from .catalog import resolve
from .codec import Fault, decode, encode

DEFAULT_BUDGET = 1000
MAX_ROWS = 5000
MAX_BYTES = 8 * 1024 * 1024
MAX_ARCS = 50_000


def fields(value, required=(), optional=()):
    if not isinstance(value, dict) or not set(required) <= set(value) <= set(required) | set(optional):
        raise Fault('invalid_request', f'Expected fields {list(required)} and optional fields {list(optional)}', 400)
    return value


def identity(schema, value):
    fields(value, ('table', 'key'))
    table = resolve(schema['tables'], value['table'])
    vals = value['key']
    if not isinstance(vals, list) or len(vals) != len(schema['tables'][table]['pk']) or None in vals:
        raise Fault('invalid_key', f'{table} needs a complete non-NULL primary key tuple')
    return {'table': table, 'key': [encode(decode(v)) for v in vals]}


def validate(schema, body):
    fields(body, ('source', 'roots'), ('follow_children', 'row_limit'))
    roots = body['roots']
    if not isinstance(roots, list) or not 1 <= len(roots) <= 256:
        raise Fault('invalid_roots', 'Specify 1 to 256 root tuples')
    edges = body.get('follow_children', [])
    if not isinstance(edges, list) or len(edges) > 256:
        raise Fault('invalid_edges', 'Specify a list of up to 256 child edges')
    selected = set()
    for value in edges:
        fields(value, ('child', 'id'), ('parent',))
        child = resolve(schema['tables'], value['child'])
        number = value['id']
        if type(number) is not int:
            raise Fault('invalid_edge', f'Unknown key number in {child}')
        matching = [edge for edge in schema['edges'] if edge['child'] == child and edge['id'] == number]
        if not matching:
            raise Fault('invalid_edge', f'Unknown key number in {child}')
        edge = matching[0]
        if 'parent' in value:
            expected = resolve(schema['tables'], value['parent'])
            if expected != edge['parent']:
                raise Fault('edge_reassigned',
                            f'Key {number} on {child} now targets {edge["parent"]}, not {expected}')
        token = (child, number)
        selected.add(token)
    limit = body.get('row_limit', DEFAULT_BUDGET)
    if type(limit) is not int or not 1 <= limit <= MAX_ROWS:
        raise Fault('invalid_limit', f'row_limit must be 1 to {MAX_ROWS}')
    return dict(source=body['source'], roots=[identity(schema, root) for root in roots],
                follow_children=[dict(child=child, id=number) for child, number in sorted(selected)],
                row_limit=limit)


def describe(version):
    return {k: version[k] for k in ('name', 'version', 'active', 'schema_digest', 'recipe')}

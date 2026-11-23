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
    selected = {}
    for value in edges:
        fields(value, ('child', 'id'))
        child = resolve(schema['tables'], value['child'])
        number = value['id']
        edge = next((item for item in schema['edges'] if item['child'] == child and item['id'] == number),
                    None) if type(number) is int else None
        if edge is None:
            raise Fault('invalid_edge', f'Unknown key number in {child}')
        selected[(child, number)] = edge
    limit = body.get('row_limit', DEFAULT_BUDGET)
    if type(limit) is not int or not 1 <= limit <= MAX_ROWS:
        raise Fault('invalid_limit', f'row_limit must be 1 to {MAX_ROWS}')
    return dict(source=body['source'], roots=[identity(schema, root) for root in roots],
                follow_children=[dict(child=child, id=number) | mapping(edge)
                                 for (child, number), edge in sorted(selected.items())], row_limit=limit)


def mapping(edge):
    """The concrete column correspondence a selected edge stood for."""
    return dict(parent=edge['parent'], child_cols=list(edge['child_cols']),
                parent_cols=list(edge['parent_cols']))


def recorded(edge):
    child_cols, parent_cols = edge.get('child_cols'), edge.get('parent_cols')
    return (isinstance(edge.get('parent'), str) and isinstance(child_cols, list)
            and isinstance(parent_cols, list) and len(child_cols) == len(parent_cols)
            and all(isinstance(name, str) for name in child_cols + parent_cols))


def relations(recipe, schema=None, digest=None, binding=None, unavailable=None):
    """Explain each stored child edge, resolving only against the bound schema.

    A mapping saved with the version is an immutable registration fact. Older
    versions only recorded the child table and foreign-key number, so the
    mapping is derived only when the live catalog carries exactly the bound
    digest; otherwise the reason it cannot be confirmed is reported instead.
    """
    current = {}
    if schema is not None and digest is not None and digest == binding:
        current = {(edge['child'], edge['id']): edge for edge in schema['edges']}
    result = []
    for chosen in recipe['follow_children']:
        item = {'child': chosen['child'], 'id': chosen['id']}
        if recorded(chosen):
            result.append(item | mapping(chosen) | {'basis': 'registered'})
        elif unavailable is not None:
            result.append(item | {'basis': unavailable})
        elif schema is None:
            result.append(item | {'basis': 'catalog_unavailable'})
        elif digest != binding:
            result.append(item | {'basis': 'schema_drift'})
        elif (chosen['child'], chosen['id']) in current:
            result.append(item | mapping(current[(chosen['child'], chosen['id'])]) | {'basis': 'catalog'})
        else:
            result.append(item | {'basis': 'edge_missing'})
    return result


def describe(version):
    return {k: version[k] for k in ('name', 'version', 'active', 'schema_digest', 'recipe')}

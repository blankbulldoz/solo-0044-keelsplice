"""Freeze a branch segment of a sealed capture and seal the shared evidence."""

from .catalog import resolve
from .codec import Fault, dumps, fingerprint, key
from .recipe import fields, identity

DIRECTIONS = ('parents', 'children', 'both')
MAX_DEPTH = 12


def normalize(schema, value):
    """Validate a share selection against the schema of the frozen capture."""
    fields(value, ('anchor', 'direction'), ('edge', 'depth'))
    anchor = identity(schema, value['anchor'])
    direction = value['direction']
    if direction not in DIRECTIONS:
        raise Fault('invalid_direction', 'Choose parents, children, or both')
    edge = value.get('edge')
    if edge is not None:
        fields(edge, ('child', 'id'))
        child = resolve(schema['tables'], edge['child'])
        number = edge['id']
        if type(number) is not int or not any(e['child'] == child and e['id'] == number for e in schema['edges']):
            raise Fault('invalid_edge', f'Unknown key number in {child}')
        edge = {'child': child, 'id': number}
    depth = value.get('depth', 2)
    if type(depth) is not int or not 1 <= depth <= MAX_DEPTH:
        raise Fault('invalid_depth', f'Depth must be 1 to {MAX_DEPTH}')
    return {'anchor': anchor, 'direction': direction, 'edge': edge, 'depth': depth}


def address(value):
    return dumps([value[0], value[1]])


def freeze(sealed, selection):
    """Walk the sealed witnesses and retain only the selected branch range.

    The walk only reads the capture's stored arcs, so it needs no source
    connection.  Rows are visited once; arcs that lead back to an already
    visited row stay in the range as explicit overlap or cycle witnesses.
    """
    schema, result = sealed['schema'], sealed['result']
    anchor = selection['anchor']
    table = anchor['table']
    if key(anchor['key']) not in result['rows'].get(table, {}):
        raise Fault('row_not_captured', 'Choose an actual typed key stored in the capture', 404)
    outgoing = {}
    for arc in result['arcs']:
        outgoing.setdefault(address(arc['origin']), []).append(arc)
    wanted_edge = [selection['edge']['child'], selection['edge']['id']] if selection['edge'] else None

    def follows(arc):
        if selection['direction'] == 'parents' and arc['direction'] != 'parent':
            return False
        if selection['direction'] == 'children' and arc['direction'] != 'child':
            return False
        return wanted_edge is None or arc['edge'] == wanted_edge

    start = address([table, anchor['key']])
    depth_of = {start: 0}
    rows = [{'table': table, 'key': anchor['key'], 'depth': 0}]
    frontier = [(start, [table, anchor['key']])]
    arcs, seen = [], set()
    for level in range(1, selection['depth'] + 1):
        following = []
        for token, _ in frontier:
            for arc in outgoing.get(token, ()):
                if not follows(arc):
                    continue
                mark = dumps([arc['origin'], arc['target'], arc['edge'], arc['direction']])
                if mark in seen:
                    continue
                seen.add(mark)
                target = arc['target']
                target_token = address(target)
                revisit = target_token in depth_of
                arcs.append({'origin': arc['origin'], 'target': target, 'edge': arc['edge'],
                             'direction': arc['direction'], 'depth': level, 'revisit': revisit})
                if not revisit:
                    depth_of[target_token] = level
                    rows.append({'table': target[0], 'key': target[1], 'depth': level})
                    following.append((target_token, target))
        frontier = following
        if not frontier:
            break
    body = {'capture_digest': fingerprint(sealed), 'schema_digest': sealed['schema_digest'],
            'selection': selection, 'rows': rows, 'arcs': arcs}
    return body | {'seal': fingerprint(body)}


def summary(view):
    return {'rows': len(view['rows']), 'witnesses': len(view['arcs']),
            'revisits': sum(1 for arc in view['arcs'] if arc['revisit']),
            'depth': view['selection']['depth'], 'direction': view['selection']['direction'],
            'anchor': view['selection']['anchor'], 'edge': view['selection']['edge']}


def describe(record):
    view = record['view']
    return {'id': record['id'], 'capture_id': record['capture_id'],
            'capture_digest': view['capture_digest'], 'schema_digest': view['schema_digest'],
            'selection': view['selection'], 'summary': summary(view), 'seal': view['seal'],
            'revoked': record['revoked'], 'created_at': record['created_at']}


def open_view(sealed, view):
    """Re-read the sealed values named by a share and recheck both seals."""
    try:
        body = {name: view[name] for name in ('capture_digest', 'schema_digest', 'selection', 'rows', 'arcs')}
    except (KeyError, TypeError) as exc:
        raise Fault('share_integrity', 'Stored share content is malformed', 409) from exc
    if view['capture_digest'] != fingerprint(sealed) or view['schema_digest'] != sealed['schema_digest']:
        raise Fault('share_source_mismatch', 'The shared capture no longer matches this share', 409)
    if fingerprint(body) != view.get('seal'):
        raise Fault('share_integrity', 'Stored share content does not match its seal', 409)
    rows, columns = [], {}
    try:
        for item in view['rows']:
            meta = sealed['schema']['tables'][item['table']]
            stored = sealed['result']['rows'][item['table']].get(key(item['key']))
            if stored is None:
                raise KeyError(item['table'])
            rows.append(item | {'values': stored})
            columns[item['table']] = [column['name'] for column in meta['columns']]
    except (KeyError, TypeError) as exc:
        raise Fault('share_source_mismatch', 'A shared row is missing from the capture', 409) from exc
    return {'capture_digest': view['capture_digest'], 'schema_digest': view['schema_digest'],
            'selection': view['selection'], 'seal': view['seal'], 'summary': summary(view),
            'rows': rows, 'arcs': view['arcs'], 'columns': columns}

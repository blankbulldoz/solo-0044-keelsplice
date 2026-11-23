"""Compare allowed dependency paths between two separately maintained catalogs."""

from collections import defaultdict

from .catalog import resolve, sql_name
from .codec import Fault

DEFAULT_STEPS = 6
MAX_STEPS = 16
MAX_PATHS = 4096
MAX_LISTED = 32


def endpoints(start, end):
    if not isinstance(start, str) or not isinstance(end, str) or not start or not end:
        raise Fault('invalid_request', 'Supply start and end table names', 400)
    if sql_name(start) == sql_name(end):
        raise Fault('invalid_endpoints', 'Start and end tables must differ')
    return start, end


def step_limit(value):
    if type(value) is not int or not 1 <= value <= MAX_STEPS:
        raise Fault('invalid_steps', f'max_steps must be 1 to {MAX_STEPS}')
    return value


def arc(edge, direction):
    """One allowed edge taken in the parent (forward) or child (reverse) direction."""
    if direction == 'parent':
        return dict(edge=(edge['child'], edge['id']), direction=direction,
                    origin=edge['child'], target=edge['parent'],
                    origin_cols=tuple(edge['child_cols']), target_cols=tuple(edge['parent_cols']))
    return dict(edge=(edge['child'], edge['id']), direction=direction,
                origin=edge['parent'], target=edge['child'],
                origin_cols=tuple(edge['parent_cols']), target_cols=tuple(edge['child_cols']))


def adjacency(schema, allowed):
    links = defaultdict(list)
    for edge in schema['edges']:
        if (edge['child'], edge['id']) not in allowed:
            continue
        links[edge['child']].append(arc(edge, 'parent'))
        links[edge['parent']].append(arc(edge, 'child'))
    for arcs in links.values():
        arcs.sort(key=lambda step: (sql_name(step['target']), step['edge'], step['direction']))
    return links


def signature(path):
    """Normalized join conditions of a path, insensitive to spelling and walk direction."""
    return tuple((sql_name(step['origin']), sql_name(step['target']),
                  tuple(sql_name(col) for col in step['origin_cols']),
                  tuple(sql_name(col) for col in step['target_cols'])) for step in path)


def enumerate_paths(schema, allowed, start, end, max_steps):
    """Simple paths from start to end over allowed edges; self-loops never connect."""
    links = adjacency(schema, allowed)
    paths = []

    def visit(current, seen, acc):
        if len(paths) >= MAX_PATHS:
            raise Fault('path_budget', 'Allowed relations yield too many dependency paths')
        if current == end:
            paths.append(tuple(acc))
            return
        if len(acc) >= max_steps:
            return
        for step in links.get(current, ()):
            if step['target'] in seen:
                continue
            seen.add(step['target'])
            acc.append(step)
            visit(step['target'], seen, acc)
            acc.pop()
            seen.discard(step['target'])

    visit(start, {start}, [])
    return paths


def resolve_endpoints(schema, start, end):
    resolved = {}
    for role, value in (('start', start), ('end', end)):
        try:
            resolved[role] = resolve(schema['tables'], value)
        except Fault:
            resolved[role] = None
    return resolved


def render_step(step):
    child, number = step['edge']
    return {'edge': {'child': child, 'id': number}, 'direction': step['direction'],
            'origin': step['origin'], 'target': step['target'],
            'origin_cols': list(step['origin_cols']), 'target_cols': list(step['target_cols'])}


def render_signature(sig):
    return [{'origin': origin, 'target': target, 'origin_cols': list(origin_cols),
             'target_cols': list(target_cols)} for origin, target, origin_cols, target_cols in sig]


def compare(sides, start, end, max_steps):
    """Classify whether the two sides still share one unambiguous join path."""
    groups, reports = {}, {}
    for key in ('left', 'right'):
        side = sides[key]
        resolved = resolve_endpoints(side['schema'], start, end)
        if resolved['start'] and resolved['end']:
            paths = enumerate_paths(side['schema'], side['allowed'],
                                    resolved['start'], resolved['end'], max_steps)
        else:
            paths = []
        groups[key] = {}
        for path in sorted(paths, key=lambda p: (len(p), signature(p))):
            groups[key].setdefault(signature(path), path)
        reports[key] = {'source': side['source'], 'schema_digest': side['schema_digest'],
                        'allowed': len(side['allowed']), 'endpoints': resolved,
                        'paths': len(paths), 'signatures': len(groups[key]),
                        'sample': [render_signature(sig) for sig in list(groups[key])[:MAX_LISTED]]}
    shared = sorted(set(groups['left']) & set(groups['right']), key=lambda sig: (len(sig), sig))
    if not reports['left']['paths'] or not reports['right']['paths']:
        outcome = 'no_path'
    elif not shared:
        outcome = 'mapping_mismatch'
    elif len(shared) > 1:
        outcome = 'ambiguous'
    else:
        outcome = 'compatible'
    solutions = [{'signature': render_signature(sig),
                  'left': {'steps': [render_step(step) for step in groups['left'][sig]]},
                  'right': {'steps': [render_step(step) for step in groups['right'][sig]]}}
                 for sig in shared[:MAX_LISTED]]
    return {'outcome': outcome, 'start': start, 'end': end, 'max_steps': max_steps,
            'left': reports['left'], 'right': reports['right'],
            'shared_count': len(shared), 'shared': solutions}

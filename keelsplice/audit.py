"""Audit marks on sealed direct relationships and their carry-over rules."""

from .codec import Fault, dumps
from .recipe import fields

VERDICTS = ('verified', 'pending')
MAX_AUDITOR = 64
MAX_NOTE = 500


def arc_key(sealed, arc):
    """Semantic identity of a sealed arc: endpoints, direction, and key columns.

    The foreign-key number and the arc's position are deliberately excluded so
    that renumbering after an unrelated schema or closure change does not break
    carry-over, while changed endpoints, direction, or key columns do.
    """
    edges = {(edge['child'], edge['id']): edge for edge in sealed['schema']['edges']}
    edge = edges[(arc['edge'][0], arc['edge'][1])]
    return dumps({'origin': arc['origin'], 'target': arc['target'], 'direction': arc['direction'],
                  'child': edge['child'], 'parent': edge['parent'],
                  'child_cols': edge['child_cols'], 'parent_cols': edge['parent_cols']})


def arc_keys(sealed):
    return [arc_key(sealed, arc) for arc in sealed['result']['arcs']]


def address(value):
    if (not isinstance(value, list) or len(value) != 2 or not isinstance(value[0], str)
            or not isinstance(value[1], list)):
        raise Fault('invalid_arc', 'Relationship endpoints must be [table, key] pairs', 400)
    return value


def validate_arc(sealed, value):
    fields(value, ('origin', 'target', 'edge', 'direction'))
    arc = {'origin': address(value['origin']), 'target': address(value['target']),
           'edge': value['edge'], 'direction': value['direction']}
    edge = arc['edge']
    if (not isinstance(edge, list) or len(edge) != 2 or not isinstance(edge[0], str)
            or type(edge[1]) is not int):
        raise Fault('invalid_arc', 'Relationship edge must be [child table, key number]', 400)
    if arc['direction'] not in ('parent', 'child'):
        raise Fault('invalid_arc', 'Relationship direction must be parent or child', 400)
    token = dumps(arc)
    matches = [i for i, sealed_arc in enumerate(sealed['result']['arcs']) if dumps(sealed_arc) == token]
    if len(matches) != 1:
        raise Fault('unknown_arc', 'This direct relationship is not part of the sealed capture', 404)
    return sealed['result']['arcs'][matches[0]]


def judgment(body):
    fields(body, ('arc', 'verdict', 'auditor'), ('note',))
    if body['verdict'] not in VERDICTS:
        raise Fault('invalid_verdict', 'Verdict must be verified or pending', 400)
    auditor = body['auditor']
    if not isinstance(auditor, str) or not 1 <= len(auditor.strip()) <= MAX_AUDITOR:
        raise Fault('invalid_auditor', 'Auditor name must be 1 to 64 characters', 400)
    note = body.get('note', '')
    if not isinstance(note, str) or len(note) > MAX_NOTE:
        raise Fault('invalid_note', f'Note must be at most {MAX_NOTE} characters', 400)
    return body['verdict'], auditor.strip(), note


def public(mark):
    return {key: mark[key] for key in ('id', 'capture', 'arc', 'verdict', 'auditor', 'note')}


def review(capture_id, sealed, marks, counts):
    """Resolve each sealed arc against the append-only marks of the same recipe.

    A judgement made on this capture applies to its exact arc. A judgement from
    another capture carries over only when its relationship key is unique in
    both captures; otherwise the arc waits for human review. Marks never move
    and are never overwritten: history always cites the original capture.
    """
    entries = []
    for arc, rel_key in zip(sealed['result']['arcs'], arc_keys(sealed)):
        history = [mark for mark in marks if mark['rel_key'] == rel_key]
        exact = [mark for mark in history if mark['capture'] == capture_id and mark['arc'] == arc]
        if exact:
            mark = exact[-1]
            entries.append({'arc': arc, 'state': mark['verdict'], 'mark': public(mark),
                            'carried': False, 'history': [public(item) for item in history]})
            continue
        candidates = [mark for mark in history if mark['capture'] != capture_id]
        if not candidates:
            entries.append({'arc': arc, 'state': 'unmarked', 'mark': None,
                            'carried': False, 'history': []})
            continue
        mark = candidates[-1]
        unique = counts[capture_id][rel_key] == 1 and counts[mark['capture']][rel_key] == 1
        entries.append({'arc': arc, 'state': mark['verdict'] if unique else 'review',
                        'mark': public(mark), 'carried': True,
                        'history': [public(item) for item in history]})
    inactive = [public(mark) for mark in marks
                if mark['capture'] != capture_id and mark['rel_key'] not in counts[capture_id]]
    return {'arcs': entries, 'inactive': inactive}

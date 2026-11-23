"""Per-tab traversal routes over the sealed direct-relationship witnesses.

A route is a self-contained, stateless description of one operator's walk
through a capture.  Every hop records the directed edge that was chosen and the
canonical stored key it led to.  The browser round-trips the encoded token in a
form field, so two tabs examining the same capture keep independent routes and
can never overwrite one another.

Loop detection is scoped to a single route: the same actual key may be a first
arrival on one branch and a return to an earlier node (a loop) on another.
"""

from .codec import Fault, decode as decode_value, dumps, key, loads
from .recipe import identity

MAX_HOPS = 64


def encode(route):
    """Serialize a route into the single-line token carried by browser forms."""
    return dumps(route)


def decode(token):
    if isinstance(token, list):
        route = token
    elif isinstance(token, str) and token and len(token) <= 16 * 1024:
        try:
            route = loads(token)
        except (ValueError, RecursionError) as exc:
            raise Fault('invalid_route', '路线数据无法识别或过长。', 400) from exc
    else:
        route = None
    if not isinstance(route, list) or len(route) > MAX_HOPS:
        raise Fault('invalid_route', '路线数据无法识别或过长。', 400)
    if not all(isinstance(hop, dict) and set(hop) == {'edge', 'direction', 'table', 'key'}
               for hop in route):
        raise Fault('invalid_route', '路线数据无法识别或过长。', 400)
    if not all(type(hop['direction']) is str and hop['direction'] in ('parent', 'child')
               and isinstance(hop['edge'], list) and len(hop['edge']) == 2
               for hop in route):
        raise Fault('invalid_route', '路线数据无法识别或过长。', 400)
    return route


def _arc_index(arcs):
    index = {}
    for arc in arcs:
        signature = (arc['origin'][0], key(arc['origin'][1]), tuple(arc['edge']), arc['direction'])
        index.setdefault(signature, arc)
    return index


def _node_label(address):
    return [address[0], address[1]]


def trace(sealed, body):
    """Validate the submitted route against sealed witnesses and annotate the view.

    Returns the ordinary trace evidence plus the canonical route, the current
    node's path position, the edge selected to reach it, and the index of the
    earlier occurrence when this branch closes a loop.  Everything is derived
    from the sealed capture, never from mutable server-side state.
    """
    from .capture import trace as trace_evidence

    token = body.get('route', '')
    route = decode(token) if token else []
    start = identity(sealed['schema'], dict(table=body['table'], key=body['key']))
    current = [start['table'], start['key']]
    if key(current[1]) not in sealed['result']['rows'][current[0]]:
        raise Fault('row_not_captured', 'Use the actual typed key stored in the capture', 404)

    arcs = _arc_index(sealed['result']['arcs'])
    path = [current]
    for hop in route:
        arc = arcs.get((current[0], key(current[1]), tuple(hop['edge']), hop['direction']))
        if arc is None:
            raise Fault('invalid_route', '路线中的边不属于当前捕获，请从证据页重新选择。', 400)
        expected = arc['target']
        try:
            hop_key = [decode_value(value) for value in hop['key']]
        except Fault:
            raise Fault('invalid_route', '路线数据无法识别或过长。', 400) from None
        if hop['table'] != expected[0] or key(hop_key) != key(expected[1]):
            raise Fault('invalid_route', '路线中的边不属于当前捕获，请从证据页重新选择。', 400)
        current = [expected[0], expected[1]]
        path.append(current)

    evidence = trace_evidence(sealed, dict(table=current[0], key=current[1]))
    positions = {}
    for index, address in enumerate(path):
        positions.setdefault((address[0], key(address[1])), index)
    loop_at = positions.get((current[0], key(current[1])))
    return evidence | {
        'route': route,
        'path': [_node_label(address) for address in path],
        'position': len(path) - 1,
        'selected_edge': route[-1]['edge'] if route else None,
        'selected_direction': route[-1]['direction'] if route else None,
        'loop_at': loop_at if loop_at != len(path) - 1 else None,
    }


def extend(route, arc):
    """Return a new route token that follows one directed witness edge."""
    if len(route) >= MAX_HOPS:
        raise Fault('invalid_route', '路线长度已达上限，请先回退到较早节点。', 400)
    hop = dict(edge=list(arc['edge']), direction=arc['direction'],
               table=arc['target'][0], key=arc['target'][1])
    return encode(route + [hop])


def backtrack(route, position):
    """Return the route prefix ending at the given zero-based path position."""
    if type(position) is not int or not 0 <= position <= len(route):
        raise Fault('invalid_route', '回退位置不在当前路线内。', 400)
    return encode(route[:position])

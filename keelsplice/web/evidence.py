"""Render stored edge witnesses with per-tab, branch-preserving traversal routes."""

from .forms import field, typed_json
from .render import code, page, segment, text, typed
from .. import route as routes
from ..codec import Fault, dumps, key


def hidden(name, value):
    return f'<input type="hidden" name="{text(name)}" value="{text(value)}">'


def edge_rows(base, edges, role, token, origin, selected_edge, selected_direction):
    """One row per direct witness.

    「查看此行」starts a fresh walk at the neighbour, while 「沿此边继续」extends
    the current tab's route, recording the chosen directed edge.  The edge used
    to arrive at the current node is marked so a branch can be restored later.
    """
    opposite = 'origin' if role == 'cause' else 'target'
    rows = []
    for edge in edges:
        table, value = edge[opposite]
        label = '必需父关系' if edge['direction'] == 'parent' else '配方反向纳入'
        chosen = edge['edge'] == selected_edge and edge['direction'] == selected_direction
        marker = ' <span class="status ready">本路线所选</span>' if chosen else ''
        fresh = (f'<form method="post" action="{base}/trace">'
                 f'{hidden("table", table)}{hidden("key", dumps(value))}'
                 '<button class="quiet">查看此行</button></form>')
        edge_table, edge_id = edge['edge']
        walk = (f'<form method="post" action="{base}/trace">'
                f'{hidden("table", origin[0])}{hidden("key", dumps(origin[1]))}'
                f'{hidden("route", token)}{hidden("edge_table", edge_table)}'
                f'{hidden("edge_id", str(edge_id))}{hidden("direction", edge["direction"])}'
                '<button class="quiet">沿此边继续</button></form>')
        highlight = ' class="chosen"' if chosen else ''
        rows.append(
            f'<tr{highlight}><td>{label}{marker}</td><td>{code(edge["edge"][0])} #{edge["edge"][1]}</td>'
            f'<td>{code(table)} {typed(value)}</td><td><div class="inline">{fresh}{walk}</div></td></tr>'
        )
    return ''.join(rows) or '<tr><td colspan="4" class="empty">无直接记录。</td></tr>'


def _find_arc(view, edge_table, edge_id, direction):
    if direction not in ('parent', 'child') or not edge_id.isascii() or not edge_id.isdecimal():
        raise Fault('invalid_route', '所选边无法识别，请从证据页重新选择。', 400)
    identity = [edge_table, int(edge_id)]
    origin = (view['row'][0], key(view['row'][1]))
    for arc in (*view['causes'], *view['dependencies'], *view['children']):
        if (arc['edge'] == identity and arc['direction'] == direction
                and (arc['origin'][0], key(arc['origin'][1])) == origin):
            return arc
    raise Fault('invalid_route', '所选边不属于当前节点，请从证据页重新选择。', 400)


def path_nav(base, view, token):
    """Clickable path positions; backtracking restores the route prefix."""
    path = view['path']
    first_seen, repeated = {}, set()
    for index, address in enumerate(path):
        label = (address[0], key(address[1]))
        if label in first_seen:
            repeated.update((first_seen[label], index))
        else:
            first_seen[label] = index
    items = []
    for index, address in enumerate(path):
        caption = f'{index + 1}. {code(address[0])} {typed(address[1])}'
        loop = ' <span class="status pending">回环</span>' if index in repeated else ''
        if index == view['position']:
            items.append(f'<li aria-current="step"><span>{caption}（当前）{loop}</span></li>')
            continue
        back = (f'<form method="post" action="{base}/trace" class="inline">'
                f'{hidden("table", path[0][0])}{hidden("key", dumps(path[0][1]))}'
                f'{hidden("route", token)}{hidden("back", str(index))}'
                f'<button class="quiet">{caption}{loop}</button></form>')
        items.append(f'<li>{back}</li>')
    return '<ol class="data-list route-path">' + ''.join(items) + '</ol>'


def show(bench, capture_id, values):
    base = f'/workbench/captures/{segment(capture_id)}'
    start = dict(table=field(values, 'table'), key=typed_json(field(values, 'key')))
    token = field(values, 'route', '')
    edge_id = field(values, 'edge_id', '')
    back = field(values, 'back', '')

    if edge_id:
        view = bench.trace_route(capture_id, start | {'route': token})
        token = routes.extend(view['route'], _find_arc(
            view, field(values, 'edge_table'), edge_id, field(values, 'direction')))
        view = bench.trace_route(capture_id, dict(table=view['path'][0][0],
                                                  key=view['path'][0][1], route=token))
    elif back != '':
        view = bench.trace_route(capture_id, start | {'route': token})
        if not back.isascii() or not back.isdecimal():
            raise Fault('invalid_route', '回退位置不在当前路线内。', 400)
        origin = view['path'][0]
        token = routes.backtrack(view['route'], int(back))
        view = bench.trace_route(capture_id, dict(table=origin[0], key=origin[1], route=token))
    else:
        view = bench.trace_route(capture_id, start | {'route': token})

    token = routes.encode(view['route'])
    origin = view['path'][0]
    occurrences = ', '.join(str(index + 1) for index in view['roots'])
    loop_notice = (
        f'<p role="status" class="notice info">回环判定：当前实存键已在本路线第 '
        f'{view["loop_at"] + 1} 个位置首次到达，此处是沿当前分支回到已见节点。</p>'
        if view['loop_at'] is not None else
        '<p class="notice info">回环判定：当前节点是本分支首次到达。</p>'
    )
    reset = (f'<form method="post" action="{base}/trace" class="inline">'
             f'{hidden("table", view["row"][0])}{hidden("key", dumps(view["row"][1]))}'
             '<button class="quiet">从此节点另开新路线</button></form>')
    return 200, page('入选证据', 'capture', 'evidence', base=base, table=code(view['row'][0]),
                     key=typed(view['row'][1]), occurrences=occurrences or '无',
                     position=f'{view["position"] + 1} / {len(view["path"])}',
                     route_path=path_nav(base, view, token), loop_notice=loop_notice,
                     reset=reset,
                     causes=edge_rows(base, view['causes'], 'cause', token, origin,
                                      view['selected_edge'], view['selected_direction']),
                     dependencies=edge_rows(base, view['dependencies'], 'parent', token, origin,
                                            view['selected_edge'], view['selected_direction']),
                     children=edge_rows(base, view['children'], 'child', token, origin,
                                        view['selected_edge'], view['selected_direction']))

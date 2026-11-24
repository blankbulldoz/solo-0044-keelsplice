"""Render stored edge witnesses and navigate with the exact typed key."""

from . import identity
from .forms import field, typed_json
from .render import page, segment

LABELS = {'parent': '必需父关系', 'child': '配方反向纳入'}


def edge_rows(base, edges, role):
    rows = []
    for edge in edges:
        table, key = edge['origin' if role == 'cause' else 'target']
        rows.append(
            f'<tr><td>{LABELS[edge["direction"]]}</td>'
            f'<td>{identity.edge(edge["edge"][0], edge["edge"][1], edge["direction"])}</td>'
            f'<td>{identity.identifier(table)} {identity.pair(key)}</td>'
            f'<td><form method="post" action="{base}/trace">{identity.fields(table, key)}'
            '<button class="quiet">查看此行</button></form></td></tr>'
        )
    return ''.join(rows) or '<tr><td colspan="4" class="empty">无直接记录。</td></tr>'


def show(bench, capture_id, values):
    result = bench.trace(capture_id, dict(table=field(values, 'table'),
                                          key=typed_json(field(values, 'key'))))
    table, key = result['row']
    base = f'/workbench/captures/{segment(capture_id)}'
    return 200, page('入选证据', 'capture', 'evidence', base=base, legend=identity.legend(),
                     table=identity.identifier(table), key=identity.pair(key),
                     occurrences=identity.root_positions(result['roots']),
                     causes=edge_rows(base, result['causes'], 'cause'),
                     dependencies=edge_rows(base, result['dependencies'], 'parent'))

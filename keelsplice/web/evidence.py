"""Render stored edge witnesses and provide navigation by the exact typed key."""

from .forms import field, typed_json
from .render import code, page, segment, text, typed
from ..codec import dumps


def edge_rows(base, edges, role):
    rows = []
    opposite = 'origin' if role == 'cause' else 'target'
    for edge in edges:
        table, key = edge[opposite]
        label = '必需父关系' if edge['direction'] == 'parent' else '配方反向纳入'
        rows.append(
            f'<tr><td>{label}</td><td>{code(edge["edge"][0])} #{edge["edge"][1]}</td>'
            f'<td>{code(table)} {typed(key)}</td><td><form method="post" action="{base}/trace">'
            f'<input type="hidden" name="table" value="{text(table)}">'
            f'<input type="hidden" name="key" value="{text(dumps(key))}">'
            '<button class="quiet">查看此行</button></form></td></tr>'
        )
    return ''.join(rows) or '<tr><td colspan="4" class="empty">无直接记录。</td></tr>'


def show(bench, capture_id, values):
    result = bench.trace(capture_id, dict(table=field(values, 'table'), key=typed_json(field(values, 'key'))))
    table, key = result['row']
    base = f'/workbench/captures/{segment(capture_id)}'
    occurrences = ', '.join(str(index + 1) for index in result['roots'])
    return 200, page('入选证据', 'capture', 'evidence', base=base, table=code(table),
                     key=typed(key), occurrences=occurrences or '无',
                     causes=edge_rows(base, result['causes'], 'cause'),
                     dependencies=edge_rows(base, result['dependencies'], 'parent'))

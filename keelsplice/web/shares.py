"""Create, open and withdraw frozen branch shares without touching the source."""

from .errors import message
from .forms import field, typed_json
from .render import code, page, segment, text, typed
from ..codec import Fault, dumps
from ..share import MAX_DEPTH

DIRECTIONS = (('both', '双向（父行与子行）'), ('parents', '仅向必需父行'), ('children', '仅向已选子行'))


def link(token):
    return f'/workbench/shares/{segment(token)}'


def summary_line(record):
    selection = record['selection']
    anchor = selection['anchor']
    edge = selection['edge']
    chosen = f'{edge["child"]} · 外键 {edge["id"]}' if edge else '全部关系'
    direction = {'both': '双向', 'parents': '仅父行', 'children': '仅子行'}[selection['direction']]
    return (f'{code(anchor["table"])} {typed(anchor["key"])} · {chosen} · '
            f'{direction} · 深度 {selection["depth"]}')


def share_rows(shares, base):
    rows = []
    for record in shares:
        label = code(record['id'][:12])
        if record['revoked']:
            state = '<span class="status muted">已撤回</span>'
            title, action = label, ''
        else:
            state = '<span class="status ready">可查看</span>'
            title = f'<a href="{link(record["id"])}">{label}</a>'
            action = (f'<form method="post" action="{link(record["id"])}/withdraw">'
                      '<button class="quiet">撤回这份分享</button></form>')
        rows.append(
            f'<tr><th scope="row">{title}</th>'
            f'<td>{summary_line(record)}</td><td>{record["summary"]["rows"]}</td>'
            f'<td>{record["summary"]["witnesses"]}</td><td>{state}</td><td>{action}</td></tr>'
        )
    return ''.join(rows) or '<tr><td colspan="6" class="empty">尚未分享此捕获的片段。</td></tr>'


def composer(base):
    options = ''.join(f'<option value="{text(value)}">{text(label)}</option>' for value, label in DIRECTIONS)
    return (
        f'<form class="trace-form" method="post" action="{base}/share">'
        '<label>锚点表名<input required name="table" placeholder="substrate"></label>'
        '<label>锚点主键（JSON）<input required name="key" placeholder=\'["East","Mica"]\'></label>'
        f'<label>分支方向<select name="direction">{options}</select></label>'
        '<label>限定外键（可选，如 0:assay）<input name="edge"></label>'
        '<label>深度<input type="number" name="depth" value="2" min="1" max="12" required></label>'
        '<button class="primary">冻结这段并生成链接</button></form>'
    )


def create(bench, capture_id, values):
    anchor_table = field(values, 'table')
    anchor_key = typed_json(field(values, 'key'))
    body = {'anchor': {'table': anchor_table, 'key': anchor_key},
            'direction': field(values, 'direction', 'both'),
            'depth': depth(field(values, 'depth', '2'))}
    raw_edge = field(values, 'edge').strip()
    if raw_edge:
        number, _, child = raw_edge.partition(':')
        if not number.isascii() or not number.isdecimal() or not child:
            raise Fault('invalid_edge', '请输入如 0:assay 的外键。', 400)
        body['edge'] = {'child': child, 'id': int(number)}
    return bench.share(capture_id, body)


def depth(value):
    if not value.isascii() or not value.isdecimal():
        raise Fault('invalid_depth', '深度须为 1 至 12 的整数。', 400)
    number = int(value)
    if not 1 <= number <= MAX_DEPTH:
        raise Fault('invalid_depth', '深度须为 1 至 12 的整数。', 400)
    return number


def show(bench, token):
    try:
        result = bench.shared(token)
    except Fault as exc:
        if exc.code == 'share_revoked':
            return exc.status, page('分享已撤回', 'capture', 'share_withdrawn', id=text(token),
                                    message=text(message(exc)))
        raise
    columns = result['columns']
    rows = ''.join(
        f'<tr><th scope="row">{code(item["table"])}</th><td>{typed(item["key"])}</td>'
        f'<td>{item["depth"]}</td><td>{stored(columns[item["table"]], item["values"])}</td></tr>'
        for item in result['rows']
    )
    arcs = ''.join(
        f'<tr><th scope="row">{arc["depth"]}</th><td>{text("父行" if arc["direction"] == "parent" else "子行")}</td>'
        f'<td>{code(arc["edge"][0])} #{arc["edge"][1]}</td><td>{typed(arc["origin"][1])}</td>'
        f'<td>{typed(arc["target"][1])}</td><td>{text("环或重合" if arc["revisit"] else "首次到达")}</td></tr>'
        for arc in result['arcs']
    )
    return 200, page('冻结证据视图', 'capture', 'share', id=text(token),
                     created=text(result['created_at']), selection=summary_line(result),
                     rows=result['summary']['rows'], witnesses=result['summary']['witnesses'],
                     revisits=result['summary']['revisits'], seal=code(result['seal']),
                     capture_digest=code(result['capture_digest']),
                     schema_digest=code(result['schema_digest']),
                     row_rows=rows or '<tr><td colspan="4" class="empty">这段没有选中行。</td></tr>',
                     arc_rows=arcs or '<tr><td colspan="6" class="empty">这段没有关系见证。</td></tr>')


def stored(columns, values):
    return ' '.join(f'{text(name)}={code(dumps(value))}' for name, value in zip(columns, values))

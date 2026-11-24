"""Present sealed key differences in native order without converting their SQLite types."""

from .forms import field
from .errors import message
from .render import code, options, page, segment, template, text, typed
from ..codec import Fault, dumps, loads

LABELS = {'added': '新增', 'removed': '移除', 'modified': '修改'}
KINDS = ('added', 'removed', 'modified')


def limit_of(values):
    raw = field(values, 'limit', '50')
    if not raw.isascii() or not raw.isdecimal() or not 1 <= int(raw) <= 200:
        raise Fault('invalid_limit', '每页条数须为 1 至 200 的整数。', 400)
    return int(raw)


def cursor_of(values, name):
    raw = field(values, name)
    if not raw:
        return None
    try:
        value = loads(raw)
    except (ValueError, TypeError) as exc:
        raise Fault('invalid_cursor', '续看键须为带表名和主键数组的 JSON。', 400) from exc
    if not isinstance(value, dict) or set(value) != {'table', 'key'} or not isinstance(value['key'], list):
        raise Fault('invalid_cursor', '续看键须为带表名和主键数组的 JSON。', 400)
    return value


def tables(summary):
    if not summary:
        return '<tr><td colspan="4" class="empty">两个捕获的所有表行内容一致。</td></tr>'
    rows = []
    for entry in summary:
        order = ', '.join(('↓' if part['descending'] else '↑') + f'{part["column"]} {part["collation"]}'
                          for part in entry['order'])
        counts = ' · '.join(f'{LABELS[kind]} {entry["counts"][kind]}' for kind in KINDS)
        rows.append(f'<tr><th scope="row">{code(entry["table"])}</th><td>{code(order)}</td>'
                    f'<td>{counts}</td><td>{entry["total"]}</td></tr>')
    return ''.join(rows)


def evidence(capture_id, table, values, label):
    action = f'/workbench/captures/{segment(capture_id)}/trace'
    return (f'<form class="inline" method="post" action="{action}">'
            f'<input type="hidden" name="table" value="{text(table)}">'
            f'<input type="hidden" name="key" value="{text(dumps(values))}">'
            f'<button class="quiet">{text(label)}</button></form>')


def paging(bound, result):
    if result['page']['done']:
        return ('<p class="empty">已核到这个键区间的末尾。</p>' if bound['resumed']
                else '<p class="empty">已核完全部差异。</p>')
    resume = dumps(result['page']['next'])
    hidden = ''.join(f'<input type="hidden" name="{name}" value="{text(bound[name])}">'
                     for name in ('left', 'right', 'table', 'limit'))
    return (f'<form class="actions" method="post" action="/workbench/compare">{hidden}'
            f'<input type="hidden" name="after" value="{text(resume)}">'
            f'<button class="primary">从 {typed(result["page"]["next"]["key"])} 之后续看</button></form>')


def changes(result, bound):
    if result is None:
        return ''
    rows = []
    for item in result['page']['items']:
        sides = ('基准、对照均有' if item['left'] and item['right'] else
                 ('仅基准捕获' if item['left'] else '仅对照捕获'))
        links = ''
        if item['left']:
            links += evidence(bound['left'], item['table'], item['key'], '基准证据')
        if item['right']:
            links += evidence(bound['right'], item['table'], item['key'], '对照证据')
        rows.append(f'<tr><th scope="row">{code(item["table"])}</th><td>{LABELS[item["change"]]}</td>'
                    f'<td>{typed(item["key"])}</td><td>{sides}</td><td>{links}</td></tr>')
    if not rows:
        rows.append('<tr><td colspan="5" class="empty">这个区间没有差异。</td></tr>')
    return template('comparison_result', rows=''.join(rows), tables=tables(result['tables']),
                    paging=paging(bound, result))


def show(bench, values=None):
    values = values or {}
    left, right = field(values, 'left'), field(values, 'right')
    table = field(values, 'table')
    recent = bench.repository.recent_captures()
    ids = [(row['id'], f'{row["name"]} 版本 {row["version"]} · {row["id"][:12]}') for row in recent]
    status, error, result = 200, '', None
    bound = {'left': left, 'right': right, 'table': table, 'limit': field(values, 'limit', '50'),
             'resumed': False}
    if values:
        try:
            limit = limit_of(values)
            body = {'left': left, 'right': right, 'limit': limit}
            if table:
                body['table'] = table
            for name in ('start', 'after'):
                cursor = cursor_of(values, name)
                if cursor is not None:
                    body[name] = cursor
            bound['limit'] = str(limit)
            result = bench.compare(body)
            bound['resumed'] = 'after' in body
        except Fault as exc:
            status, error = exc.status, message(exc)
    known = {key for key, _ in ids}

    def choices(selected):
        available = [('', '请选择捕获'), *ids]
        if selected and selected not in known:
            available.append((selected, selected))
        return options(available, selected)

    return status, page('捕获对比', 'compare', 'compare',
                        left=choices(left), right=choices(right), result=changes(result, bound),
                        table=text(table), limit=bound['limit'], start=text(field(values, 'start')),
                        error=f'<p class="notice" role="alert">{text(error)}</p>' if error else '')

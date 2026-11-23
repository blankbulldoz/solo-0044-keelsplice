"""Present sealed key differences and per-change left/right relationship facts."""

import re

from .forms import field, typed_json
from .errors import message
from .render import code, location, options, page, segment, template, text, typed
from ..codec import Fault, dumps

LABELS = {'added': '新增', 'removed': '移除', 'modified': '修改', 'unchanged': '无变化'}
BADGES = {'values': 'muted', 'respelled': 'ready', 'reparented': 'pending',
          'one-sided': 'muted', 'unchanged': 'muted'}
VERDICTS = {'values': '普通值变化', 'respelled': '子键拼法变化，父行未变', 'reparented': '父对象已更换',
            'one-sided': '仅一侧存在', 'unchanged': '两侧一致'}
HEADLINES = {
    'values': '普通值变化：差异不涉及任何外键子键。',
    'respelled': '子键拼法变化：两侧仍指向同一实际父行。',
    'reparented': '父对象已更换：外键子键指向了不同的父行。',
    'unchanged': '两侧行内容一致，此键不在差异列表中。',
}
EDGE_VERDICTS = {
    'unchanged': '子键与父行均未变',
    'respelled': '拼法变化，父行相同',
    'reparented': '父对象不同',
    'unreferenced': '子键取值变化，两侧均无父引用',
    'one-sided': '仅一侧有此行',
}


def changes(result):
    if result is None:
        return ''
    rows = []
    number = 0
    for table, differences in result['changes'].items():
        for kind, keys in differences.items():
            for key in keys:
                href = location('/workbench/compare/change', left=result['left'], right=result['right'],
                                table=table, key=dumps(key), row=number)
                rows.append(f'<tr id="change-{number}"><th scope="row">{code(table)}</th><td>{LABELS[kind]}</td>'
                            f'<td>{typed(key)}</td><td><a href="{text(href)}">详情</a></td></tr>')
                number += 1
    if not rows:
        rows.append('<tr><td colspan="4" class="empty">这两个捕获的行内容一致。</td></tr>')
    return template('comparison_result', rows=''.join(rows))


def show(bench, values=None):
    values = values or {}
    left, right = field(values, 'left'), field(values, 'right')
    recent = bench.repository.recent_captures()
    ids = [(row['id'], f'{row["name"]} 版本 {row["version"]} · {row["id"][:12]}') for row in recent]
    status, error, result = 200, '', None
    if left and right:
        try:
            result = bench.compare(dict(left=left, right=right))
        except Fault as exc:
            status, error = exc.status, message(exc)
    known = {key for key, _ in ids}

    def choices(selected):
        available = [('', '请选择捕获'), *ids]
        if selected and selected not in known:
            available.append((selected, selected))
        return options(available, selected)

    return status, page('捕获对比', 'compare', 'compare',
                        left=choices(left), right=choices(right), result=changes(result),
                        error=f'<p class="notice" role="alert">{text(error)}</p>' if error else '')


def trace_form(capture_id, table, key, label):
    return (f'<form class="inline" method="post" action="/workbench/captures/{segment(capture_id)}/trace">'
            f'<input type="hidden" name="table" value="{text(table)}">'
            f'<input type="hidden" name="key" value="{text(dumps(key))}">'
            f'<button class="quiet">{label}</button></form>')


def column_rows(detail):
    rows = []
    present = detail['present']
    for col in detail['columns']:
        name = code(col['name']) + (' <span class="muted">（主键）</span>' if col['pk'] else '')
        left = typed(col['left']) if present['left'] else '<span class="muted">不存在</span>'
        right = typed(col['right']) if present['right'] else '<span class="muted">不存在</span>'
        if not all(present.values()):
            state, style = ('仅基准侧存在' if present['left'] else '仅对照侧存在'), ''
        elif col['changed']:
            state, style = '已变化', ' class="changed"'
        else:
            state, style = '一致', ''
        rows.append(f'<tr{style}><th scope="row">{name}</th><td>{left}</td><td>{right}</td><td>{state}</td></tr>')
    return ''.join(rows)


def parent_cell(side, capture_id):
    if side['parent'] is None:
        note = '无父引用（子键含 NULL）' if None in side['child'] else '无见证记录'
        return f'<span class="muted">{note}</span>'
    table, key = side['parent']
    return f'{code(table)} {typed(key)} {trace_form(capture_id, table, key, "核查父行")}'


def reference_rows(detail):
    rows = []
    for ref in detail['references']:
        mapping = ', '.join(f'{child} → {parent}'
                            for child, parent in zip(ref['child_cols'], ref['parent_cols']))
        cells = []
        for side, capture_id in (('left', detail['left']), ('right', detail['right'])):
            if ref[side] is None:
                cells.append('<td class="muted">不存在</td><td class="muted">—</td>')
            else:
                cells.append(f'<td>{typed(ref[side]["child"])}</td><td>{parent_cell(ref[side], capture_id)}</td>')
        rows.append(f'<tr><th scope="row">{code(ref["edge"][0])} #{ref["edge"][1]} → {code(ref["parent"])}'
                    f'<br><span class="muted">{code(mapping)}</span></th>'
                    f'{"".join(cells)}<td>{EDGE_VERDICTS[ref["verdict"]]}</td></tr>')
    return ''.join(rows) or '<tr><td colspan="6" class="empty">此表没有外键关系。</td></tr>'


def change(bench, query):
    left, right = field(query, 'left'), field(query, 'right')
    anchor = field(query, 'row', '0')
    if not re.fullmatch(r'[0-9]{1,6}', anchor):
        anchor = '0'
    detail = bench.compare_detail(dict(left=left, right=right, table=field(query, 'table'),
                                       key=typed_json(field(query, 'key'))))
    kind, verdict = detail['kind'], detail['verdict']
    if kind == 'added':
        headline = '此行仅存在于对照捕获（新增）。'
    elif kind == 'removed':
        headline = '此行仅存在于基准捕获（移除）。'
    else:
        headline = HEADLINES[verdict]
    traces = ''.join(
        trace_form(detail[side], detail['table'], detail['key'], f'在{label}捕获中追踪此行')
        for side, label in (('left', '基准'), ('right', '对照')) if detail['present'][side]
    )
    back = location('/workbench/compare', left=left, right=right) + f'#change-{anchor}'
    badge = f'<span class="status {BADGES[verdict]}">{VERDICTS[verdict]}</span>'
    captures = ''.join(
        f'<div><dt>{label}捕获（{side}）</dt>'
        f'<dd><a href="/workbench/captures/{segment(detail[side])}">{code(detail[side][:12])}</a></dd></div>'
        for side, label in (('left', '基准'), ('right', '对照'))
    )
    return 200, page('变化详情', 'compare', 'comparison_change',
                     table=code(detail['table']), key=typed(detail['key']), kind=LABELS[kind],
                     headline=text(headline), badge=badge, traces=traces, captures=captures,
                     back=text(back), columns=column_rows(detail), references=reference_rows(detail))

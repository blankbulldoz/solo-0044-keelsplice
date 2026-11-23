"""Summarize stored witnesses by relationship, then page through one group.

A capture can hold many arcs for one row.  The page therefore never unfolds
every arc at once: it first reports the exact groups and totals read from the
sealed result, then shows a single group in fixed-size segments.  Every segment
link carries the capture base and the stored typed key, so related rows always
open evidence from the same capture.
"""

from .forms import field, typed_json
from .render import code, location, page, segment, text, typed
from ..codec import Fault, dumps

SEGMENT = 25
ROLES = {'cause': '直接入选原因', 'dependency': '必需父关系'}
DIRECTIONS = {'child': '父行 → 子行（反向纳入）', 'parent': '子行 → 父行（必需父行）'}


def segment_value(values, name):
    raw = field(values, name, '0')
    if not raw.isascii() or not raw.isdecimal() or len(raw) > 9:
        raise Fault('invalid_segment', '证据分段参数须为非负整数。', 400)
    return int(raw)


def root_positions(roots):
    if not roots:
        return '<p class="empty">无，此行走关系纳入而非输入根。</p>'
    return '<ul class="key-list">' + ''.join(
        f'<li>第 {index + 1} 项</li>' for index in roots) + '</ul>'


def group_rows(base, table, key, entries, selected):
    rows = []
    for index, (role, group) in enumerate(entries):
        href = location(f'{base}/trace', table=table, key=dumps(key), group=index)
        current = ' aria-current="true"' if index == selected else ''
        marker = ' class="selected"' if index == selected else ''
        rows.append(
            f'<tr{marker}><th scope="row">{ROLES[role]}</th>'
            f'<td>{code(group["child"])} → {code(group["parent"])}</td>'
            f'<td>{code(group["child"])} #{group["id"]}</td>'
            f'<td>{DIRECTIONS[group["direction"]]}</td><td>{group["count"]}</td>'
            f'<td><a class="button quiet" href="{text(href)}"{current}>分段查看</a></td></tr>'
        )
    return ''.join(rows) or (
        '<tr><td colspan="6" class="empty">此捕获没有指向或来自该行的直接证据。</td></tr>'
    )


def related(arc, role):
    return arc['origin' if role == 'cause' else 'target']


def group_arcs(result, role, group):
    source = result['causes'] if role == 'cause' else result['dependencies']
    edge = group['edge']
    arcs = [arc for arc in source
            if arc['direction'] == group['direction'] and arc['edge'] == edge]
    return sorted(arcs, key=lambda arc: (dumps(related(arc, role)[1]), dumps(related(arc, role)[0])))


def witness_rows(base, role, arcs):
    rows = []
    for arc in arcs:
        table, key = related(arc, role)
        rows.append(
            f'<tr><td>{code(arc["edge"][0])} #{arc["edge"][1]}</td>'
            f'<td>{DIRECTIONS[arc["direction"]]}</td>'
            f'<td>{code(table)} {typed(key)}</td><td><form method="post" action="{base}/trace">'
            f'<input type="hidden" name="table" value="{text(table)}">'
            f'<input type="hidden" name="key" value="{text(dumps(key))}">'
            '<button class="quiet">查看此行</button></form></td></tr>'
        )
    return ''.join(rows) or '<tr><td colspan="4" class="empty">本段没有记录。</td></tr>'


def overview_link(base, table, key):
    return text(location(f'{base}/trace', table=table, key=dumps(key)))


def pager(base, table, key, index, offset, total):
    def link(value, label):
        href = location(f'{base}/trace', table=table, key=dumps(key), group=index, offset=value)
        return f'<a class="button quiet" href="{text(href)}">{label}</a>'

    def halted(label):
        return f'<span class="button quiet disabled" aria-disabled="true">{label}</span>'

    pages = max(1, (total + SEGMENT - 1) // SEGMENT)
    current = offset // SEGMENT + 1
    previous = offset - SEGMENT if offset else None
    following = offset + SEGMENT if offset + SEGMENT < total else None
    return (
        '<nav class="actions pager" aria-label="证据分段">'
        + (link(previous, '上一段') if previous is not None else halted('上一段'))
        + f'<span class="pager-note">第 {current} / {pages} 段 · 第 {offset + 1}–{min(offset + SEGMENT, total)} 条，共 {total} 条</span>'
        + (link(following, '下一段') if following is not None else halted('下一段'))
        + f'<a class="quiet" href="{overview_link(base, table, key)}">回到分组合计</a></nav>'
    )


def selected_group(values, entries):
    if not entries or 'group' not in values:
        return None
    index = segment_value(values, 'group')
    if index >= len(entries):
        raise Fault('invalid_segment', f'所选证据分组超出范围（共 {len(entries)} 组）。', 400)
    return index


def detail(base, table, key, result, entries, values, selected):
    if not entries:
        return '<p class="empty">没有可分段查看的证据。</p>'
    if selected is None:
        return '<p class="empty">先在上表选一组关系，即可按固定大小分段查看该组的相关行。</p>'
    role, group = entries[selected]
    arcs = group_arcs(result, role, group)
    offset = segment_value(values, 'offset')
    offset = min(offset - offset % SEGMENT, max(0, (len(arcs) - 1) // SEGMENT * SEGMENT))
    shown = arcs[offset:offset + SEGMENT]
    return (
        f'<h3>{ROLES[role]} · {code(group["child"])} → {code(group["parent"])} · '
        f'外键 {code(group["child"])} #{group["id"]}</h3>'
        f'<p class="notice info">方向：{DIRECTIONS[group["direction"]]}。以下只展开本组的当前分段。</p>'
        f'<div class="scroll"><table><thead><tr><th scope="col">外键</th><th scope="col">方向</th>'
        f'<th scope="col">相关行</th><th scope="col">操作</th></tr></thead><tbody>'
        f'{witness_rows(base, role, shown)}</tbody></table></div>'
        f'{pager(base, table, key, selected, offset, len(arcs))}'
    )


def show(bench, capture_id, values):
    result = bench.trace(capture_id, dict(table=field(values, 'table'), key=typed_json(field(values, 'key'))))
    table, key = result['row']
    base = f'/workbench/captures/{segment(capture_id)}'
    entries = ([('cause', group) for group in result['grouping']['causes']]
               + [('dependency', group) for group in result['grouping']['dependencies']])
    totals = result['grouping']['totals']
    selected = selected_group(values, entries)
    return 200, page('入选证据', 'capture', 'evidence', base=base, table=code(table),
                     key=typed(key), root_positions=root_positions(result['roots']),
                     cause_total=totals['causes'], dependency_total=totals['dependencies'],
                     cause_groups=totals['cause_groups'], dependency_groups=totals['dependency_groups'],
                     group_total=totals['groups'], record_total=totals['records'],
                     groups=group_rows(base, table, key, entries, selected),
                     detail=detail(base, table, key, result, entries, values, selected))

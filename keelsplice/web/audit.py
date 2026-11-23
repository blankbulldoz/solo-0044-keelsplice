"""Render relationship audit states and accept new judgements."""

import re

from .errors import invalid_route
from .forms import field, typed_json
from .render import code, page, segment, text, typed
from ..codec import Fault, dumps

LABELS = {'verified': '核对过', 'pending': '待说明', 'review': '待复核', 'unmarked': '未标记'}
STYLES = {'verified': 'ready', 'pending': 'pending', 'review': 'review', 'unmarked': 'muted'}
DIRECTIONS = {'parent': '必需父关系', 'child': '配方反向纳入'}
INDEX = re.compile(r'[0-9]{1,10}\Z')


def badge(state):
    return f'<span class="status {STYLES[state]}">{LABELS[state]}</span>'


def relation(arc):
    origin_table, origin_key = arc['origin']
    target_table, target_key = arc['target']
    return (f'{code(origin_table)} {typed(origin_key)} → {code(target_table)} {typed(target_key)}'
            f'<br><span class="muted">{DIRECTIONS[arc["direction"]]} · 外键 #{arc["edge"][1]}</span>')


def provenance(entry, capture_id):
    mark = entry['mark']
    if mark is None:
        return '<span class="muted">尚无标记</span>'
    auditor = text(mark['auditor'])
    if not entry['carried']:
        return f'本捕获标记 #{mark["id"]} · {auditor}'
    link = f'<a href="/workbench/captures/{segment(mark["capture"])}">{code(mark["capture"][:12])}</a>'
    if entry['state'] == 'review':
        return f'标记 #{mark["id"]}（{link} · {auditor}）无法唯一对应，请人工复核'
    return f'沿用标记 #{mark["id"]}（{link} · {auditor}）'


def section(view, capture_id):
    rows = []
    for index, entry in enumerate(view['arcs']):
        href = f'/workbench/captures/{segment(capture_id)}/arcs/{index}'
        rows.append(f'<tr><td>{index}</td><td>{relation(entry["arc"])}</td>'
                    f'<td>{badge(entry["state"])}</td><td>{provenance(entry, capture_id)}</td>'
                    f'<td><a href="{href}">核对与历史</a></td></tr>')
    note = ''
    if view['inactive']:
        note = (f'<p class="muted">另有 {len(view["inactive"])} 条历史标记对应的关系未在本捕获出现，'
                '请到其所属捕获查看当时的记录。</p>')
    body = ''.join(rows) or '<tr><td colspan="5" class="empty">本捕获没有直接关系见证。</td></tr>'
    return body, note


def arc_page(bench, capture_id, number):
    view = bench.audit(capture_id)
    index = int(number)
    if not 0 <= index < len(view['arcs']):
        raise invalid_route()
    entry = view['arcs'][index]
    arc = entry['arc']
    base = f'/workbench/captures/{segment(capture_id)}'
    history = ''.join(
        f'<tr><td>#{mark["id"]}</td>'
        f'<td><a href="/workbench/captures/{segment(mark["capture"])}">{code(mark["capture"][:12])}</a></td>'
        f'<td>{relation(mark["arc"])}</td><td>{badge(mark["verdict"])}</td>'
        f'<td>{text(mark["auditor"])}</td><td>{text(mark["note"]) or "—"}</td></tr>'
        for mark in entry['history']
    ) or '<tr><td colspan="6" class="empty">这条关系尚无核对标记。</td></tr>'
    return 200, page('直接关系核对', 'capture', 'arc', base=base, index=index,
                     state=badge(entry['state']), relation=relation(arc),
                     edge=f'{code(arc["edge"][0])} #{arc["edge"][1]}',
                     direction=DIRECTIONS[arc['direction']], provenance=provenance(entry, capture_id),
                     arc_json=text(dumps(arc)), history=history)


def mark(bench, capture_id, values):
    index = field(values, 'index')
    if not INDEX.fullmatch(index):
        raise Fault('browser_form', '关系序号无效。', 400)
    bench.mark(capture_id, dict(arc=typed_json(field(values, 'arc')),
                                verdict=field(values, 'verdict'),
                                auditor=field(values, 'auditor'), note=field(values, 'note')))
    return f'/workbench/captures/{segment(capture_id)}/arcs/{index}'

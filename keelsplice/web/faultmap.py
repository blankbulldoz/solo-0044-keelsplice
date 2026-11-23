"""Render a fault map, keeping sealed evidence and the later read visibly apart."""

from .errors import message
from .forms import field, typed_json
from .render import code, page, segment, text, typed
from ..codec import Fault, dumps
from ..fault import KIND_LABELS

DIRECTION = {'parent': '指向父行', 'child': '反向子行'}
REFERENCE = {
    'ok': '已找到', 'null': '可空键为 NULL', 'missing': '父键缺失',
    'ambiguous': '父键不唯一', 'not-followed': '配方未纳入此反向关系',
    'absent-row': '当前快照无此行',
}
SAMPLE = {'inside': '<span class="status ready">样本内</span>',
          'outside': '<span class="status muted">样本外</span>'}
KINDS = {0: 'plain', 2: 'virtual', 3: 'stored'}


def choices(sealed):
    return ''.join(
        f'<option value="{text(table)}.{text(col["name"])}"'
        f'{" disabled" if col["hidden"] == 1 else ""}>'
        f'{text(table)} · {text(col["name"])}（{text(KIND_LABELS[KINDS.get(col["hidden"], "plain")])}）</option>'
        for table, meta in sealed['schema']['tables'].items() for col in meta['columns']
    )


def form(base, sealed, values, error=''):
    operator = field(values, 'operator', '=')
    raw = field(values, 'value')
    operators = ''.join(
        f'<option{" selected" if operator == item else ""}>{item}</option>'
        for item in ('=', '!=', '<', '<=', '>', '>=', 'IS NULL', 'IS NOT NULL')
    )
    return (
        f'<form class="fault-form" method="post" action="{base}/fault">'
        f'<label>表与列<select name="column" required>{choices(sealed)}</select></label>'
        f'<label>运算符<select name="operator" required>{operators}</select></label>'
        f'<label>值（SQLite JSON 编码；NULL 测试留空）'
        f'<input name="value" value="{text(raw)}" placeholder="&quot;east&quot;、11 或 {{&quot;real&quot;:&quot;0x1.8p+2&quot;}}">'
        f'</label><button class="primary">绘制断层地图</button></form>'
        + (f'<p class="notice" role="alert">{text(error)}</p>' if error else '')
    )


def hidden_condition(report):
    condition = report['condition']
    fields = {'condition_column': f'{condition["table"]}.{condition["column"]}',
              'condition_operator': condition['operator'],
              'condition_value': '' if not condition['value_given'] else dumps(condition['value'])}
    return ''.join(f'<input type="hidden" name="{name}" value="{text(value)}">'
                   for name, value in fields.items())


def time_cell(item):
    if item['sealed'] and item['current']:
        return '<span class="status ready">封存时＋现在</span>'
    if item['sealed']:
        return '<span class="status pending">仅封存时</span>'
    return '<span class="status info">仅后来观察</span>'


def neighbour_rows(base, capture_id, entry, row, report):
    if not entry['rows']:
        state = entry['sealed'] or entry['current'] or {}
        note = {
            'null': '可空外键在封存时含 NULL，没有必需父行。',
            'missing': '封存时找不到匹配的父行。',
            'not-followed': '该反向关系未写入配方，封存证据不包含子行。',
            'absent-row': '边界行在当前快照中已不存在。',
        }.get(state.get('reference'), '没有相邻行。')
        return f'<tr><td colspan="5" class="empty">{text(note)}</td></tr>'
    rows = []
    for item in entry['rows']:
        cycle = '<span class="status pending">循环边</span> ' if item['cycle'] else ''
        rows.append(
            f'<tr><th scope="row">{code(item["table"])}</th><td>{typed(item["key"])}</td>'
            f'<td>{SAMPLE[item["sample"]]}</td><td>{time_cell(item)}</td>'
            f'<td>{cycle}<form class="inline" method="post" action="{base}/fault/evidence">'
            f'<input type="hidden" name="table" value="{text(item["table"])}">'
            f'<input type="hidden" name="key" value="{text(dumps(item["key"]))}">'
            f'<input type="hidden" name="origin_table" value="{text(report["condition"]["table"])}">'
            f'<input type="hidden" name="origin_key" value="{text(dumps(row["key"]))}">'
            f'<input type="hidden" name="edge" value="{text(dumps(entry["edge"]))}">'
            f'<input type="hidden" name="direction" value="{text(entry["direction"])}">'
            f'{hidden_condition(report)}'
            '<button class="quiet">回到原证据</button></form></td></tr>'
        )
    return ''.join(rows)


def neighbour_blocks(base, capture_id, item, report):
    blocks = []
    for entry in item['neighbours']:
        sealed_state = entry['sealed'] or {}
        current_state = entry['current'] or {}
        notes = [text(DIRECTION[entry['direction']]),
                 f'{code(", ".join(entry["child_cols"]))} → {code(", ".join(entry["parent_cols"]))}']
        if sealed_state:
            notes.append('封存时：' + text(REFERENCE.get(sealed_state['reference'], sealed_state['reference'])))
        if entry['current'] is not None:
            notes.append('现在：' + text(REFERENCE.get(current_state['reference'], current_state['reference'])))
        if entry['nullable']:
            notes.append('可空键')
        if entry['direction'] == 'child' and not entry['followed']:
            notes.append('配方未采纳')
        blocks.append(
            f'<details class="edge"><summary>{" · ".join(notes)}</summary>'
            f'<div class="scroll"><table><thead><tr><th scope="col">相邻表</th>'
            f'<th scope="col">带类型主键</th><th scope="col">样本内 / 外</th>'
            f'<th scope="col">时间</th><th scope="col">标记与证据</th></tr></thead><tbody>'
            f'{neighbour_rows(base, capture_id, entry, item, report)}</tbody></table></div></details>'
        )
    return ''.join(blocks) or '<p class="empty">此表没有声明的外键关系。</p>'


def boundary_blocks(base, capture_id, report):
    table = report['condition']['table']
    if not report['rows']:
        return '<p class="empty">封存样本中没有满足此条件的行。</p>'
    blocks = []
    for item in report['rows']:
        sealed_value = code(dumps(item['sealed']['column_value'])) if item['sealed'] else '<span class="muted">未封存</span>'
        current = item['current']
        if current is None:
            current_value = '<span class="muted">未观察</span>'
        elif not current['present']:
            current_value = '<span class="status muted">现在已无此行</span>'
        elif not current['matches_condition']:
            current_value = f'{code(dumps(current["column_value"]))} <span class="status pending">现在不再满足条件</span>'
        else:
            current_value = code(dumps(current['column_value']))
        changed = ' <span class="status pending">行值已变</span>' if item['changed'] else ''
        blocks.append(
            f'<details class="boundary"><summary>{code(table)} {typed(item["key"])}　'
            f'{SAMPLE[item["sample"]]}{changed}</summary>'
            f'<dl class="facts"><div><dt>封存时列值</dt><dd>{sealed_value}</dd></div>'
            f'<div><dt>现在列值</dt><dd>{current_value}</dd></div>'
            f'<div><dt>样本边界</dt><dd>{SAMPLE[item["sample"]]}</dd></div></dl>'
            f'{neighbour_blocks(base, capture_id, item, report)}</details>'
        )
    return ''.join(blocks)


def observation(report):
    later = report['observation']
    if later['state'] == 'current':
        return (f'<p class="notice info">封存时证据来自捕获（<code>{text(report["sealed"]["sealed_at"] or "未记录时间")}</code>），'
                f'后来观察来自 <code>{text(later["at"])}</code> 的独立读事务。'
                '「后来观察」只说明该邻居在封存后的这次读取中出现，捕获无法证明它是在封存之后才写入的。</p>')
    reasons = {'schema_drift': '当前源模式已与封存模式不同。',
               'source_unavailable': '这个捕获的源别名已不再注册。',
               'observation_disabled': '本次只显示封存时证据。'}
    return (f'<p class="notice">未执行后来观察：{text(reasons.get(later["reason"], later["reason"]))}'
            ' 下方所有相邻行只反映封存时的捕获。</p>')


def summary(report):
    data = report['summary']
    facts = (('边界行', data['rows']), ('样本内 / 外', f'{data["inside"]} / {data["outside"]}'),
             ('封存关系证据', data['sealed_neighbours']), ('仅封存时', data['sealed_only']),
             ('仅后来观察', data['current_only']), ('可空边缘', data['null_references']),
             ('循环边', data['cycles']), ('行值已变', data['changed']))
    return ('<dl class="facts">'
            + ''.join(f'<div><dt>{text(name)}</dt><dd>{value}</dd></div>' for name, value in facts)
            + '</dl>'
            + ('<p class="notice">边界行超过 200 行上限，只显示前 200 行。</p>' if data['truncated'] else ''))


def show(bench, capture_id, values):
    base = f'/workbench/captures/{segment(capture_id)}'
    sealed = bench.repository.load_capture(capture_id)['sealed']
    picked = field(values, 'column')
    if not picked:
        return 200, page('断层地图', 'capture', 'fault', id=text(capture_id), base=base,
                         condition_form=form(base, sealed, values),
                         observation='<p class="notice info">选择一个封存表上的条件即可绘制断层地图。</p>',
                         summary='', boundaries='')
    table, _, column = picked.partition('.')
    body = {'table': table, 'column': column, 'operator': field(values, 'operator', '=')}
    raw = field(values, 'value')
    if raw.strip():
        try:
            body['value'] = typed_json(raw)
        except Fault:
            return 200, page('断层地图', 'capture', 'fault', id=text(capture_id), base=base,
                             condition_form=form(base, sealed, values, '值必须是有效的 SQLite JSON 编码。'),
                             observation='', summary='', boundaries='')
    try:
        report = bench.fault_map(capture_id, body)
    except Fault as exc:
        return exc.status, page('断层地图', 'capture', 'fault', id=text(capture_id), base=base,
                                condition_form=form(base, sealed, values, message(exc)),
                                observation='', summary='', boundaries='')
    return 200, page('断层地图', 'capture', 'fault', id=text(capture_id), base=base,
                     condition_form=form(base, sealed, values), observation=observation(report),
                     summary=summary(report), boundaries=boundary_blocks(base, capture_id, report))


def evidence(bench, capture_id, values):
    """Explain one neighbour by the captured witness, or say there is none."""
    base = f'/workbench/captures/{segment(capture_id)}'
    sealed = bench.repository.load_capture(capture_id)['sealed']
    table = field(values, 'table')
    key = typed_json(field(values, 'key'))
    origin_table = field(values, 'origin_table')
    origin_key = typed_json(field(values, 'origin_key'))
    edge = typed_json(field(values, 'edge'))
    direction = field(values, 'direction')
    back = dict(condition_column=field(values, 'condition_column'),
                condition_operator=field(values, 'condition_operator'),
                condition_value=field(values, 'condition_value'))
    target = [table, key]
    origin = [origin_table, origin_key]
    witness = next((arc for arc in sealed['result']['arcs']
                    if arc['edge'] == edge and arc['direction'] == direction
                    and arc['origin'] == origin and arc['target'] == target), None)
    captured = dumps(key) in sealed['result']['rows'].get(table, {})
    if witness is not None:
        verdict = '<span class="status ready">封存证据已确认</span>'
        notice = ('<p class="notice info">这条关系在封存捕获中有直接证据：'
                  f'<code>{text(dumps(witness))}</code>。它属于封存时的时间，不随后来的源更新改变。</p>')
    elif captured:
        verdict = '<span class="status pending">行已封存，关系是后来观察</span>'
        notice = ('<p class="notice">目标行在封存样本内，但捕获没有记录这条方向的关系；'
                  '它来自后来观察，不能作为封存时关系的证明。</p>')
    else:
        verdict = '<span class="status muted">仅后来观察，无封存证据</span>'
        notice = ('<p class="notice">目标行不在封存样本中，捕获无法证明它在封存时存在，也无法证明它何时出现。'
                  '此处只报告封存之后一次读取的结果。</p>')
    rows = []
    for role, address in (('边界行', origin), ('相邻行', target)):
        rows.append(f'<tr><th scope="row">{role}</th><td>{code(address[0])}</td>'
                    f'<td>{typed(address[1])}</td></tr>')
    parts = [f'<section><h2>关系两端</h2><div class="scroll"><table><thead><tr><th scope="col">角色</th>'
             f'<th scope="col">表</th><th scope="col">带类型主键</th></tr></thead><tbody>'
             f'{"".join(rows)}</tbody></table></div></section>']
    for role, address in (('边界行', origin), ('相邻行', target)):
        parts.append(trace_section(bench, capture_id, role, address, base))
    back_form = (
        f'<form method="post" action="{base}/fault">'
        f'<input type="hidden" name="column" value="{text(back["condition_column"])}">'
        f'<input type="hidden" name="operator" value="{text(back["condition_operator"])}">'
        f'<input type="hidden" name="value" value="{text(back["condition_value"])}">'
        '<button>回到断层地图</button></form>'
    )
    return 200, page('原证据确认', 'capture', 'fault_evidence', base=base, verdict=verdict,
                     notice=notice, sections=''.join(parts), back=back_form,
                     edge=code(dumps(edge)), direction=text(DIRECTION.get(direction, direction)))


def trace_section(bench, capture_id, role, address, base):
    table, key_values = address
    try:
        result = bench.trace(capture_id, {'table': table, 'key': key_values})
    except Fault:
        return (f'<section><h2>{text(role)}封存证据</h2>'
                '<p class="empty">该行不在封存样本中，没有可确认的封存原因。</p></section>')
    causes = ''.join(
        f'<tr><td>{text(DIRECTION.get(arc["direction"], arc["direction"]))}</td>'
        f'<td>{code(arc["edge"][0])} #{arc["edge"][1]}</td>'
        f'<td>{code(arc["origin"][0])} {typed(arc["origin"][1])}</td></tr>'
        for arc in result['causes'])
    occurrences = ', '.join(str(index + 1) for index in result['roots']) or '无'
    empty = '<tr><td colspan="3" class="empty">没有直接入选原因。</td></tr>'
    return (f'<section><h2>{text(role)}封存证据</h2>'
            f'<dl class="facts"><div><dt>根序号</dt><dd>{text(occurrences)}</dd></div></dl>'
            '<div class="scroll"><table><thead><tr><th scope="col">方向</th><th scope="col">外键</th>'
            f'<th scope="col">来源行</th></tr></thead><tbody>'
            f'{causes or empty}</tbody></table></div>'
            f'<form class="inline" method="post" action="{base}/trace">'
            f'<input type="hidden" name="table" value="{text(table)}">'
            f'<input type="hidden" name="key" value="{text(dumps(key_values))}">'
            '<button class="quiet">打开完整证据页</button></form></section>')

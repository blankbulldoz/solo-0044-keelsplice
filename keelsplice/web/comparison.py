"""Present sealed key differences and regroup them along a chosen edge direction."""

from .forms import field
from .errors import message
from .render import code, options, page, segment, template, text, typed
from ..codec import Fault, dumps

LABELS = {'added': '新增', 'removed': '移除', 'modified': '修改'}
STYLES = {'added': 'ready', 'removed': 'muted', 'modified': 'pending'}
SIDES = {'left': '基准', 'right': '对照'}


def badge(kind):
    return f'<span class="status {STYLES[kind]}">{LABELS[kind]}</span>'


def parse_direction(token):
    number, _, rest = token.partition(':')
    child, _, towards = rest.rpartition(':')
    if (not number.isascii() or not number.isdecimal() or len(number) > 10
            or towards not in ('parent', 'child')):
        raise Fault('invalid_direction', '归并方向须引用对比模式中已登记的外键及有效朝向。')
    return dict(child=child, id=int(number), towards=towards)


def direction_choices(schema, active):
    values = [('', '不归并（平铺）')]
    for edge in schema['edges'] if schema else ():
        cols = ', '.join(edge['child_cols'])
        token = f'{edge["id"]}:{edge["child"]}'
        values.append((f'{token}:child', f'{edge["parent"]} → {edge["child"]}（{cols}）· 向子归并'))
        values.append((f'{token}:parent', f'{edge["child"]} → {edge["parent"]}（{cols}）· 向父归并'))
    if active and active not in {value for value, _ in values}:
        values.append((active, active))
    return options(values, active)


def provenance(captures, address, sides):
    table, values = address
    forms = ''.join(
        f'<form method="post" action="/workbench/captures/{segment(captures[side])}/trace">'
        f'<input type="hidden" name="table" value="{text(table)}">'
        f'<input type="hidden" name="key" value="{text(dumps(values))}">'
        f'<button class="quiet">{SIDES[side]}</button></form>'
        for side in sides
    )
    return f'<div class="inline">{forms}</div>'


def member_table(captures, entries):
    rows = ''.join(
        f'<tr><td>{badge(entry["kind"])}</td><td>{code(entry["row"][0])}</td>'
        f'<td>{typed(entry["row"][1])}</td><td>{provenance(captures, entry["row"], entry["sides"])}</td></tr>'
        for entry in entries
    )
    return ('<div class="scroll"><table><thead><tr><th scope="col">变化</th><th scope="col">表</th>'
            '<th scope="col">实存主键</th><th scope="col">溯源</th></tr></thead>'
            f'<tbody>{rows}</tbody></table></div>')


def group_block(captures, group):
    table, values = group['anchor']
    own = badge(group['own']) if group['own'] else ''
    members = member_table(captures, group['members']) if group['members'] else '<p class="muted">组内无成员差异。</p>'
    return template('comparison_group', anchor=f'{code(table)} {typed(values)}', own=own,
                    count=len(group['members']) + int(group['own'] is not None),
                    trace=provenance(captures, group['anchor'], group['sides']), members=members)


def groups_view(result, left, right):
    groups = result['groups']
    if groups is None:
        return ''
    captures = {'left': left, 'right': right}
    direction = groups['direction']
    towards = '向子归并' if direction['towards'] == 'child' else '向父归并'
    label = f'{direction["anchor"]} → {direction["member"]}（{towards}）'
    totals = groups['totals']
    blocks = ''.join(group_block(captures, group) for group in groups['groups'])
    if not blocks:
        blocks = '<p class="muted">没有可归并的差异。</p>'
    rest = groups['ungrouped']
    remaining = (template('comparison_ungrouped', count=len(rest), table=member_table(captures, rest))
                 if rest else '<p class="muted">所有差异均已归并入组。</p>')
    return template('comparison_groups', direction=text(label), changes=totals['changes'],
                    added=totals['added'], removed=totals['removed'], modified=totals['modified'],
                    references=totals['references'], ungrouped=totals['ungrouped'],
                    groups=blocks, rest=remaining)


def changes(result):
    if result is None:
        return ''
    rows = []
    for table, differences in result['changes'].items():
        for kind, keys in differences.items():
            rows.extend(f'<tr><th scope="row">{code(table)}</th><td>{LABELS[kind]}</td><td>{typed(key)}</td></tr>'
                        for key in keys)
    if not rows:
        rows.append('<tr><td colspan="3" class="empty">这两个捕获的行内容一致。</td></tr>')
    return template('comparison_result', rows=''.join(rows))


def show(bench, values=None):
    values = values or {}
    left, right = field(values, 'left'), field(values, 'right')
    token = field(values, 'direction')
    recent = bench.repository.recent_captures()
    ids = [(row['id'], f'{row["name"]} 版本 {row["version"]} · {row["id"][:12]}') for row in recent]
    status, error, result, schema = 200, '', None, None
    if values:
        try:
            body = dict(left=left, right=right)
            if token:
                body['direction'] = parse_direction(token)
            result = bench.compare(body)
            schema = bench.repository.load_capture(left)['sealed']['schema']
        except Fault as exc:
            status, error = exc.status, message(exc)
    known = {key for key, _ in ids}

    def choices(selected):
        available = [('', '请选择捕获'), *ids]
        if selected and selected not in known:
            available.append((selected, selected))
        return options(available, selected)

    outcome = groups_view(result, left, right) + changes(result) if result is not None else ''
    return status, page('捕获对比', 'compare', 'compare',
                        left=choices(left), right=choices(right), directions=direction_choices(schema, token),
                        result=outcome,
                        error=f'<p class="notice" role="alert">{text(error)}</p>' if error else '')

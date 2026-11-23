"""Render the two-source dependency path troubleshooting board."""

from .directory import source_options
from .errors import message
from .forms import field
from .render import code, location, page, template, text
from ..codec import Fault
from ..pathboard import DEFAULT_STEPS, MAX_STEPS

STYLES = {'compatible': 'ready', 'ambiguous': 'pending', 'mapping_mismatch': 'pending', 'no_path': 'muted'}
DIRECTIONS = {'parent': '正向（子表 → 父表）', 'child': '反向（父表 → 子表）'}


def edge_boxes(schema, name, picked):
    boxes = []
    for edge in schema['edges']:
        token = f'{edge["id"]}:{edge["child"]}'
        checked = ' checked' if picked is None or token in picked else ''
        loop = ' · 自环' if edge['child'] == edge['parent'] else ''
        mapping = f'{", ".join(edge["child_cols"])} → {", ".join(edge["parent_cols"])}'
        caption = f'{edge["child"]} → {edge["parent"]} #{edge["id"]}{loop}（{mapping}）'
        boxes.append(f'<label class="edge-choice"><input type="checkbox" name="{name}" value="{text(token)}"'
                     f'{checked}> {code(caption)}</label>')
    return ''.join(boxes) or '<p class="empty">此源库没有可选关系。</p>'


def table_link(alias, name):
    return f'<a href="{text(location("/workbench", source=alias, table=name))}">{code(name)}</a>'


def chain(hops):
    parts = [hops[0]['origin']] if hops else []
    for hop in hops:
        parts.append(f'—[{", ".join(hop["origin_cols"])} ⇒ {", ".join(hop["target_cols"])}]→ {hop["target"]}')
    return code(' '.join(parts))


def step_rows(alias, steps):
    rows = []
    for step in steps:
        edge = step['edge']
        mapping = f'{", ".join(step["origin_cols"])} → {", ".join(step["target_cols"])}'
        rows.append(f'<tr><td>{table_link(alias, step["origin"])} → {table_link(alias, step["target"])}</td>'
                    f'<td>{code(mapping)}</td><td>{code(edge["child"])} #{edge["id"]}</td>'
                    f'<td>{DIRECTIONS[step["direction"]]}</td></tr>')
    return ''.join(rows)


def solution_block(result, index, solution):
    def side(label, alias, steps):
        return (f'<section><h4>{label} · {text(alias)}</h4><div class="scroll"><table>'
                '<thead><tr><th scope="col">经过</th><th scope="col">列映射</th><th scope="col">外键</th>'
                '<th scope="col">方向</th></tr></thead>'
                f'<tbody>{step_rows(alias, steps)}</tbody></table></div></section>')

    return (f'<section><h3>方案 {index}</h3><p>{chain(solution["signature"])}</p><div class="editor-grid">'
            f'{side("左侧", result["left"]["source"], solution["left"]["steps"])}'
            f'{side("右侧", result["right"]["source"], solution["right"]["steps"])}</div></section>')


def mismatch_block(result):
    def column(key, label):
        side = result[key]
        items = ''.join(f'<li>{chain(sig)}</li>' for sig in side['sample'])
        omitted = side['signatures'] - len(side['sample'])
        if omitted > 0:
            items += f'<li class="empty">… 其余 {omitted} 组从略</li>'
        return f'<section><h3>{label} · {text(side["source"])}</h3><ul class="data-list">{items}</ul></section>'

    return ('<p class="muted">两侧各自都能走通，但没有任何一对路径使用相同的逐跳列映射。</p>'
            f'<div class="editor-grid">{column("left", "左侧映射")}{column("right", "右侧映射")}</div>')


def presence_block(result):
    rows = []
    for key, label in (('left', '左侧'), ('right', '右侧')):
        side = result[key]
        for role, role_label in (('start', '起点表'), ('end', '终点表')):
            name = side['endpoints'][role]
            cell = table_link(side['source'], name) if name else '不存在'
            rows.append(f'<tr><th scope="row">{label} · {text(side["source"])}</th>'
                        f'<td>{role_label}</td><td>{cell}</td></tr>')
    return ('<section><h3>起止表在两侧目录中的存在情况</h3><div class="scroll"><table>'
            '<thead><tr><th scope="col">侧</th><th scope="col">端点</th><th scope="col">目录中的表</th></tr></thead>'
            f'<tbody>{"".join(rows)}</tbody></table></div></section>')


def outcome_label(result):
    return {
        'compatible': '可对接：两侧存在唯一共同路径方案。',
        'ambiguous': f'方案不唯一：两侧共享 {result["shared_count"]} 组对接方案，需人工选定。',
        'mapping_mismatch': '列映射不一致：两侧路径经过不同的列映射。',
        'no_path': '没有路径：至少一侧在允许关系和步数上限内走不通。',
    }[result['outcome']]


def render_result(result):
    facts = ''.join(
        f'<div><dt>{term}</dt><dd>{value}</dd></div>' for term, value in (
            ('左侧数据源', code(result['left']['source'])),
            ('右侧数据源', code(result['right']['source'])),
            ('左侧路径 / 映射', f'{result["left"]["paths"]} / {result["left"]["signatures"]}'),
            ('右侧路径 / 映射', f'{result["right"]["paths"]} / {result["right"]["signatures"]}'),
            ('步数上限', result['max_steps']),
        ))
    outcome = result['outcome']
    if outcome in ('compatible', 'ambiguous'):
        sections = ''.join(solution_block(result, index, solution)
                           for index, solution in enumerate(result['shared'], 1))
        omitted = result['shared_count'] - len(result['shared'])
        if omitted > 0:
            sections += f'<p class="muted">… 其余 {omitted} 组方案从略。</p>'
    elif outcome == 'mapping_mismatch':
        sections = mismatch_block(result)
    else:
        sections = presence_block(result)
    return template('paths_result', style=STYLES[outcome], label=text(outcome_label(result)),
                    facts=facts, sections=sections)


def board(bench, left_alias, right_alias, values, result=None, error='', status=200):
    submitted = values is not None
    values = values or {}
    left_catalog = bench.catalog(left_alias)
    right_catalog = bench.catalog(right_alias)
    return status, page('路径排障', 'paths', 'paths',
                        left_options=source_options(bench, left_alias),
                        right_options=source_options(bench, right_alias),
                        left_source=text(left_alias), right_source=text(right_alias),
                        start=text(field(values, 'start')), end=text(field(values, 'end')),
                        max_steps=text(field(values, 'max_steps', str(DEFAULT_STEPS))), max_limit=MAX_STEPS,
                        left_edges=edge_boxes(left_catalog['schema'], 'left_edge',
                                              values.get('left_edge', []) if submitted else None),
                        right_edges=edge_boxes(right_catalog['schema'], 'right_edge',
                                               values.get('right_edge', []) if submitted else None),
                        error=f'<p role="alert" class="notice">{text(error)}</p>' if error else '',
                        result=render_result(result) if result else '')


def show(bench, query):
    aliases = sorted(bench.repository.sources)
    fallback = aliases[0] if aliases else ''
    left_alias = field(query, 'left', fallback)
    right_alias = field(query, 'right', aliases[1] if len(aliases) > 1 else fallback)
    return board(bench, left_alias, right_alias, None)


def parse_edges(tokens):
    chosen = []
    for item in tokens:
        number, _, child = item.partition(':')
        if not number.isascii() or not number.isdecimal() or len(number) > 10 or not child:
            raise Fault('invalid_edge', 'Invalid edge')
        chosen.append(dict(child=child, id=int(number)))
    return chosen


def check(bench, values):
    left_alias = field(values, 'left_source')
    right_alias = field(values, 'right_source')
    try:
        raw_steps = field(values, 'max_steps')
        if not raw_steps.isascii() or not raw_steps.isdecimal() or len(raw_steps) > 2:
            raise Fault('invalid_steps', 'Invalid max_steps')
        body = {'start': field(values, 'start'), 'end': field(values, 'end'),
                'max_steps': int(raw_steps),
                'left': {'source': left_alias, 'edges': parse_edges(values.get('left_edge', []))},
                'right': {'source': right_alias, 'edges': parse_edges(values.get('right_edge', []))}}
        result = bench.path_check(body)
    except Fault as exc:
        if exc.code in {'unknown_source', 'unsupported_schema'}:
            raise
        return board(bench, left_alias, right_alias, values, error=message(exc), status=exc.status)
    return board(bench, left_alias, right_alias, values, result=result)

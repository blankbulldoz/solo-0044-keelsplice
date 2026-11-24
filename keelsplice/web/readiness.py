"""Render a source-index readiness report, keeping plan, work and closure distinct."""

from .render import code, page, segment, text, typed

VERDICTS = {
    'seeks': ('获得定位', 'ready'),
    'less_work': ('省下工作量', 'ready'),
    'neutral': ('无变化', 'muted'),
    'costs': ('反而更贵', 'pending'),
    'unusable': ('无法用于此读', 'pending'),
}
KINDS = {'root': '根定位', 'parent': '父行定位', 'child': '子行扇出'}
SHAPES = {'seek': '键定位', 'scan': '全表扫描', 'unusable': '无法取样'}
DELTAS = {'less': '低于无索引', 'same': '与无索引相同', 'more': '高于无索引'}


def verdict(item):
    label, style = VERDICTS[item['verdict']]
    return f'<span class="status {style}">{label}</span>'


def work(value):
    if value is None:
        return '<span class="muted">未测量</span>'
    if value.get('skipped'):
        return '<span class="muted">超出探测预算</span>'
    if value.get('capped'):
        return f'{value["units"]:,} 步以上'
    return f'{value["units"]:,} 步'


def path(value):
    if value is None or value.get('detail') is None:
        return '<span class="muted">无可用计划</span>'
    return code(value['detail'])


def via(value):
    parts = []
    for reason in value:
        if 'root' in reason:
            parts.append(f'根 {typed(reason["root"])}')
        elif 'edge' in reason:
            parts.append(f'{code(reason["edge"][0])} 外键 {reason["edge"][1]}')
    return '、'.join(parts)


def condition(item):
    pieces = [code(', '.join(item['columns']))] if item['columns'] else []
    pieces.append('<span class="muted">部分索引</span>' if item['partial'] else '')
    pieces.append('<span class="muted">唯一</span>' if item['unique'] else '')
    return ' '.join(piece for piece in pieces if piece)


def index_rows(read):
    if not read['indexes']:
        return '<tr><td colspan="5" class="empty">此表没有可枚举的索引。</td></tr>'
    return ''.join(
        f'<tr><th scope="row">{code(item["name"])}</th><td>{condition(item)}</td>'
        f'<td>{verdict(item)}</td><td>{DELTAS.get(item["work_delta"], "未知")}</td>'
        f'<td>{"是" if item["chosen"] else "否"} · {SHAPES.get(item["shape"], text(item["shape"]))}</td></tr>'
        for item in read['indexes']
    )


def read_block(read):
    natural, baseline = read['natural'], read['baseline']
    sample = ''
    if read['sample'] is not None:
        sample = f'取样自 {code(read["sample"]["table"])} ({", ".join(read["sample"]["columns"])})'
    note = f'<p class="notice info">{text(read["note"])}</p>' if read.get('note') else ''
    return (
        f'<section><h2>{KINDS.get(read["kind"], text(read["kind"]))} · {code(read["table"])}</h2>'
        f'<p class="muted">用于 {via(read["via"])}；{sample}</p>{note}'
        f'<dl class="facts">'
        f'<div><dt>当前计划</dt><dd>{SHAPES.get(natural["shape"], text(natural["shape"]))} · {work(natural["work"])}</dd></div>'
        f'<div><dt>无索引基线</dt><dd>{SHAPES.get(baseline["shape"], text(baseline["shape"]))} · {work(baseline["work"])}</dd></div>'
        f'<div class="wide"><dt>实际计划</dt><dd>{path(natural)}</dd></div>'
        '</dl>'
        '<div class="scroll"><table><thead><tr><th scope="col">索引</th><th scope="col">列与谓词</th>'
        '<th scope="col">对本读</th><th scope="col">工作量</th><th scope="col">被采用</th></tr></thead>'
        f'<tbody>{index_rows(read)}</tbody></table></div></section>'
    )


def counts(summary):
    return ''.join(
        f'<tr><th scope="row">{VERDICTS.get(key, (text(key), ""))[0]}</th><td>{value}</td></tr>'
        for key, value in sorted(summary.items())
    ) or '<tr><td colspan="2" class="empty">没有可枚举的索引。</td></tr>'


def closure(observed):
    if 'error' in observed:
        return (f'<p class="notice">闭包未能完成：{text(observed["message"])} '
                f'（{code(observed["error"])}）</p>')
    summary = observed['summary']
    rows = ''.join(f'<tr><th scope="row">{code(table)}</th><td>{count}</td></tr>'
                   for table, count in summary['counts'].items())
    return (
        '<p class="notice info">闭包由实际捕获引擎在同一快照上算出；装不装索引都不会改变它选中的行。</p>'
        f'<dl class="facts"><div><dt>入选行</dt><dd>{summary["rows"]}</dd></div>'
        f'<div><dt>关系见证</dt><dd>{summary["witnesses"]}</dd></div>'
        f'<div><dt>根出现项</dt><dd>{summary["root_occurrences"]}</dd></div>'
        f'<div class="wide"><dt>行集合摘要</dt><dd>{code(observed["rows"][:16])}</dd></div></dl>'
        '<div class="scroll"><table><thead><tr><th scope="col">表</th><th scope="col">入选行</th>'
        f'</tr></thead><tbody>{rows}</tbody></table></div>'
    )


def show(bench, name, number):
    report = bench.readiness(dict(name=name, version=number))
    base = f'/workbench/recipes/{segment(name)}/{number}'
    return 200, page('索引就绪', 'recipe', 'readiness', name=text(name), number=number, base=base,
                     digest=text(report['schema_digest']), summary=counts(report['summary']),
                     reads=''.join(read_block(read) for read in report['reads']),
                     closure=closure(report['closure']),
                     probe=f'{report["budget"]["probe_spent"]:,} / {report["budget"]["probe_steps"]:,}',
                     active='投用中' if report['active'] else '已停用')

"""Render a table-and-relationship cross-reference across retained history."""

from .directory import source_options, table_links
from .errors import message
from .forms import field
from .render import code, location, page, segment, text, typed
from ..codec import Fault, dumps

STATUS_ORDER = ('correspond', 'divergent', 'incomparable')
STATUS_LABELS = {'correspond': '真实对应', 'divergent': '同名不同定义', 'incomparable': '历史关系已不可比'}
STATUS_CLASS = {'correspond': 'ready', 'divergent': 'pending', 'incomparable': 'muted'}
SCHEMA_LABELS = {'current': '当前模式', 'retained': '历史模式', 'unavailable': '模式缺失'}
REASON_TEXT = {
    'used': '子表、父表与列映射都与当前关系一致。',
    'number_definition': '旧模式把这个外键号分给了另一条定义，不能当作同一条关系。',
    'definition_changed': '子表与父表相同，但列映射或引用动作指向另一条定义。',
    'table_absent': '旧模式中缺少这张表，无法对齐。',
    'schema_unavailable': '旧模式没有被任何捕获保留，无法比较。',
    'table_used': '历史记录用过这张表，但没有直接使用这条关系。',
}
NOTE_LABELS = {
    'renumbered': '同一关系在旧模式中使用外键号 {}',
    'number_reused': '旧模式把外键号 {} 分给了另一条定义',
    'actions_changed': '引用动作已改变（旧：ON UPDATE {} / ON DELETE {}）',
}


def _notes(notes):
    items = []
    for name, value in notes:
        template = NOTE_LABELS[name]
        items.append(template.format(*value) if isinstance(value, tuple) else template.format(value))
    return '；'.join(items)


def _edge_cell(edge):
    if edge is None:
        return '<span class="muted">无</span>'
    return (f'{code(edge["child"])} → {code(edge["parent"])}'
            f'<span class="muted"> · {code(", ".join(edge["child_cols"]))} → '
            f'{code(", ".join(edge["parent_cols"]))}</span>')


def _target(entry):
    if entry['kind'] == 'capture':
        return (f'/workbench/captures/{segment(entry["id"])}',
                f'捕获 {code(entry["id"][:12])} · {text(entry["name"])} v{entry["version"]}')
    return (f'/workbench/recipes/{segment(entry["name"])}/{entry["version"]}',
            f'配方 {text(entry["name"])} v{entry["version"]}')


def _witness_links(entry):
    if entry['kind'] != 'capture' or not entry['witnesses']:
        return ''
    base = f'/workbench/captures/{segment(entry["id"])}'
    links, seen = [], set()
    for witness in entry['witnesses']:
        for table, key in (witness['origin'], witness['target']):
            token = (table, dumps(key))
            if token in seen:
                continue
            seen.add(token)
            links.append(
                f'<form class="inline" method="post" action="{base}/trace">'
                f'<input type="hidden" name="table" value="{text(table)}">'
                f'<input type="hidden" name="key" value="{text(token[1])}">'
                f'<button class="quiet">{code(table)} {typed(key)}</button></form>')
    return ''.join(links)


def _row(entry):
    href, label = _target(entry)
    badge = (f'<span class="status {STATUS_CLASS[entry["status"]]}">{STATUS_LABELS[entry["status"]]}</span>'
             if entry['status'] else '')
    detail = REASON_TEXT.get(entry['reason'], '')
    notes = _notes(entry['notes'])
    if notes:
        detail = f'{detail} {notes}' if detail else notes
    witness = f'{entry["witness_count"]} 条证据' if entry['kind'] == 'capture' else '—'
    return (f'<tr><th scope="row"><a href="{href}">{label}</a>'
            f'<div class="muted">{code(entry["digest"][:12])} · {SCHEMA_LABELS[entry["schema_state"]]}</div></th>'
            f'<td>{badge}<div class="muted">{detail}</div></td>'
            f'<td>{_edge_cell(entry["edge"])}</td><td>{witness}</td>'
            f'<td>{_witness_links(entry)}</td></tr>')


GROUP_NOTE = {
    'correspond': '同一子表、父表和列映射，历史记录确实用过。',
    'divergent': '外键号或子父表看似相同，但实际定义指向另一条约束。',
    'incomparable': '当时的模式缺少这张表或关系，或没有被任何捕获保留，无法对齐。',
}


def _group(result, status):
    entries = result['groups'][status]
    body = ''.join(_row(entry) for entry in entries)
    if entries:
        content = ('<div class="scroll"><table><thead><tr><th scope="col">记录</th><th scope="col">判定</th>'
                   '<th scope="col">当时的定义</th><th scope="col">直接证据</th>'
                   f'<th scope="col">回到封存证据</th></tr></thead><tbody>{body}</tbody></table></div>')
    else:
        content = '<p class="empty">没有记录落入这一类。</p>'
    return (f'<section aria-labelledby="group-{status}"><h2 id="group-{status}">'
            f'<span class="status {STATUS_CLASS[status]}">{STATUS_LABELS[status]}</span>'
            f'<span class="minor"> · {len(entries)} 项</span></h2>'
            f'<p class="muted">{GROUP_NOTE[status]}</p>{content}</section>')


def _table_row(entry):
    href, label = _target(entry)
    rows = '—' if entry['table_rows'] is None else f'{entry["table_rows"]} 行'
    if entry['status'] == 'incomparable':
        detail = f'<span class="muted">{REASON_TEXT.get(entry["reason"], "不可比")}</span>'
    elif entry['edges']:
        detail = '<br>'.join(_edge_cell(edge) for edge in entry['edges'])
    else:
        detail = '<span class="muted">直接作为根或被选中的行</span>'
    return (f'<tr><th scope="row"><a href="{href}">{label}</a>'
            f'<div class="muted">{code(entry["digest"][:12])} · {SCHEMA_LABELS[entry["schema_state"]]}</div></th>'
            f'<td>{rows}</td><td>{detail}</td></tr>')


def _edge_index(result):
    if not result['edge_index']:
        return '<p class="empty">此表没有外键关系。</p>'
    base = location('/workbench/lineage', source=result['alias'], table=result['table'])
    outgoing = [item for item in result['edge_index'] if item['direction'] == 'outgoing']
    incoming = [item for item in result['edge_index'] if item['direction'] == 'incoming']

    def rows(items):
        return ''.join(
            f'<tr><th scope="row">{_edge_cell(item["edge"])}</th><td>{item["count"]} 项历史记录直接用过</td>'
            f'<td><a class="button quiet" href="{text(base)}&amp;edge={item["edge"]["id"]}'
            f'&amp;child={text(item["edge"]["child"])}">查看此关系的交叉引用</a></td></tr>'
            for item in items)

    parts = []
    if outgoing:
        parts.append('<h3>本表作为子表</h3><div class="scroll"><table><thead><tr>'
                     '<th scope="col">关系</th><th scope="col">历史使用</th><th scope="col"></th>'
                     f'</tr></thead><tbody>{rows(outgoing)}</tbody></table></div>')
    if incoming:
        parts.append('<h3>本表作为父表</h3><div class="scroll"><table><thead><tr>'
                     '<th scope="col">关系</th><th scope="col">历史使用</th><th scope="col"></th>'
                     f'</tr></thead><tbody>{rows(incoming)}</tbody></table></div>')
    return ''.join(parts)


def _table_links(tables, alias, active):
    return ''.join(
        f'<a class="table-link" href="{text(location("/workbench/lineage", source=alias, table=name))}"'
        f'{" aria-current=page" if name == active else ""}>{code(name)}</a>'
        for name in tables
    )


def show(bench, query):
    alias = field(query, 'source', next(iter(sorted(bench.repository.sources)), ''))
    try:
        catalog = bench.catalog(alias)
    except Fault as exc:
        return exc.status, page('交叉引用', 'lineage', 'catalog_unavailable',
                                options=source_options(bench, alias), error=text(message(exc)))
    tables = catalog['schema']['tables']
    selected = field(query, 'table')
    if selected and selected not in tables:
        exc = Fault('unknown_identifier', 'Unknown table', 404)
        return exc.status, page('交叉引用', 'lineage', 'catalog_unavailable',
                                options=source_options(bench, alias), error=text(message(exc)))
    selected = selected or next(iter(tables))
    child = field(query, 'child') or selected
    raw = field(query, 'edge')
    try:
        number = None if not raw else int(raw)
    except ValueError:
        number = None
    if number is not None and not bench.has_edge(catalog, child, number):
        raise Fault('invalid_edge', 'Unknown key number in table', 404)
    result = bench.cross_reference(alias, selected, child, number)
    stats = result['stats']
    truncated = ''
    if stats['truncated']:
        examined, totals = stats['examined'], stats['totals']
        truncated = (f'<p class="notice info">只检查了最近的 {examined["captures"]} 次捕获与 '
                     f'{examined["recipes"]} 个配方版本；库中共有 {totals.get("captures", 0)} 次捕获与 '
                     f'{totals.get("recipes", 0)} 个版本，尚有 {stats["truncated"]} 项未检查。</p>')
    if result['edge'] is not None:
        groups = ''.join(_group(result, status) for status in STATUS_ORDER)
        extra = ''
        if result['table_only']:
            extra = ('<section><h2>同表但未用此关系<span class="minor"> · '
                     f'{len(result["table_only"])} 项</span></h2><div class="scroll"><table><thead><tr>'
                     '<th scope="col">记录</th><th scope="col">判定</th><th scope="col">当时的定义</th>'
                     '<th scope="col">直接证据</th><th scope="col">回到封存证据</th></tr></thead>'
                     f'<tbody>{"".join(_row(entry) for entry in result["table_only"])}</tbody>'
                     '</table></div></section>')
        body = (f'<dl class="facts"><div class="wide"><dt>检查的当前关系</dt>'
                f'<dd>{_edge_cell(result["edge"])} · 外键 {result["edge"]["id"]}'
                f'（子表 {code(result["edge"]["child"])}）</dd></div>'
                f'<div><dt>直接使用此关系的配方版本</dt><dd>{stats["recipes"]}</dd></div>'
                f'<div><dt>直接使用此关系的封存捕获</dt><dd>{stats["captures"]}</dd></div></dl>'
                f'{truncated}{groups}{extra}'
                f'<p><a href="{text(location("/workbench/lineage", source=alias, table=selected))}">'
                '返回此表的全部关系</a></p>')
    else:
        entries = ''.join(_table_row(entry) for entry in result['table_entries'])
        body = (f'<dl class="facts"><div><dt>相关配方版本</dt><dd>{stats["recipes"]}</dd></div>'
                f'<div><dt>相关封存捕获</dt><dd>{stats["captures"]}</dd></div></dl>'
                f'{truncated}<section><h2>当前外键关系</h2>{_edge_index(result)}</section>'
                '<section><h2>历史记录如何使用这张表</h2><div class="scroll"><table><thead><tr>'
                '<th scope="col">记录</th><th scope="col">入选行</th><th scope="col">关系</th>'
                f'</tr></thead><tbody>{entries or "<tr><td colspan=3 class=empty>暂无历史记录。</td></tr>"}'
                '</tbody></table></div></section>')
    heading = (f'{child} → {result["edge"]["parent"]}' if result['edge'] is not None
               else f'{selected} · 全部关系')
    return 200, page('交叉引用', 'lineage', 'lineage',
                     options=source_options(bench, alias),
                     table_links=_table_links(tables, alias, selected),
                     alias=text(alias), selected=text(selected), heading=text(heading),
                     catalog=text(location('/workbench', source=alias, table=selected)),
                     table_count=len(tables), digest=text(catalog['schema_digest']), body=body)


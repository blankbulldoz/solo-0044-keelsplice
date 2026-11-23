"""Turn live SQLite catalog evidence into the browsable relationship directory."""

import sqlite3

from ..codec import Fault, fingerprint
from . import tablelink
from .errors import message
from .forms import field
from .render import code, location, options, page, text

STATE_STYLE = {'match': 'ready', 'review': 'pending', 'unknown': 'muted'}


def source_options(bench, active):
    aliases = sorted(bench.repository.sources)
    return options(((alias, alias) for alias in aliases), active)


def table_links(tables, alias, active, tokens):
    return ''.join(
        f'<a class="table-link" href="{text(location("/workbench", source=alias, table=name, handoff=tokens[name]))}"'
        f'{" aria-current=page" if name == active else ""}>{code(name)}</a>'
        for name in tables
    )


def column_rows(table):
    rows = []
    for col in table['columns']:
        key_order = f'第 {col["pk"]} 列' if col['pk'] else '—'
        source = {2: '虚拟生成', 3: '存储生成'}.get(col['hidden'], '实存')
        nullable = '否' if col['notnull'] else '未显式限制'
        rows.append(f'<tr><th scope="row">{code(col["name"])}</th><td>{text(col["type"] or "未声明")}</td>'
                    f'<td>{key_order}</td><td>{nullable}</td><td>{source}</td></tr>')
    return ''.join(rows)


def edge_rows(edges, current):
    result = []
    for edge in edges:
        if current not in (edge['child'], edge['parent']):
            continue
        direction = '自引用' if edge['child'] == edge['parent'] else ('指向父表' if current == edge['child'] else '来自子表')
        child = ', '.join(edge['child_cols'])
        parent = ', '.join(edge['parent_cols'])
        result.append(f'<tr><td>{direction}</td><td>{code(edge["child"])} → {code(edge["parent"])}</td>'
                      f'<td>{code(child)} → {code(parent)}</td><td>{edge["id"]}</td></tr>')
    return ''.join(result) or '<tr><td colspan="4" class="empty">此表没有外键关系。</td></tr>'


def handoff_panel(verdict, alias):
    state = verdict['state']
    points = ''.join(f'<li>{text(point)}</li>' for point in verdict['points'])
    points = f'<ul class="data-list">{points}</ul>' if points else ''
    candidates = ''
    if verdict['candidates']:
        links = '、'.join(
            f'<a href="{text(location("/workbench", source=alias, table=name))}">{code(name)}</a>'
            for name in verdict['candidates'])
        candidates = f'<p>候选表：{links}</p>'
    envelope = verdict['envelope']
    evidence = ''
    if envelope is not None:
        record = envelope['basis']['table']
        indexes = ''.join(f'<li>{code(item["name"])}<pre>{text(item["sql"])}</pre></li>'
                          for item in envelope['basis']['indexes']) or '<li class="empty">无显式索引。</li>'
        evidence = (
            '<section class="handoff-basis" aria-label="原结构依据">'
            '<h3>原结构依据（交接时记录）</h3>'
            '<dl class="facts">'
            f'<div><dt>源别名</dt><dd>{code(envelope["source"])}</dd></div>'
            f'<div><dt>表</dt><dd>{code(envelope["table"])}</dd></div>'
            f'<div class="wide"><dt>交接时模式摘要</dt><dd>{code(envelope["schema_digest"])}</dd></div>'
            f'<div class="wide"><dt>表级依据摘要</dt><dd>{code(fingerprint(envelope["basis"]))}</dd></div>'
            '</dl>'
            '<h4>列定义</h4>'
            '<div class="scroll"><table><thead><tr><th scope="col">列</th><th scope="col">声明类型</th>'
            '<th scope="col">主键顺序</th><th scope="col">允许空值</th><th scope="col">值来源</th></tr></thead>'
            f'<tbody>{column_rows(record)}</tbody></table></div>'
            '<h4>外键关系</h4>'
            '<div class="scroll"><table><thead><tr><th scope="col">相对方向</th><th scope="col">子表 → 父表</th>'
            '<th scope="col">列映射</th><th scope="col">外键编号</th></tr></thead>'
            f'<tbody>{edge_rows(envelope["basis"]["edges"], envelope["table"])}</tbody></table></div>'
            f'<h4>索引</h4><ul class="indexes">{indexes}</ul>'
            f'<h4>建表语句</h4><pre>{text(record["ddl"])}</pre>'
            '</section>'
        )
    open_attr = '' if state == 'match' else ' open'
    return (f'<details class="handoff {state}"{open_attr}><summary>交接核对：'
            f'<span class="status {STATE_STYLE[state]}">{text(verdict["title"])}</span></summary>'
            f'<p>{text(verdict["detail"])}</p>{points}{candidates}{evidence}</details>')


def show(bench, query):
    alias = field(query, 'source', next(iter(sorted(bench.repository.sources)), ''))
    token = field(query, 'handoff')
    envelope, token_error = None, ''
    if token:
        try:
            envelope = tablelink.decode(token)
        except Fault as exc:
            token_error = message(exc)
    try:
        catalog = bench.catalog(alias)
        failure = None
    except Fault as exc:
        catalog, failure = None, (exc.status, message(exc))
    except sqlite3.Error:
        catalog, failure = None, (422, message(Fault('database_input', '')))
    if failure is not None:
        panel = handoff_panel(tablelink.assess(envelope, token_error, alias, field(query, 'table'), None, failure[1]),
                              alias) if token else ''
        return failure[0], page('关系目录', 'catalog', 'catalog_unavailable',
                                options=source_options(bench, alias), error=text(failure[1]), handoff=panel)
    tables = catalog['schema']['tables']
    selected = field(query, 'table')
    panel = handoff_panel(tablelink.assess(envelope, token_error, alias, selected, catalog), alias) if token else ''
    if selected and selected not in tables:
        exc = Fault('unknown_identifier', 'Unknown table', 404)
        return exc.status, page('关系目录', 'catalog', 'catalog_unavailable',
                                options=source_options(bench, alias), error=text(message(exc)), handoff=panel)
    selected = selected or next(iter(tables))
    tokens = {name: tablelink.token(catalog, alias, name) for name in tables}
    table = tables[selected]
    indexes = [item for item in catalog['schema']['indexes'] if item['tbl_name'] == selected]
    index_items = ''.join(f'<li>{code(item["name"])}<pre>{text(item["sql"])}</pre></li>' for item in indexes)
    return 200, page('关系目录', 'catalog', 'catalog',
                     options=source_options(bench, alias), table_links=table_links(tables, alias, selected, tokens),
                     alias=text(alias), selected=text(selected), digest=text(catalog['schema_digest']),
                     table_count=len(tables), column_count=len(table['columns']),
                     pk=code(' → '.join(table['pk'])), columns=column_rows(table),
                     edges=edge_rows(catalog['schema']['edges'], selected),
                     indexes=index_items or '<li class="empty">无显式索引。</li>',
                     recipe=text(location('/workbench/recipes/new', source=alias)),
                     handoff=panel,
                     handoff_link=text(location('/workbench', source=alias, table=selected,
                                                handoff=tokens[selected])),
                     ddl=text(table['ddl']))

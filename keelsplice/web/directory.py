"""Turn live SQLite catalog evidence into the browsable relationship directory."""

from ..codec import Fault
from . import relations
from .errors import message
from .forms import field
from .render import code, location, options, page, text


def source_options(bench, active):
    aliases = sorted(bench.repository.sources)
    return options(((alias, alias) for alias in aliases), active)


def table_links(tables, alias, active):
    return ''.join(
        f'<a class="table-link" href="{text(location("/workbench", source=alias, table=name))}"'
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


def edge_rows(edges, current, alias, focus, state):
    result = []
    siblings = {}
    for edge in edges:
        siblings.setdefault((edge['child'], edge['parent']), []).append(edge['id'])
    for edge in edges:
        if current not in (edge['child'], edge['parent']):
            continue
        direction = '自引用' if edge['child'] == edge['parent'] else ('指向父表' if current == edge['child'] else '来自子表')
        if len(siblings[(edge['child'], edge['parent'])]) > 1:
            direction += ' · 同父多边'
        child = ', '.join(edge['child_cols'])
        parent = ', '.join(edge['parent_cols'])
        marked = focus is not None and (edge['child'], edge['id']) == (focus['child'], focus['id'])
        standing = ''
        if marked:
            standing = ('<span class="status ready">正在考察</span> ' if state == 'current'
                        else '<span class="status muted">编号已改指其他列</span> ')
        result.append(f'<tr{" aria-current=true" if marked and state == "current" else ""}>'
                      f'<td>{direction}</td><td>{code(edge["child"])} → {code(edge["parent"])}</td>'
                      f'<td>{code(child)} → {code(parent)}</td><td>{edge["id"]}</td>'
                      f'<td>{standing}{relations.link(alias, edge, "查看列映射")}</td></tr>')
    return ''.join(result) or '<tr><td colspan="5" class="empty">此表没有外键关系。</td></tr>'


def focus_notice(schema, focus, alias):
    if focus is None:
        return ''
    edge, state = relations.locate(schema, focus)
    shown = edge if state == 'current' else focus
    href = text(location('/workbench/relations', edge=relations.token(relations.reference(alias, shown))))
    mapping = ' → '.join(shown['child_cols']) + ' ⇒ ' + ' → '.join(shown['parent_cols'])
    if state == 'current':
        return (f'<p class="notice info">正在考察外键 #{shown["id"]}：{code(shown["child"])} '
                f'的 {code(mapping)}。<a href="{href}">打开此关系的列映射与依据</a></p>')
    if state == 'changed':
        return (f'<p role="alert" class="notice">外键编号 #{shown["id"]} 现在指向另一组列，'
                f'原先考察的 {code(mapping)} 已不是当前映射。<a href="{href}">复查旧关系并重新选择</a></p>')
    return (f'<p role="alert" class="notice">外键编号 #{shown["id"]} 已不在当前目录中，'
            f'原先考察的 {code(mapping)} 无法继续。<a href="{href}">复查旧关系并重新选择</a></p>')


def show(bench, query):
    raw_edge = field(query, 'edge')
    focus = relations.parse(raw_edge) if raw_edge else None
    default_source = focus['source'] if focus else next(iter(sorted(bench.repository.sources)), '')
    alias = field(query, 'source', default_source)
    try:
        catalog = bench.catalog(alias)
    except Fault as exc:
        return exc.status, page('关系目录', 'catalog', 'catalog_unavailable',
                                options=source_options(bench, alias), error=text(message(exc)))
    tables = catalog['schema']['tables']
    selected = field(query, 'table')
    if selected and selected not in tables:
        exc = Fault('unknown_identifier', 'Unknown table', 404)
        return exc.status, page('关系目录', 'catalog', 'catalog_unavailable',
                                options=source_options(bench, alias), error=text(message(exc)))
    if focus is not None and focus['source'] != alias:
        raise Fault('invalid_edge_token', 'Relationship link belongs to another source', 400)
    selected = selected or (focus['child'] if focus and focus['child'] in tables else next(iter(tables)))
    table = tables[selected]
    indexes = [item for item in catalog['schema']['indexes'] if item['tbl_name'] == selected]
    index_items = ''.join(f'<li>{code(item["name"])}<pre>{text(item["sql"])}</pre></li>' for item in indexes)
    _, state = relations.locate(catalog['schema'], focus) if focus else (None, None)
    return 200, page('关系目录', 'catalog', 'catalog',
                     options=source_options(bench, alias), table_links=table_links(tables, alias, selected),
                     alias=text(alias), selected=text(selected), digest=text(catalog['schema_digest']),
                     table_count=len(tables), column_count=len(table['columns']),
                     pk=code(' → '.join(table['pk'])), columns=column_rows(table),
                     focus=focus_notice(catalog['schema'], focus, alias),
                     edges=edge_rows(catalog['schema']['edges'], selected, alias, focus, state),
                     indexes=index_items or '<li class="empty">无显式索引。</li>',
                     recipe=text(location('/workbench/recipes/new', source=alias)),
                     ddl=text(table['ddl']))

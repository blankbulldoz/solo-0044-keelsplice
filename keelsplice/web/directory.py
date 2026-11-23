"""Turn live SQLite catalog evidence into the browsable relationship directory."""

from ..codec import Fault
from .errors import message
from .forms import field
from .render import code, location, options, page, text


def source_options(bench, active):
    aliases = sorted(bench.repository.sources)
    return options(((alias, alias) for alias in aliases), active)


def table_links(tables, alias, active, path='/workbench'):
    return ''.join(
        f'<a class="table-link" href="{text(location(path, source=alias, table=name))}"'
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


def show(bench, query):
    alias = field(query, 'source', next(iter(sorted(bench.repository.sources)), ''))
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
    selected = selected or next(iter(tables))
    table = tables[selected]
    indexes = [item for item in catalog['schema']['indexes'] if item['tbl_name'] == selected]
    index_items = ''.join(f'<li>{code(item["name"])}<pre>{text(item["sql"])}</pre></li>' for item in indexes)
    return 200, page('关系目录', 'catalog', 'catalog',
                     options=source_options(bench, alias), table_links=table_links(tables, alias, selected),
                     alias=text(alias), selected=text(selected), digest=text(catalog['schema_digest']),
                     table_count=len(tables), column_count=len(table['columns']),
                     pk=code(' → '.join(table['pk'])), columns=column_rows(table),
                     edges=edge_rows(catalog['schema']['edges'], selected),
                     indexes=index_items or '<li class="empty">无显式索引。</li>',
                     recipe=text(location('/workbench/recipes/new', source=alias)),
                     keys=text(location('/workbench/keys', source=alias, table=selected)),
                     ddl=text(table['ddl']))

"""Turn live SQLite catalog evidence into the browsable relationship directory."""

from . import identity
from .errors import message
from .forms import field
from .render import location, options, page, text
from ..codec import Fault


def source_options(bench, active):
    aliases = sorted(bench.repository.sources)
    return options(((alias, alias) for alias in aliases), active)


def table_links(tables, alias, active):
    return ''.join(
        f'<a class="table-link" href="{text(location("/workbench", source=alias, table=name))}"'
        f'{" aria-current=page" if name == active else ""}>{identity.identifier(name)}</a>'
        for name in tables
    )


def column_rows(table):
    rows = []
    for col in table['columns']:
        key_order = f'第 {col["pk"]} 列' if col['pk'] else '—'
        source = {2: '虚拟生成', 3: '存储生成'}.get(col['hidden'], '实存')
        nullable = '否' if col['notnull'] else '未显式限制'
        rows.append(f'<tr><th scope="row">{identity.identifier(col["name"])}</th>'
                    f'<td>{text(col["type"] or "未声明")}</td>'
                    f'<td>{key_order}</td><td>{nullable}</td><td>{source}</td></tr>')
    return ''.join(rows)


def edge_rows(edges, current):
    rows = []
    for edge in edges:
        if current not in (edge['child'], edge['parent']):
            continue
        # The reference is read from the selected table towards its relative.
        direction = 'self' if edge['child'] == edge['parent'] else (
            'parent' if current == edge['child'] else 'child')
        rows.append(
            f'<tr><td>{identity.edge(edge["child"], edge["id"], direction)}</td>'
            f'<td>{identity.identifier(edge["parent"])}</td>'
            f'<td>{identity.identifiers(edge["child_cols"], "、")} → '
            f'{identity.identifiers(edge["parent_cols"], "、")}</td></tr>')
    return ''.join(rows) or '<tr><td colspan="3" class="empty">此表没有外键关系。</td></tr>'


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
    index_items = ''.join(f'<li>{identity.identifier(item["name"])}<pre>{text(item["sql"])}</pre></li>'
                          for item in indexes)
    return 200, page('关系目录', 'catalog', 'catalog',
                     options=source_options(bench, alias), table_links=table_links(tables, alias, selected),
                     alias=text(alias), selected=identity.identifier(selected),
                     digest=text(catalog['schema_digest']), legend=identity.legend(),
                     table_count=len(tables), column_count=len(table['columns']),
                     pk=identity.identifiers(table['pk'], ' → '), columns=column_rows(table),
                     edges=edge_rows(catalog['schema']['edges'], selected),
                     indexes=index_items or '<li class="empty">无显式索引。</li>',
                     recipe=text(location('/workbench/recipes/new', source=alias)),
                     ddl=text(table['ddl']))

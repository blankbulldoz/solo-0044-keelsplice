"""Render saved recipe versions and their allowed next operations."""

from .render import code, page, segment, text, typed


def index(bench):
    records = bench.repository.recent_recipes()
    rows = ''.join(
        f'<tr><th scope="row"><a href="/workbench/recipes/{segment(row["name"])}/{row["version"]}">'
        f'{text(row["name"])}</a></th><td>{row["version"]}</td><td>{code(row["source"])}</td>'
        f'<td>{status(row["active"])}</td><td>{code(row["schema_digest"][:12])}</td></tr>'
        for row in records
    )
    return 200, page('配方版本', 'recipe', 'recipes',
                     rows=rows or '<tr><td colspan="5" class="empty">尚未登记配方。</td></tr>')


def status(active):
    return '<span class="status ready">投用中</span>' if active else '<span class="status muted">已停用</span>'


def root_list(roots):
    return ''.join(f'<li>{code(root["table"])} {typed(root["key"])}</li>' for root in roots)


BASIS = {
    'registered': ('ready', '登记时保存', '此版本登记时写入了父表和两端列顺序。'),
    'catalog': ('ready', '按登记摘要确认', '旧版本只保存了子表和外键编号；当前目录摘要与登记时一致，按同编号边补全列映射。'),
    'schema_drift': ('muted', '无法确认', '旧版本只保存了子表和外键编号；当前目录摘要已变化，不能把现行编号当作登记时的边。'),
    'edge_missing': ('muted', '无法确认', '旧版本只保存了子表和外键编号；当前目录中已没有该编号的边。'),
    'source_unregistered': ('muted', '无法确认', '数据源别名当前未注册，无法读取目录补全列映射。'),
    'catalog_unavailable': ('muted', '无法确认', '数据源当前无法读取，无法补全登记时的列映射。'),
}


def relation_rows(relations):
    rows = []
    for item in relations:
        parent = code(item['parent']) if 'parent' in item else '<span class="muted">未确认</span>'
        if 'child_cols' in item:
            columns = code(', '.join(item['child_cols']) + ' → ' + ', '.join(item['parent_cols']))
        else:
            columns = '<span class="muted">登记时未保存</span>'
        style, label, note = BASIS[item['basis']]
        rows.append(
            f'<tr><th scope="row">{code(item["child"])}</th><td>{parent}</td><td>{columns}</td>'
            f'<td>{item["id"]}</td><td><span class="status {style}">{label}</span>'
            f'<p class="muted basis-note">{note}</p></td></tr>'
        )
    return ''.join(rows) or '<tr><td colspan="5" class="empty">未选择反向关系。</td></tr>'


def detail(bench, name, number):
    saved = bench.version_detail(name, number)
    recipe = saved['recipe']
    href = f'/workbench/recipes/{segment(name)}/{number}'
    actions = ''
    if saved['active']:
        actions = (
            f'<form method="post" action="{href}/preview"><button>试算闭包</button></form>'
            f'<form method="post" action="{href}/capture"><button class="primary">封存当前样本</button></form>'
            f'<form method="post" action="{href}/retire"><button class="quiet">停止投用</button></form>'
        )
    return 200, page('配方详情', 'recipe', 'recipe',
                     name=text(name), number=number, source=code(recipe['source']),
                     status=status(saved['active']), digest=text(saved['schema_digest']),
                     roots=root_list(recipe['roots']), edges=relation_rows(saved['relations']),
                     limit=recipe['row_limit'], actions=actions)

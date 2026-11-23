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


def detail(bench, name, number):
    saved = bench.version(name, number)
    recipe = saved['recipe']
    href = f'/workbench/recipes/{segment(name)}/{number}'
    actions = ''
    if saved['active']:
        actions = (
            f'<form method="post" action="{href}/preview"><button>试算闭包</button></form>'
            f'<form method="post" action="{href}/capture"><button class="primary">封存当前样本</button></form>'
            f'<form method="post" action="{href}/retire"><button class="quiet">停止投用</button></form>'
        )
    edges = ''.join(f'<li>{code(edge["child"])} · 外键 {edge["id"]}</li>'
                    for edge in recipe['follow_children'])
    return 200, page('配方详情', 'recipe', 'recipe',
                     name=text(name), number=number, source=code(recipe['source']),
                     status=status(saved['active']), digest=text(saved['schema_digest']),
                     roots=root_list(recipe['roots']), edges=edges or '<li class="empty">未选择反向关系。</li>',
                     limit=recipe['row_limit'], actions=actions)

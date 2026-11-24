"""Render saved recipe versions and their allowed next operations."""

from .render import code, page, segment, text, typed


def provenance(bench, *, generic='配方投用状态取自快照，不代表主实例当前状态。'):
    context = bench.context()
    if context['authoritative']:
        return ''
    return (f'<p class="notice info">备用实例视图：{text(generic)} 快照时间 {text(context["as_of"])}，'
            f'来源 <code>{text(context["snapshot_source_repository"])}</code>。</p>')


def index(bench):
    records = bench.repository.recent_recipes()
    live = not bench.read_only
    rows = ''.join(
        f'<tr><th scope="row"><a href="/workbench/recipes/{segment(row["name"])}/{row["version"]}">'
        f'{text(row["name"])}</a></th><td>{row["version"]}</td><td>{code(row["source"])}</td>'
        f'<td>{status(row["active"], live=live)}</td><td>{code(row["schema_digest"][:12])}</td></tr>'
        for row in records
    )
    return 200, page('配方版本', 'recipe', 'recipes', provenance=provenance(bench),
                     rows=rows or '<tr><td colspan="5" class="empty">尚未登记配方。</td></tr>')


def status(active, *, live=True):
    if not live:
        label = '快照时投用' if active else '快照时已停用'
        return f'<span class="status muted">{label}</span>'
    return '<span class="status ready">投用中</span>' if active else '<span class="status muted">已停用</span>'


def root_list(roots):
    return ''.join(f'<li>{code(root["table"])} {typed(root["key"])}</li>' for root in roots)


def detail(bench, name, number):
    saved = bench.version(name, number)
    recipe = saved['recipe']
    live = not bench.read_only
    href = f'/workbench/recipes/{segment(name)}/{number}'
    actions = ''
    if saved['active'] and live:
        actions = (
            f'<form method="post" action="{href}/preview"><button>试算闭包</button></form>'
            f'<form method="post" action="{href}/capture"><button class="primary">封存当前样本</button></form>'
            f'<form method="post" action="{href}/retire"><button class="quiet">停止投用</button></form>'
        )
    edges = ''.join(f'<li>{code(edge["child"])} · 外键 {edge["id"]}</li>'
                    for edge in recipe['follow_children'])
    return 200, page('配方详情', 'recipe', 'recipe',
                     name=text(name), number=number, source=code(recipe['source']),
                     status=status(saved['active'], live=live), digest=text(saved['schema_digest']),
                     provenance=provenance(bench),
                     roots=root_list(recipe['roots']), edges=edges or '<li class="empty">未选择反向关系。</li>',
                     limit=recipe['row_limit'], actions=actions)

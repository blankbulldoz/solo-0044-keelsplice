"""Render saved recipe versions and their allowed next operations."""

from .forms import field, version
from .render import code, page, segment, text, typed


def index(bench):
    records = bench.repository.recent_recipes()
    rows = ''.join(
        f'<tr><th scope="row"><a href="/workbench/recipes/{segment(row["name"])}/{row["version"]}">'
        f'{text(row["name"])}</a></th><td>{row["version"]}</td><td>{code(row["source"])}</td>'
        f'<td>{status(row["active"])}</td><td>{code(row["schema_digest"][:12])}</td>'
        f'<td><form class="inline" method="post" action="/workbench/recipes/{segment(row["name"])}/history">'
        f'<button class="quiet">历史切面</button></form></td></tr>'
        for row in records
    )
    return 200, page('配方版本', 'recipe', 'recipes',
                     rows=rows or '<tr><td colspan="6" class="empty">尚未登记配方。</td></tr>')


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
    actions += (f'<form method="post" action="/workbench/recipes/{segment(name)}/history">'
                f'<button>浏览同名版本历史</button></form>')
    edges = ''.join(f'<li>{code(edge["child"])} · 外键 {edge["id"]}</li>'
                    for edge in recipe['follow_children'])
    return 200, page('配方详情', 'recipe', 'recipe',
                     name=text(name), number=number, source=code(recipe['source']),
                     status=status(saved['active']), digest=text(saved['schema_digest']),
                     roots=root_list(recipe['roots']), edges=edges or '<li class="empty">未选择反向关系。</li>',
                     limit=recipe['row_limit'], actions=actions)


def open_history(bench, name):
    session_id = bench.open_history(name)
    return f'/workbench/recipes/{segment(name)}/history/{session_id}'


def history_row(name, entry):
    current = status(entry['current'])
    if entry['recorded'] != entry['current']:
        current += ' <span class="status pending">资格已变化</span>'
    return (f'<tr><th scope="row"><a href="/workbench/recipes/{segment(name)}/{entry["version"]}">'
            f'版本 {entry["version"]}</a></th><td>{code(entry["source"])}</td>'
            f'<td>{status(entry["recorded"])}</td><td>{current}</td>'
            f'<td>{code(entry["schema_digest"][:12])}</td></tr>')


def history(bench, name, session_id, query):
    before = field(query, 'before')
    cursor = version(before) if before else None
    view = bench.history(name, session_id, before=cursor)
    base = f'/workbench/recipes/{segment(name)}/history/{segment(session_id)}'
    handoff = base if cursor is None else f'{base}?before={cursor}'
    entries = view['entries']
    rows = ''.join(history_row(name, entry) for entry in entries)
    if rows:
        caption = f'本页版本 {entries[-1]["version"]}–{entries[0]["version"]} · 切面共收录 {view["total"]} 个版本'
    else:
        rows = '<tr><td colspan="5" class="empty">此位置没有更早的版本。</td></tr>'
        caption = '本页没有条目'
    newer = view['current_high'] - view['high_water']
    newer_note = f'切面开启后又有 {newer} 个新版本登记，不属于本切面。' if newer else ''
    pagination = []
    if cursor is not None:
        pagination.append(f'<a class="button" href="{base}">回到切面首页</a>')
    if view['next_before'] is not None:
        pagination.append(f'<a class="button" href="{base}?before={view["next_before"]}">更早版本 →</a>')
    return 200, page('同名版本历史', 'recipe', 'recipe_history',
                     name=text(name), opened=text(view['opened']),
                     high_water=view['high_water'], current_high=view['current_high'],
                     handoff=text(handoff), newer_note=text(newer_note),
                     caption=text(caption), rows=rows, pagination=''.join(pagination))

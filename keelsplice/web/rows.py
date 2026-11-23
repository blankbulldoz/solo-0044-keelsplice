"""Page through sealed rows and open full values, all read from the capture."""

import base64

from .forms import field
from .render import code, location, options, page, segment, text, typed
from ..codec import Fault, digest, dumps

BLOB_PREVIEW_BYTES = 24
TEXT_PREVIEW_CHARS = 120

CLASS_LABEL = {'null': 'NULL', 'integer': '整数', 'real': '实数', 'text': '文本', 'blob': 'BLOB'}
SOURCE_LABEL = {0: '实存', 2: '虚拟生成', 3: '存储生成'}


def storage_class(value):
    if value is None:
        return 'null'
    if type(value) is int:
        return 'integer'
    if type(value) is str:
        return 'text'
    if isinstance(value, dict) and set(value) == {'blob'}:
        return 'blob'
    return 'real'


def value_size(value):
    kind = storage_class(value)
    if kind == 'blob':
        return len(base64.b64decode(value['blob'], validate=True)), '字节'
    if kind == 'text':
        return len(value), '字符'
    return None, ''


def value_digest(value):
    return digest(dumps(value).encode('ascii'))


def real_hex(value):
    return float(value['real']).hex()


def full_value(value):
    kind = storage_class(value)
    if kind == 'null':
        return '<span class="empty">NULL（无值）</span>'
    if kind == 'blob':
        data = base64.b64decode(value['blob'], validate=True)
        return f'<pre class="value-full">{text(data.hex())}</pre>'
    if kind == 'real':
        return (f'<pre class="value-full">{text(value["real"])}</pre>'
                f'<p class="muted">十六进制：{code(real_hex(value))}</p>')
    return f'<pre class="value-full">{text(value)}</pre>'


def badge(value):
    kind = storage_class(value)
    return f'<span class="type-badge {kind}" title="SQLite 存储类型">{CLASS_LABEL[kind]}</span>'


def preview(value):
    """Return (html, truncated); truncated previews never pretend to be whole."""
    kind = storage_class(value)
    if kind == 'null':
        return '<span class="empty">NULL</span>', False
    if kind == 'blob':
        data = base64.b64decode(value['blob'], validate=True)
        if len(data) <= BLOB_PREVIEW_BYTES:
            return f'<code>{text(data.hex())}</code>', False
        head = data[:BLOB_PREVIEW_BYTES].hex()
        return (f'<code>{text(head)}</code>'
                f'<span class="truncated" title="仅显示前 {BLOB_PREVIEW_BYTES} 字节，完整值需展开查看">'
                '…已截短</span>'), True
    if kind == 'real':
        return f'<code>{text(value["real"])}</code>', False
    if kind == 'integer':
        return f'<code>{text(value)}</code>', False
    if len(value) <= TEXT_PREVIEW_CHARS:
        return f'<code>{text(value)}</code>', False
    head = value[:TEXT_PREVIEW_CHARS]
    return (f'<code>{text(head)}</code>'
            f'<span class="truncated" title="仅显示前 {TEXT_PREVIEW_CHARS} 个字符，完整值需展开查看">'
            '…已截短</span>'), True


def cell_html(base, table_name, column_name, value):
    body, truncated = preview(value)
    head = f'{badge(value)} {body}'
    if truncated:
        href = location(base + '/value', table=table_name, column=column_name)
        head += f' <a class="expand" href="{text(href)}">展开原值</a>'
    return f'<td>{head}</td>'


def column_head(column):
    source = SOURCE_LABEL.get(column['hidden'], '实存')
    marker = ' class="generated"' if column['hidden'] else ''
    return (f'<th scope="col"{marker}><span class="col-name">{text(column["name"])}</span>'
            f'<span class="col-meta">{text(source)} · {text(column["type"] or "未声明")}</span></th>')


def table_picker(base, counts, active):
    choices = options(((name, f'{name}（{count} 行）') for name, count in counts), active)
    return ('<form class="table-picker" method="get" action="' + base + '/rows">'
            '<label>切换表<select name="table">' + choices + '</select></label>'
            '<input type="hidden" name="page" value="1"><button>浏览入选行</button></form>')


def pager(base, table_name, view):
    page, pages = view['page'], view['pages']

    def link(target, label, enabled):
        if not enabled:
            return f'<span class="page-step muted">{label}</span>'
        href = location(base + '/rows', table=table_name, page=target)
        return f'<a class="page-step" href="{text(href)}">{label}</a>'

    return (f'<nav class="pager" aria-label="分页">'
            f'{link(1, "首页", page > 1)}{link(page - 1, "上一页", page > 1)}'
            f'<span class="page-current">第 {page} / {pages} 页</span>'
            f'{link(page + 1, "下一页", page < pages)}{link(pages, "末页", page < pages)}</nav>')


def show_rows(bench, capture_id, query):
    base = f'/workbench/captures/{segment(capture_id)}'
    record = bench.status(capture_id)
    table_name = field(query, 'table')
    raw_page = field(query, 'page', '1')
    if not raw_page.isascii() or not raw_page.isdecimal():
        raise Fault('invalid_page', 'Page numbers start at 1', 400)
    view = bench.browse(capture_id, table_name, int(raw_page))
    name = view['table']
    counts = record['summary']['counts']
    head = ''.join(column_head(col) for col in view['columns'])
    body_rows = []
    for row_number, (token_key, values) in enumerate(
            view['rows'], start=(view['page'] - 1) * view['page_size'] + 1):
        cells = ''.join(cell_html(base, name, view['columns'][i]['name'], value)
                        for i, value in enumerate(values))
        trace = ('<td><form method="post" action="' + base + '/trace">'
                 '<input type="hidden" name="table" value="' + text(name) + '">'
                 '<input type="hidden" name="key" value="' + text(dumps(token_key)) + '">'
                 '<button class="quiet">继续追踪</button></form></td>')
        body_rows.append(f'<tr><th scope="row" class="row-index">{row_number}</th>{cells}{trace}</tr>')
    empty = '<tr><td colspan="{}" class="empty">该表在本次捕获中没有入选行；表结构仍随子库交付。</td></tr>'.format(
        len(view['columns']) + 2)
    return 200, page('入选行浏览', 'capture', 'rows', base=base, id=text(capture_id),
                     table=code(name), total=view['total'], page_size=view['page_size'],
                     digest=text(record['schema_digest']),
                     picker=table_picker(base, counts.items(), name),
                     columns=head, pager=pager(base, name, view),
                     rows=''.join(body_rows) or empty,
                     empty_notice='<p class="notice info">该表只有 DDL、没有入选行。</p>'
                     if view['total'] == 0 else '')


def show_value(bench, capture_id, query):
    base = f'/workspace/captures/{segment(capture_id)}'
    record = bench.status(capture_id)
    view = bench.cell(capture_id, field(query, 'table'), field(query, 'column'))
    name, column = view['table'], view['column']
    entries = []
    for token_key, value in view['rows']:
        size, unit = value_size(value)
        size_text = f'{size} {unit}' if size is not None else '—'
        entries.append(
            '<tr><th scope="row">' + typed(token_key) + '</th><td>' + badge(value) + '</td>'
            '<td>' + text(size_text) + '</td><td><code>' + text(value_digest(value)) + '</code></td>'
            '<td>' + full_value(value) + '</td>'
            '<td><form method="post" action="' + base + '/trace">'
            '<input type="hidden" name="table" value="' + text(name) + '">'
            '<input type="hidden" name="key" value="' + text(dumps(token_key)) + '">'
            '<button class="quiet">继续追踪</button></form></td></tr>'
        )
    empty = '<tr><td colspan="6" class="empty">该列所属表在本次捕获中没有入选行。</td></tr>'
    source = SOURCE_LABEL.get(column['hidden'], '实存')
    back = location(base + '/rows', table=name)
    return 200, page('封存值详情', 'capture', 'value', base=base, id=text(capture_id),
                     table=code(name), column=code(column['name']), source=text(source),
                     declared=text(column['type'] or '未声明'), digest=text(record['schema_digest']),
                     back=text(back), entries=''.join(entries) or empty)

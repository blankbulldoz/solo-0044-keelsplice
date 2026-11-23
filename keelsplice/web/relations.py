"""Identify one declared foreign key and show its concrete column mapping."""

import base64
import binascii

from .errors import message
from .forms import field
from .render import code, location, options, page, text
from ..codec import Fault, dumps, loads

MAX_TOKEN = 4096
MAX_NAME = 200
MAX_COLUMNS = 2000


def mapping(edge):
    """Return the parts of an edge that give it meaning, independent of its number."""
    return {'child': edge['child'], 'parent': edge['parent'],
            'child_cols': list(edge['child_cols']), 'parent_cols': list(edge['parent_cols'])}


def reference(alias, edge):
    return {'source': alias, 'id': edge['id']} | mapping(edge)


def token(ref):
    raw = dumps(ref).encode('utf-8')
    return base64.urlsafe_b64encode(raw).decode('ascii').rstrip('=')


def _invalid():
    return Fault('invalid_edge_token', 'Relationship link is invalid or belongs to another source', 400)


def parse(value):
    """Decode a relationship token and reject anything that is not its canonical form."""
    if not isinstance(value, str) or not value or len(value) > MAX_TOKEN:
        raise _invalid()
    try:
        padded = value + '=' * (-len(value) % 4)
        raw = base64.b64decode(padded.encode('ascii'), altchars=b'-_', validate=True)
        ref = loads(raw.decode('utf-8'))
    except (binascii.Error, UnicodeError, ValueError, RecursionError) as exc:
        raise _invalid() from exc
    expected = {'source', 'child', 'id', 'parent', 'child_cols', 'parent_cols'}
    if not isinstance(ref, dict) or set(ref) != expected:
        raise _invalid()
    for name in ('source', 'child', 'parent'):
        if not isinstance(ref[name], str) or not ref[name] or len(ref[name]) > MAX_NAME:
            raise _invalid()
    if type(ref['id']) is not int or not 0 <= ref['id'] < 2**31:
        raise _invalid()
    for name in ('child_cols', 'parent_cols'):
        columns = ref[name]
        if not isinstance(columns, list) or not 1 <= len(columns) <= MAX_COLUMNS:
            raise _invalid()
        if any(not isinstance(column, str) or not column or len(column) > MAX_NAME for column in columns):
            raise _invalid()
    if len(ref['child_cols']) != len(ref['parent_cols']) or token(ref) != value:
        raise _invalid()
    return ref


def locate(schema, ref):
    """Find the live edge with the stored number and report whether its columns still agree."""
    edge = next((item for item in schema['edges']
                 if item['child'] == ref['child'] and item['id'] == ref['id']), None)
    if edge is None:
        return None, 'missing'
    return edge, 'current' if mapping(edge) == mapping(ref) else 'changed'


def relocated(schema, ref):
    """Find the same column mapping under another number, if the source still declares it."""
    return next((item for item in schema['edges']
                 if item['id'] != ref['id'] and mapping(item) == mapping(ref)), None)


def source_options(bench, active):
    aliases = sorted(bench.repository.sources)
    return options(((alias, alias) for alias in aliases), active)


def nullable_columns(table):
    return {column['name']: not column['notnull'] for column in table['columns']} if table else {}


def mapping_rows(child_cols, parent_cols, nullable):
    rows = []
    for number, (child, parent) in enumerate(zip(child_cols, parent_cols), 1):
        if child in nullable:
            allowance = '允许空值' if nullable[child] else '必填'
        else:
            allowance = '当前子表无此列'
        rows.append(f'<tr><th scope="row">{number}</th><td>{code(child)}</td>'
                    f'<td>{allowance}</td><td>{code(parent)}</td></tr>')
    return ''.join(rows)


def mapping_table(edge, tables):
    child = tables.get(edge['child'])
    body = mapping_rows(edge['child_cols'], edge['parent_cols'], nullable_columns(child))
    return (f'<div class="scroll"><table><thead><tr><th scope="col">映射序号</th>'
            f'<th scope="col">子表列</th><th scope="col">子列可空性</th>'
            f'<th scope="col">父表列</th></tr></thead><tbody>{body}</tbody></table></div>')


def any_nullable(edge, tables):
    nullable = nullable_columns(tables.get(edge['child']))
    return any(nullable.get(column, False) for column in edge['child_cols'])


def nullable_note(edge, tables):
    if not any_nullable(edge, tables):
        return ''
    return ('<p class="notice info">此关系的子键含有可空列：某行只要任一分量为 NULL，'
            '就不参与这条关系——正向不会要求父行，反向也不会因它被纳入。</p>')


def link(alias, edge, label):
    href = location('/workbench/relations', edge=token(reference(alias, edge)))
    return f'<a href="{text(href)}">{label}</a>'


def directory_link(alias, table=None, ref=None):
    query = {'source': alias}
    if table:
        query['table'] = table
    if ref is not None:
        query['edge'] = token(ref)
    return location('/workbench', **query)


def siblings(schema, alias, child, current_id, state, tables, ref=None):
    rows = []
    for edge in schema['edges']:
        if edge['child'] != child:
            continue
        if edge['id'] == current_id:
            standing = ('<span class="status ready">正在考察</span>' if state == 'current'
                        else '<span class="status muted">编号已改为另一组列</span>')
        elif ref is not None and mapping(edge) == mapping(ref):
            standing = '<span class="status ready">与原列映射相同，可直接改用</span>'
        elif state != 'current':
            standing = '<span class="status muted">可选作重新选择</span>'
        elif edge['parent'] == child:
            standing = '<span class="status muted">自引用</span>'
        else:
            standing = '<span class="status muted">另一条关系</span>'
        label = f'{code(", ".join(edge["child_cols"]))} → {code(", ".join(edge["parent_cols"]))}'
        rows.append(f'<tr><th scope="row">#{edge["id"]}</th><td>{code(edge["parent"])}</td>'
                    f'<td>{link(alias, edge, label)}</td><td>{standing}</td></tr>')
    if not rows:
        if child in tables:
            return '<p class="empty">子表当前没有声明外键，无法在原编号上重新选择。</p>'
        return '<p class="empty">子表已不在当前目录中，没有可重新选择的关系。</p>'
    return (f'<div class="scroll"><table><thead><tr><th scope="col">外键编号</th>'
            f'<th scope="col">父表</th><th scope="col">列映射</th><th scope="col">状态</th>'
            f'</tr></thead><tbody>{"".join(rows)}</tbody></table></div>')


def table_link(alias, table, edge_token, label):
    href = location('/workbench', source=alias, table=table, edge=edge_token)
    return f'<a class="button" href="{text(href)}">{label}</a>'


def notice(shown, state):
    if state == 'changed':
        return (f'<p role="alert" class="notice">外键编号 #{shown["id"]} 在当前目录中已指向另一组列。'
                '原先考察的关系仍保留在下方供复查；工作台不会把新列当作旧关系继续使用，请重新选择。</p>')
    if state == 'missing':
        return (f'<p role="alert" class="notice">外键编号 #{shown["id"]} 已不在当前目录中。'
                '原先考察的关系仍保留在下方供复查，请从同一子表现有的外键中重新选择。</p>')
    return ''

FORWARD = (
    '<p>开始试算或封存时，读到子表行的工作台总会沿声明的外键补齐父行，并按目录列映射与 SQLite '
    '比较语义（含 NOCASE 与类型亲和性）查找。复合子键任一分量为 NULL 时不要求父行；'
    '否则必须恰好找到一行父行，找不到或出现多行都会让整次选样以「无效父引用」失败。</p>'
    '<p>入选证据把这类行记为方向 <code>parent</code>：来源是子行、目标是父行，标签为'
    '「必需父关系」。它证明该父行是因为某个已入选子行的声明外键而被强制补齐。</p>'
)

REVERSE = (
    '<p>只有配方在「允许反向纳入的外键」中明确勾选此关系后，遍历到父行时才会沿该边找出'
    '匹配的子行并加入闭包。任一分量为 NULL 的复合子键不会被等值连接匹配，因此不会仅凭'
    '此边被纳入。</p>'
    '<p>入选证据把这类行记为方向 <code>child</code>：来源是父行、目标是子行，标签为'
    '「配方反向纳入」。它证明该子行来自配方的反向扩展，而不是自己的根或必需父关系；'
    '同一类型化主键只展开一次，环不会重复增长。</p>'
)

LINK_NOTE = (
    '<p class="muted">关系链接由数据源、外键编号和列映射共同识别，不绑定模式摘要或索引；'
    '源库只是新增无关索引时仍可继续查看。若同一编号改作另一组列，旧关系会保留供复查，'
    '并提示重新选择。</p>'
)


def show(bench, query):
    ref = parse(field(query, 'edge'))
    alias = ref['source']
    try:
        catalog = bench.catalog(alias)
    except Fault as exc:
        return exc.status, page('关系列映射', 'catalog', 'catalog_unavailable',
                                options=source_options(bench, alias), error=text(message(exc)))
    schema = catalog['schema']
    tables = schema['tables']
    edge, state = locate(schema, ref)
    shown = edge if state == 'current' else ref
    old_mapping = ''
    current_now = ''
    mapping_section = ''
    if state == 'current':
        mapping_section = ('<section aria-labelledby="mapping"><h2 id="mapping">列映射</h2>'
                           '<p class="muted">按外键声明的顺序逐项列出，序号相同的一对列构成一个等值条件。</p>'
                           + mapping_table(edge, tables) + nullable_note(edge, tables) + '</section>')
    else:
        old_mapping = ('<section aria-labelledby="old"><h2 id="old">原先考察的关系（链接中保存的列映射）</h2>'
                       '<p class="muted">这是打开链接时记录的关系，不会随源库改写；它只供复查，'
                       '当前目录并不保证仍声明这组列。</p>'
                       + mapping_table(ref, tables) + '</section>')
        if edge is not None:
            current_now = (f'<section aria-labelledby="now"><h2 id="now">当前编号 #{edge["id"]} 的列映射</h2>'
                           '<p class="muted">这组列与链接保存的关系不同，工作台不会把它当作原先考察的关系。</p>'
                           + mapping_table(edge, tables) + nullable_note(edge, tables) + '</section>')
    directory = (directory_link(alias, ref['child'], ref) if ref['child'] in tables
                 else directory_link(alias, None, ref))
    reselect = ''
    if state != 'current':
        moved = relocated(schema, ref)
        options_ = []
        if moved is not None:
            options_.append(link(alias, moved, f'改用 #{moved["id"]} 的相同列映射'))
        options_.append(f'<a class="button" href="{text(directory)}">返回子表并重新选择</a>')
        reselect = f'<div class="actions">{"".join(options_)}</div>'
    relation_token = token(ref)
    links = []
    if shown['child'] in tables:
        links.append(table_link(alias, shown['child'], relation_token, '查看子表 ' + shown['child']))
    if shown['parent'] in tables:
        label = '查看本表 ' if shown['parent'] == shown['child'] else '查看父表 '
        links.append(table_link(alias, shown['parent'], relation_token, label + shown['parent']))
    navigation = f'<div class="actions">{"".join(links)}</div>' if links else ''
    return 200, page('关系列映射', 'catalog', 'relation',
                     alias=text(alias), heading=f'{code(shown["child"])} → {code(shown["parent"])}',
                     edge_id=shown['id'], notice=notice(shown, state),
                     old_mapping=old_mapping, current_now=current_now,
                     mapping_section=mapping_section,
                     reselect=reselect, navigation=navigation,
                     forward=FORWARD, reverse=REVERSE, link_note=LINK_NOTE,
                     siblings=siblings(schema, alias, shown['child'], shown['id'], state, tables,
                                       ref if state != 'current' else None),
                     directory=text(directory), digest=text(catalog['schema_digest']))

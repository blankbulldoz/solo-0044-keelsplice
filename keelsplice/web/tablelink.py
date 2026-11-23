"""Build and assess self-contained handover links for catalog tables."""

import base64
import re

from ..codec import Fault, dumps, fingerprint, loads

VERSION = 1
MAX_TOKEN = 32_768
TOKEN_SHAPE = re.compile(r'[A-Za-z0-9_-]+\Z')

COLUMN_KEYS = {'cid', 'name', 'type', 'notnull', 'dflt_value', 'pk', 'hidden'}
EDGE_KEYS = {'child', 'id', 'parent', 'child_cols', 'parent_cols', 'on_update', 'on_delete'}
INDEX_KEYS = {'name', 'tbl_name', 'sql'}


def basis(catalog, table):
    schema = catalog['schema']
    return {
        'table': schema['tables'][table],
        'edges': [edge for edge in schema['edges'] if table in (edge['child'], edge['parent'])],
        'indexes': [item for item in schema['indexes'] if item['tbl_name'] == table],
    }


def token(catalog, alias, table):
    envelope = {'v': VERSION, 'source': alias, 'table': table,
                'schema_digest': catalog['schema_digest'], 'basis': basis(catalog, table)}
    return base64.urlsafe_b64encode(dumps(envelope).encode('ascii')).decode('ascii').rstrip('=')


def decode(token):
    if len(token) > MAX_TOKEN or not TOKEN_SHAPE.fullmatch(token):
        raise Fault('handoff_link', 'Handoff token is malformed', 400)
    try:
        padded = token + '=' * (-len(token) % 4)
        envelope = loads(base64.b64decode(padded, altchars=b'-_', validate=True).decode('ascii'))
    except (ValueError, UnicodeError, RecursionError):
        raise Fault('handoff_link', 'Handoff token does not decode', 400) from None
    _check(envelope)
    return envelope


def _check(envelope):
    if not isinstance(envelope, dict) or set(envelope) != {'v', 'source', 'table', 'schema_digest', 'basis'}:
        _fail()
    if envelope['v'] != VERSION or not all(
            isinstance(envelope[key], str) and envelope[key] for key in ('source', 'table', 'schema_digest')):
        _fail()
    record = envelope['basis']
    if not isinstance(record, dict) or set(record) != {'table', 'edges', 'indexes'}:
        _fail()
    table = record['table']
    if not isinstance(table, dict) or set(table) != {'ddl', 'columns', 'pk', 'without_rowid', 'strict'}:
        _fail()
    if not isinstance(table['ddl'], str) or not isinstance(table['without_rowid'], bool) \
            or not isinstance(table['strict'], bool) or not _str_list(table['pk']):
        _fail()
    if not all(_column_ok(column) for column in _list(table['columns'])):
        _fail()
    if not all(_edge_ok(edge) for edge in _list(record['edges'])):
        _fail()
    if not all(_index_ok(item) for item in _list(record['indexes'])):
        _fail()


def _fail():
    raise Fault('handoff_link', 'Handoff token content is not a table basis', 400)


def _list(value):
    return value if isinstance(value, list) else _fail()


def _str_list(value):
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def _column_ok(column):
    return isinstance(column, dict) and set(column) == COLUMN_KEYS \
        and isinstance(column['cid'], int) and isinstance(column['name'], str) \
        and isinstance(column['type'], str) and isinstance(column['notnull'], int) \
        and (column['dflt_value'] is None or isinstance(column['dflt_value'], str)) \
        and isinstance(column['pk'], int) and isinstance(column['hidden'], int)


def _edge_ok(edge):
    return isinstance(edge, dict) and set(edge) == EDGE_KEYS \
        and isinstance(edge['child'], str) and isinstance(edge['parent'], str) \
        and isinstance(edge['id'], int) and _str_list(edge['child_cols']) and _str_list(edge['parent_cols']) \
        and isinstance(edge['on_update'], str) and isinstance(edge['on_delete'], str)


def _index_ok(item):
    return isinstance(item, dict) and set(item) == INDEX_KEYS \
        and all(isinstance(item[key], str) for key in INDEX_KEYS)


def assess(envelope, token_error, alias, table, catalog, catalog_error=''):
    if token_error:
        return dict(state='unknown', title='无法确定', envelope=None, points=[], candidates=[],
                    detail=f'交接依据无法解析（{token_error}），链接可能已损坏或被截断，请与交班人重新索取。')
    if envelope['source'] != alias or envelope['table'] != table:
        return dict(state='unknown', title='无法确定', envelope=envelope, points=[], candidates=[],
                    detail='链接地址与交接依据记录的源或表不一致，未继续核对，以免走错源或表。')
    if catalog is None:
        return dict(state='unknown', title='无法确定', envelope=envelope, points=[], candidates=[],
                    detail=f'当前源不可读（{catalog_error}），无法核对；保留交接依据供人工对照。')
    if table not in catalog['schema']['tables']:
        candidates = _rename_candidates(envelope, catalog)
        points = [f'当前源中没有表 {table}。']
        if candidates:
            points.append('下列表的列与外键和交接依据一致，可能是同一表被重命名，需接手人确认。')
        else:
            points.append('当前源中也没有结构相同的表，需接手人确认表是否已移除。')
        return dict(state='review', title='需要接手人确认', envelope=envelope, points=points,
                    candidates=candidates, detail='交接依据中的表在当前源里找不到同名表。')
    current = basis(catalog, table)
    if fingerprint(current) == fingerprint(envelope['basis']):
        note = '整个目录与交接时一致。' if catalog['schema_digest'] == envelope['schema_digest'] \
            else '目录中其他表有变化，此表本身与交接依据一致。'
        return dict(state='match', title='可沿用原确认', envelope=envelope, points=[], candidates=[],
                    detail=f'当前表结构与交接依据一致。{note}')
    return dict(state='review', title='需要接手人确认', envelope=envelope, candidates=[],
                points=_differences(envelope['basis'], current),
                detail='当前表结构与交接依据不一致，以下各点需接手人确认。')


def _differences(recorded, current):
    points = []
    old, new = recorded['table'], current['table']
    old_cols = {column['name']: column for column in old['columns']}
    new_cols = {column['name']: column for column in new['columns']}
    for column in old['columns']:
        if column['name'] not in new_cols:
            points.append(f'列 {column["name"]} 在交接依据中存在，当前源中缺少。')
    for column in new['columns']:
        if column['name'] not in old_cols:
            points.append(f'列 {column["name"]} 为当前源新增。')
    for name, before in old_cols.items():
        after = new_cols.get(name)
        if after is None:
            continue
        if before['type'] != after['type']:
            points.append(f'列 {name} 声明类型由 {before["type"] or "未声明"} 变为 {after["type"] or "未声明"}。')
        if before['notnull'] != after['notnull']:
            points.append(f'列 {name} 的空值约束有变化。')
        if before['pk'] != after['pk']:
            points.append(f'列 {name} 的主键位置由 {_pk_slot(before["pk"])} 变为 {_pk_slot(after["pk"])}。')
        if before['hidden'] != after['hidden']:
            points.append(f'列 {name} 的值来源有变化（实存 / 虚拟生成 / 存储生成）。')
        if before['dflt_value'] != after['dflt_value']:
            points.append(f'列 {name} 默认值由 {before["dflt_value"]} 变为 {after["dflt_value"]}。')
    if old['without_rowid'] != new['without_rowid'] or old['strict'] != new['strict']:
        points.append('表存储格式（WITHOUT ROWID / STRICT）有变化。')
    old_edges = {dumps(edge): edge for edge in recorded['edges']}
    new_edges = {dumps(edge): edge for edge in current['edges']}
    for key, edge in old_edges.items():
        if key not in new_edges:
            points.append(f'外键缺失：{_edge_label(edge)}。')
    for key, edge in new_edges.items():
        if key not in old_edges:
            points.append(f'外键新增：{_edge_label(edge)}。')
    old_idx = {item['name']: item for item in recorded['indexes']}
    new_idx = {item['name']: item for item in current['indexes']}
    for name, item in old_idx.items():
        if name not in new_idx:
            points.append(f'索引 {name} 在交接依据中存在，当前源中缺少。')
        elif item['sql'] != new_idx[name]['sql']:
            points.append(f'索引 {name} 的定义有变化。')
    for name in new_idx:
        if name not in old_idx:
            points.append(f'索引 {name} 为当前源新增。')
    if not points:
        if old['ddl'] != new['ddl']:
            points.append('建表语句文本有差异（如引号或约束写法），需人工对照。')
        else:
            points.append('结构摘要与交接依据不一致，但未定位到具体差异，需人工对照建表语句。')
    return points


def _pk_slot(order):
    return f'第 {order} 列' if order else '不在主键中'


def _edge_label(edge):
    child = ', '.join(edge['child_cols'])
    parent = ', '.join(edge['parent_cols'])
    return f"{edge['child']} → {edge['parent']}（{child} → {parent}，外键 {edge['id']}）"


def _edge_shapes(edges, table):
    child_side = sorted(
        (edge['parent'], tuple(edge['child_cols']), tuple(edge['parent_cols']),
         edge['on_update'], edge['on_delete']) for edge in edges if edge['child'] == table)
    parent_side = sorted(
        (edge['child'], tuple(edge['child_cols']), tuple(edge['parent_cols']),
         edge['on_update'], edge['on_delete']) for edge in edges
        if edge['parent'] == table and edge['child'] != table)
    return child_side, parent_side


def _rename_candidates(envelope, catalog):
    record = envelope['basis']['table']
    shapes = _edge_shapes(envelope['basis']['edges'], envelope['table'])
    return [name for name, current in catalog['schema']['tables'].items()
            if current['columns'] == record['columns'] and current['pk'] == record['pk']
            and current['without_rowid'] == record['without_rowid'] and current['strict'] == record['strict']
            and _edge_shapes(catalog['schema']['edges'], name) == shapes]

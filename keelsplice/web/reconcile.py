"""Recover a browser draft after the source catalog drifted underneath it.

The registration form carries a compact, self-consistent snapshot of the
catalog the operator was looking at.  When the live digest no longer matches,
the stale submission must never be silently reinterpreted against the new
catalog.  Instead this module presents every draft item next to the current
catalog and lets the operator explicitly confirm the items whose meaning is
still provable.  Items whose old meaning can no longer be determined —
including an edge that kept the same number but changed meaning — are shown as
undecidable and are never preselected.
"""

import re

from ..codec import Fault, loads
from .forms import field, typed_json
from .render import code, page, text

EVIDENCE_LIMIT = 128 * 1024
ROOTS_LIMIT = 256
EDGES_LIMIT = 256
DIGEST = re.compile(r'[0-9a-f]{64}\Z')

CONFIRMABLE = 'confirmable'
UNDECIDABLE = 'undecidable'


def catalog_evidence(snapshot):
    """Project a live catalog snapshot into the evidence embedded in a form.

    The digest is the live full-schema digest the form binds to, so the
    evidence can be cross-checked against the editor's hidden digest field.
    """
    schema = snapshot['schema']
    return {
        'digest': snapshot['schema_digest'],
        'tables': {name: {'pk': list(meta['pk'])} for name, meta in schema['tables'].items()},
        'edges': [
            {key: edge[key] for key in ('child', 'id', 'parent', 'child_cols', 'parent_cols')}
            for edge in schema['edges']
        ],
    }


def parse_evidence(raw):
    if not isinstance(raw, str) or not raw or len(raw) > EVIDENCE_LIMIT:
        raise Fault('browser_form', '缺少打开登记页时的目录证据，无法核对原草稿。', 400)
    try:
        evidence = loads(raw)
    except (ValueError, RecursionError) as exc:
        raise Fault('browser_form', '目录证据无法解析，请重新打开登记页。', 400) from exc
    if not isinstance(evidence, dict) or set(evidence) != {'digest', 'tables', 'edges'}:
        raise Fault('browser_form', '目录证据形状不正确，请重新打开登记页。', 400)
    tables, edges, digest = evidence['tables'], evidence['edges'], evidence['digest']
    if (not isinstance(digest, str) or not isinstance(tables, dict) or not isinstance(edges, list)
            or len(edges) > EDGES_LIMIT):
        raise Fault('browser_form', '目录证据形状不正确，请重新打开登记页。', 400)
    for name, meta in tables.items():
        if not isinstance(name, str) or not isinstance(meta, dict) or set(meta) != {'pk'}:
            raise Fault('browser_form', '目录证据中的表信息不正确。', 400)
        pk = meta['pk']
        if not isinstance(pk, list) or not pk or not all(isinstance(col, str) for col in pk):
            raise Fault('browser_form', '目录证据中的主键顺序不正确。', 400)
    for edge in edges:
        if not isinstance(edge, dict) or set(edge) != {'child', 'id', 'parent', 'child_cols', 'parent_cols'}:
            raise Fault('browser_form', '目录证据中的外键信息不正确。', 400)
        if (not isinstance(edge['child'], str) or not isinstance(edge['parent'], str)
                or type(edge['id']) is not int
                or not _string_list(edge['child_cols']) or not _string_list(edge['parent_cols'])
                or not edge['child_cols'] or len(edge['child_cols']) != len(edge['parent_cols'])):
            raise Fault('browser_form', '目录证据中的外键信息不正确。', 400)
    if not DIGEST.fullmatch(digest):
        raise Fault('browser_form', '目录证据中的模式摘要格式不正确，请重新打开登记页。', 400)
    return evidence


def _string_list(value):
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def _current_edge(schema, child, number):
    for edge in schema['edges']:
        if edge['child'] == child and edge['id'] == number:
            return edge
    return None


def _same_meaning(old, new):
    return (old['parent'] == new['parent'] and old['child_cols'] == new['child_cols']
            and old['parent_cols'] == new['parent_cols'])


def classify(evidence, values, current):
    """Pair every draft item with its status against the live catalog."""
    schema = current['schema']
    tables = schema['tables']
    roots = typed_json(field(values, 'roots'))
    if not isinstance(roots, list) or not 1 <= len(roots) <= ROOTS_LIMIT:
        raise Fault('invalid_roots', '根记录须为包含 1 至 256 项的 JSON 数组。')
    root_items = []
    for index, draft in enumerate(roots):
        if not isinstance(draft, dict) or set(draft) != {'table', 'key'} or not isinstance(draft['table'], str):
            root_items.append((index, draft, UNDECIDABLE, '草稿不是 {table, key} 形状，无法判定其含义。'))
            continue
        table = draft['table']
        if not any(name.lower() == table.lower() for name in evidence['tables']):
            root_items.append((index, draft, UNDECIDABLE, '该表不在打开登记页时的目录证据中。'))
            continue
        if table not in tables:
            root_items.append((index, draft, UNDECIDABLE,
                               f'表 {table} 已从当前目录消失，旧编号含义无法判定。'))
            continue
        key = draft['key']
        width = len(tables[table]['pk'])
        if not isinstance(key, list) or len(key) != width or None in key:
            root_items.append((index, draft, UNDECIDABLE,
                               f'表 {table} 当前主键为 {width} 个分量，草稿键的维数或空值无法对照。'))
            continue
        root_items.append((index, draft, CONFIRMABLE,
                           '表与主键维数仍可对照，是否保留需由人工确认；行是否仍存在将在试算时验证。'))
    edge_items = []
    for token in values.get('edge', []):
        number_text, _, child = token.partition(':')
        if not number_text.isdecimal() or not child:
            edge_items.append((token, None, None, UNDECIDABLE, '草稿中的关系选项编码无法解析。'))
            continue
        number = int(number_text)
        old = next((edge for edge in evidence['edges']
                    if edge['child'] == child and edge['id'] == number), None)
        if old is None:
            edge_items.append((token, child, number, UNDECIDABLE,
                               f'外键 {number}（子表 {child}）不在原目录证据中，旧编号含义无法判定。'))
            continue
        new = _current_edge(schema, child, number)
        if new is None:
            edge_items.append((token, child, number, UNDECIDABLE,
                               f'外键 {number}（子表 {child}）已不存在，旧编号含义无法判定。'))
            continue
        if not _same_meaning(old, new):
            edge_items.append((token, child, number, UNDECIDABLE,
                               f'外键 {number}（子表 {child}）编号仍在，但父表或列映射已改变，'
                               '旧编号含义无法判定，不能代为选择。'))
            continue
        edge_items.append((token, child, number, CONFIRMABLE,
                           '父表与列映射均未改变，是否仍反向纳入需由人工确认。', new))
    return roots, root_items, edge_items


def edge_label(edge):
    return f'{edge["parent"]} → {edge["child"]} ({", ".join(edge["child_cols"])})'


def root_key_text(draft):
    key = draft.get('key') if isinstance(draft, dict) else None
    return code(key if isinstance(key, list) else draft)


def show(bench, values, error=''):
    alias = field(values, 'source')
    evidence = parse_evidence(field(values, 'evidence'))
    if evidence['digest'] != field(values, 'digest'):
        raise Fault('browser_form', '目录证据与表单绑定的模式摘要不一致，请重新打开登记页。', 400)
    current = bench.catalog(alias)
    if evidence['digest'] == current['schema_digest']:
        raise Fault('browser_form', '目录与原草稿一致，无需核对，请直接重新提交。', 400)
    _, root_items, edge_items = classify(evidence, values, current)
    root_rows = []
    for index, draft, status, reason in root_items:
        table = draft.get('table') if isinstance(draft, dict) else '—'
        if status == CONFIRMABLE:
            control = ('<input type="checkbox" name="keep_root" value="' + text(str(index)) + '">'
                       '<span class="status ready">可人工确认</span>')
        else:
            control = '<span class="status muted">含义无法判定</span>'
        root_rows.append(
            f'<tr><td>{index}</td><td>{code(table)}</td><td>{root_key_text(draft)}</td>'
            f'<td>{control}</td><td>{text(reason)}</td></tr>')
    edge_rows = []
    for token, child, number, status, reason, *rest in edge_items:
        old = next((edge for edge in evidence['edges']
                    if child is not None and edge['child'] == child and edge['id'] == number), None)
        old_text = edge_label(old) if old else '—'
        if status == CONFIRMABLE:
            new = rest[0]
            control = (f'<input type="checkbox" name="keep_edge" value="{text(token)}">'
                       '<span class="status ready">可人工确认</span>')
            new_text = edge_label(new)
        else:
            control = '<span class="status muted">含义无法判定</span>'
            new = _current_edge(current['schema'], child, number) if child is not None else None
            new_text = edge_label(new) if new else '（当前目录无此编号）'
        edge_rows.append(
            f'<tr><td>{code(child if child is not None else "—")} · '
            f'{text(number if number is not None else "—")}</td>'
            f'<td>{code(old_text)}</td><td>{code(new_text)}</td><td>{control}</td><td>{text(reason)}</td></tr>')
    has_confirmable_root = any(item[2] == CONFIRMABLE for item in root_items)
    notice = (f'<p role="alert" class="notice">{text(error)}</p>' if error else
              '<p role="alert" class="notice info">源库目录已在填表期间变化。请逐项核对：'
              '只有勾选的项目会进入新版本，含义无法判定的项目不会被代选。</p>')
    return 409, page('核对漂移目录中的草稿', 'recipe', 'recipe_reconcile',
                     source=text(alias), name=text(field(values, 'name')),
                     limit=text(field(values, 'row_limit', '1000')),
                     old_digest=text(evidence['digest']), new_digest=text(current['schema_digest']),
                     evidence=text(field(values, 'evidence')),
                     roots=text(field(values, 'roots')),
                     root_rows=''.join(root_rows),
                     edge_rows=''.join(edge_rows) or
                     '<tr><td colspan="5" class="empty">原草稿没有选择反向关系。</td></tr>',
                     can_commit='' if has_confirmable_root else 'disabled', notice=notice)


def recover(bench, values):
    """Build a register request only from explicitly confirmed draft items."""
    alias = field(values, 'source')
    evidence = parse_evidence(field(values, 'evidence'))
    if evidence['digest'] != field(values, 'digest'):
        raise Fault('browser_form', '目录证据与表单绑定的模式摘要不一致，请重新打开核对页。', 400)
    current = bench.catalog(alias)
    new_digest = field(values, 'new_digest')
    if not DIGEST.fullmatch(new_digest):
        raise Fault('browser_form', '当前模式摘要格式不正确，请重新打开核对页。', 400)
    if new_digest != current['schema_digest']:
        raise Fault('browser_schema', '目录在核对期间再次变化，请重新核对主键和外键。', 409)
    if evidence['digest'] == new_digest:
        raise Fault('browser_form', '目录与原草稿一致，无需走核对流程。', 400)
    _, root_items, edge_items = classify(evidence, values, current)
    kept_roots = set(_indices(values.get('keep_root', []), len(root_items)))
    kept_edges = set(values.get('keep_edge', []))
    confirmable_roots = {index for index, _, status, _ in root_items if status == CONFIRMABLE}
    confirmable_edges = {token for token, _, _, status, *_ in edge_items if status == CONFIRMABLE}
    if not kept_roots <= confirmable_roots or not kept_edges <= confirmable_edges:
        raise Fault('browser_form', '只能勾选仍可对照的项目，无法判定的旧编号不能提交。', 400)
    if not kept_roots:
        raise Fault('invalid_roots', '请至少显式确认一条仍可对照的根记录，否则无法登记新版本。', 400)
    if field(values, 'confirmed') != '1':
        raise Fault('browser_form', '请勾选最终提交确认，明确绑定到当前模式版本。', 400)
    roots = [draft for index, draft, status, _ in root_items if index in kept_roots]
    chosen = []
    for token in kept_edges:
        number_text, _, child = token.partition(':')
        chosen.append(dict(child=child, id=int(number_text)))
    raw_limit = field(values, 'row_limit')
    if not raw_limit.isascii() or not raw_limit.isdecimal() or len(raw_limit) > 4:
        raise Fault('invalid_limit', 'Invalid row limit')
    body = dict(source=alias, roots=roots, follow_children=chosen, row_limit=int(raw_limit))
    return bench.register(field(values, 'name'), body, expected_schema=new_digest)


def _indices(tokens, upper):
    result = []
    for token in tokens:
        if not token.isdecimal():
            raise Fault('browser_form', '根确认字段格式不正确。', 400)
        index = int(token)
        if not 0 <= index < upper:
            raise Fault('browser_form', '确认的根记录不在草稿范围内。', 400)
        result.append(index)
    return result

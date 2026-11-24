"""Evaluate draft foreign keys that are not declared in the source schema.

An onboarding team may know which columns *look like* a relationship even though
the SQLite catalog carries no ``FOREIGN KEY`` declaration. A draft is judged from
two independent kinds of evidence:

* Catalog-only conditions (collation, type affinity, and whether the parent key
  can ever be guaranteed unique) can be decided without reading a single row.
* Row-data conditions (whether the current snapshot actually contains duplicate
  parents, orphan children, or matches that only hold under NOCASE) require
  reading real rows inside the caller's read transaction.

The rules mirror what SQLite itself does when a declared foreign key is used:
the parent key needs a primary key or a non-partial, non-expression unique
index whose key columns match (in either order) with the same collation; the
parent column's collation and affinity drive comparison; and a composite child
key with any NULL component has no required parent.
"""

import re

from .catalog import resolve
from .codec import Fault, encode, quote

WITNESS_LIMIT = 5
COLLATION_RE = re.compile(r'\bCOLLATE\s+([A-Za-z_][A-Za-z0-9_]*)\b')


def affinity(declared_type):
    upper = (declared_type or '').upper()
    if not upper:
        return 'BLOB'
    if 'INT' in upper:
        return 'INTEGER'
    if any(token in upper for token in ('CHAR', 'CLOB', 'TEXT', 'STRING')):
        return 'TEXT'
    if any(token in upper for token in ('BLOB', 'REAL', 'FLOA', 'DOUB')):
        return 'REAL' if any(token in upper for token in ('REAL', 'FLOA', 'DOUB')) else 'BLOB'
    return 'NUMERIC'


def _split_top_level(body):
    parts, start, depth = [], 0, 0
    quote = None
    for index, char in enumerate(body):
        if quote:
            if char == quote:
                quote = None
            continue
        if char in ('"', "'", '`'):
            quote = char
        elif char == '(':
            depth += 1
        elif char == ')':
            depth -= 1
        elif char == ',' and depth == 0:
            parts.append(body[start:index])
            start = index + 1
    parts.append(body[start:])
    return parts


def column_collations(table):
    """Extract each column's declared collation from the stored CREATE TABLE."""
    ddl = table['ddl']
    opening = ddl.find('(')
    body = ddl[opening + 1:ddl.rfind(')')]
    result = {}
    for fragment in _split_top_level(body):
        token = fragment.strip()
        match = re.match(r'^(?:"(?:[^"]|"")+"|`[^`]+`|\[[^]]+\]|[A-Za-z_][A-Za-z0-9_$]*)', token)
        if not match:
            continue
        head = match.group(0).strip('"`[]')
        if head.upper() in {
            'PRIMARY', 'FOREIGN', 'UNIQUE', 'CHECK', 'CONSTRAINT', 'LIKE'
        }:
            continue
        found = COLLATION_RE.search(token)
        result[head] = found.group(1) if found else 'BINARY'
    for column in table['columns']:
        result.setdefault(column['name'], 'BINARY')
    return result


def _index_signature(conn, table, index_name):
    rows = conn.execute(f'PRAGMA main.index_xinfo({quote(index_name)})').fetchall()
    columns, collations = [], []
    for row in rows:
        if row['key'] != 1:
            continue
        if row['cid'] < 0:
            return None
        columns.append(table['columns'][row['cid']]['name'])
        collations.append((row['coll'] or 'BINARY'))
    return columns, collations


def unique_indexes(conn, table_name, table):
    guarantees = []
    for entry in conn.execute(f'PRAGMA main.index_list({quote(table_name)})').fetchall():
        if not entry['unique'] or entry['partial']:
            continue
        signature = _index_signature(conn, table, entry['name'])
        if signature is None:
            continue
        guarantees.append(dict(name=entry['name'], origin=entry['origin'],
                               columns=signature[0], collations=signature[1]))
    return guarantees


def _same_columns(guarantee, parent_cols):
    return sorted(guarantee['columns']) == sorted(parent_cols)


def catalog_checks(conn, schema, proposal):
    """Decide every condition that follows from the catalog alone."""
    child = resolve(schema['tables'], proposal['child'])
    parent = resolve(schema['tables'], proposal['parent'])
    child_cols = [resolve((c['name'] for c in schema['tables'][child]['columns']), name)
                  for name in proposal['child_cols']]
    parent_cols = [resolve((c['name'] for c in schema['tables'][parent]['columns']), name)
                   for name in proposal['parent_cols']]
    child_table, parent_table = schema['tables'][child], schema['tables'][parent]
    child_collations = column_collations(child_table)
    parent_collations = column_collations(parent_table)
    findings = []
    blockers = []

    def finding(scope, code, detail, blocking, evidence=None):
        item = dict(scope=scope, code=code, detail=detail, blocking=blocking)
        if evidence:
            item['evidence'] = evidence
        findings.append(item)
        if blocking:
            blockers.append(code)

    if len(child_cols) != len(parent_cols):
        finding('catalog', 'dimension_mismatch',
                f'子键 {len(child_cols)} 列与父键 {len(parent_cols)} 列维度不一致，SQLite 无法建立此外键。', True)
    if len(set(child_cols)) != len(child_cols) or len(set(parent_cols)) != len(parent_cols):
        finding('catalog', 'repeated_column', '外键的一侧出现重复列，候选列必须两两不同。', True)
    if child_cols and len(child_cols) == len(parent_cols):
        for child_col, parent_col in zip(child_cols, parent_cols):
            child_coll = child_collations.get(child_col, 'BINARY')
            parent_coll = parent_collations.get(parent_col, 'BINARY')
            if child_coll != parent_coll:
                finding('catalog', 'collation_difference',
                        f'子列 {child_col} 为 {child_coll}，父列 {parent_col} 为 {parent_coll}；'
                        '匹配始终按父列排序规则解释，接入前应统一排序规则。',
                        False, dict(child_collation=child_coll, parent_collation=parent_coll,
                                    child_column=child_col, parent_column=parent_col))
            child_affinity = affinity(next(col['type'] for col in child_table['columns']
                                           if col['name'] == child_col))
            parent_affinity = affinity(next(col['type'] for col in parent_table['columns']
                                            if col['name'] == parent_col))
            if child_affinity != parent_affinity:
                finding('catalog', 'affinity_difference',
                        f'子列 {child_col} 与父列 {parent_col} 的类型亲和性不同'
                        f'（{child_affinity} vs {parent_affinity}），数值与文本比较会被亲和性改写。',
                        False, dict(child_affinity=child_affinity, parent_affinity=parent_affinity,
                                    child_column=child_col, parent_column=parent_col))
        nullable = [col for col in child_table['columns']
                    if col['name'] in child_cols and not col['notnull']]
        if nullable:
            finding('catalog', 'nullable_child_key',
                    '子键含可空列 ' + '、'.join(col['name'] for col in nullable)
                    + '；复合子键任一分量为 NULL 时 SQLite 不要求父行，这些行不计为缺父。',
                    False, dict(columns=[col['name'] for col in nullable]))

    guarantees = [candidate for candidate in unique_indexes(conn, parent, parent_table)
                  if _same_columns(candidate, parent_cols)] if child_cols else []
    pk_match = parent_table['pk'] and (
        sorted(parent_table['pk']) == sorted(parent_cols) and len(parent_table['pk']) == len(parent_cols))
    if pk_match:
        guarantee = dict(kind='primary_key', name='PRIMARY KEY', columns=parent_table['pk'])
        finding('catalog', 'parent_key_guaranteed',
                f'父表 {parent} 的主键覆盖候选父键，唯一性由目录保证。', False, guarantee)
    elif guarantees:
        chosen = guarantees[0]
        finding('catalog', 'parent_key_guaranteed',
                f'父表 {parent} 的非部分唯一索引 {chosen["name"]} 覆盖候选父键，唯一性由目录保证。',
                False, dict(kind='unique_index', name=chosen['name'],
                            columns=chosen['columns'], origin=chosen['origin']))
    else:
        finding('catalog', 'parent_key_not_guaranteed',
                f'父表 {parent} 没有覆盖这些列的主键或非部分唯一索引；即使当前样本恰好唯一，'
                'SQLite 在写入时仍会报 foreign key mismatch，需要先补约束。',
                True, dict(parent_columns=parent_cols))
    normalized = dict(child=child, parent=parent, child_cols=child_cols, parent_cols=parent_cols)
    return normalized, findings, blockers, parent_collations


def _not_null(columns, alias):
    return ' AND '.join(f'{alias}.{quote(column)} IS NOT NULL' for column in columns)


def row_checks(conn, schema, normalized, parent_collations):
    """Read real rows to verify the conditions a catalog cannot settle."""
    child, parent = normalized['child'], normalized['parent']
    child_cols, parent_cols = normalized['child_cols'], normalized['parent_cols']
    findings = []
    blockers = []
    if not child_cols or len(child_cols) != len(parent_cols):
        return findings, blockers, dict(child_rows=0, complete_child_keys=0, null_component_rows=0,
                                        semantic_matches=0, binary_matches=0, orphans=0,
                                        ambiguous_parent_keys=0, duplicate_parent_keys=0)

    def finding(scope, code, detail, blocking, evidence=None):
        item = dict(scope=scope, code=code, detail=detail, blocking=blocking)
        if evidence:
            item['evidence'] = evidence
        findings.append(item)
        if blocking:
            blockers.append(code)

    child_alias = ', '.join(f'c.{quote(column)}' for column in child_cols)
    parent_alias = ', '.join(f'p.{quote(column)}' for column in parent_cols)
    semantic_on = ' AND '.join(
        f'p.{quote(p)} = +c.{quote(c)} COLLATE {parent_collations.get(p, "BINARY")}'
        for c, p in zip(child_cols, parent_cols))
    binary_on = ' AND '.join(f'p.{quote(p)} = +c.{quote(c)} COLLATE BINARY'
                             for c, p in zip(child_cols, parent_cols))
    child_rows = conn.execute(f'SELECT COUNT(*) FROM {quote(child)} c').fetchone()[0]
    null_rows = conn.execute(
        f'SELECT COUNT(*) FROM {quote(child)} c WHERE NOT ({_not_null(child_cols, "c")})').fetchone()[0]
    complete = child_rows - null_rows

    def distinct_parent(comparison):
        sql = f"""SELECT COUNT(*) FROM (
            SELECT {child_alias} FROM {quote(child)} c
            WHERE {_not_null(child_cols, 'c')} AND EXISTS (
                SELECT 1 FROM {quote(parent)} p WHERE {comparison})
            GROUP BY {child_alias})"""
        return conn.execute(sql).fetchone()[0]

    semantic_matches = distinct_parent(semantic_on)
    binary_matches = distinct_parent(binary_on)

    orphan_sql = f"""SELECT {child_alias} FROM {quote(child)} c
        WHERE {_not_null(child_cols, 'c')} AND NOT EXISTS (
            SELECT 1 FROM {quote(parent)} p WHERE {semantic_on}) LIMIT {WITNESS_LIMIT + 1}"""
    orphan_witnesses = [[encode(value) for value in row]
                        for row in conn.execute(orphan_sql).fetchall()]
    orphan_count = conn.execute(f"""SELECT COUNT(*) FROM {quote(child)} c
        WHERE {_not_null(child_cols, 'c')} AND NOT EXISTS (
            SELECT 1 FROM {quote(parent)} p WHERE {semantic_on})""").fetchone()[0]
    if orphan_witnesses:
        finding('rows', 'orphan_child_keys',
                f'当前快照有 {orphan_count} 个完整子键在父表找不到匹配，声明外键前必须先清理或补父。',
                True, dict(count=orphan_count, witnesses=orphan_witnesses[:WITNESS_LIMIT],
                           truncated=len(orphan_witnesses) > WITNESS_LIMIT))

    group_cols = ', '.join(quote(column) for column in parent_cols)
    parent_where = _not_null(parent_cols, 'p')
    duplicate_sql = f"""SELECT {group_cols} FROM {quote(parent)} p
        WHERE {parent_where}
        GROUP BY {group_cols} HAVING COUNT(*) > 1 LIMIT {WITNESS_LIMIT + 1}"""
    duplicate_witnesses = [[encode(value) for value in row]
                           for row in conn.execute(duplicate_sql).fetchall()]
    duplicate_count = conn.execute(f"""SELECT COUNT(*) FROM (
        SELECT {group_cols} FROM {quote(parent)} p
        WHERE {parent_where}
        GROUP BY {group_cols} HAVING COUNT(*) > 1)""").fetchone()[0]
    if duplicate_witnesses:
        finding('rows', 'duplicate_parent_keys',
                f'父表存在 {duplicate_count} 个重复父键，父查找无法唯一，接入后会触发 foreign key mismatch。',
                True, dict(count=duplicate_count, witnesses=duplicate_witnesses[:WITNESS_LIMIT],
                           truncated=len(duplicate_witnesses) > WITNESS_LIMIT))

    ambiguous_sql = f"""SELECT {child_alias} FROM {quote(child)} c
        WHERE {_not_null(child_cols, 'c')} AND (
            SELECT COUNT(*) FROM {quote(parent)} p WHERE {semantic_on}) > 1
        LIMIT {WITNESS_LIMIT + 1}"""
    ambiguous_witnesses = [[encode(value) for value in row]
                           for row in conn.execute(ambiguous_sql).fetchall()]
    ambiguous_count = conn.execute(f"""SELECT COUNT(*) FROM {quote(child)} c
        WHERE {_not_null(child_cols, 'c')} AND (
            SELECT COUNT(*) FROM {quote(parent)} p WHERE {semantic_on}) > 1""").fetchone()[0]
    if ambiguous_witnesses:
        finding('rows', 'ambiguous_child_keys',
                f'有 {ambiguous_count} 个子键能匹配多个父行，关系不具函数性。',
                True, dict(count=ambiguous_count, witnesses=ambiguous_witnesses[:WITNESS_LIMIT],
                           truncated=len(ambiguous_witnesses) > WITNESS_LIMIT))

    if complete and semantic_matches > binary_matches:
        nocase_sql = f"""SELECT {child_alias} FROM {quote(child)} c
            WHERE {_not_null(child_cols, 'c')} AND EXISTS (
                SELECT 1 FROM {quote(parent)} p WHERE {semantic_on})
            AND NOT EXISTS (SELECT 1 FROM {quote(parent)} p WHERE {binary_on})
            LIMIT {WITNESS_LIMIT + 1}"""
        nocase_witnesses = [[encode(value) for value in row]
                            for row in conn.execute(nocase_sql).fetchall()]
        finding('rows', 'nocase_only_matches',
                f'{semantic_matches - binary_matches} 个完整子键仅在父列排序规则下匹配，'
                '按二进制拼法并不相等；接入建议保留父列排序规则而不是改写数据。',
                False, dict(witnesses=nocase_witnesses[:WITNESS_LIMIT],
                            truncated=len(nocase_witnesses) > WITNESS_LIMIT))
    if null_rows:
        finding('rows', 'null_component_exempt',
                f'{null_rows} 行的子键至少一个分量为 NULL；按 SQLite 语义它们没有必需父行，不计为缺父。',
                False, dict(count=null_rows))
    metrics = dict(child_rows=child_rows, complete_child_keys=complete, null_component_rows=null_rows,
                   semantic_matches=semantic_matches, binary_matches=binary_matches, orphans=orphan_count,
                   ambiguous_parent_keys=ambiguous_count, duplicate_parent_keys=duplicate_count)
    return findings, blockers, metrics


def _verdict(catalog_blockers, row_blockers, metrics):
    if catalog_blockers:
        return 'blocked_catalog', '目录条件不成立：需先修正结构或补唯一约束，暂不建议声明此外键。'
    if row_blockers:
        return 'blocked_data', '当前数据不满足外键条件：请按下列行级证据清理数据后再评估。'
    if metrics['complete_child_keys'] == 0:
        return 'inconclusive', '没有完整非空子键可供行级验证，目录条件成立但数据证据不足。'
    return 'ready', '目录与当前快照均支持该关系，可按建议声明外键；行级结论仅对本次读快照有效。'


def normalize(body):
    if not isinstance(body, dict):
        raise Fault('invalid_request', 'Expected a JSON object', 400)
    required = ('source', 'child', 'parent', 'child_cols', 'parent_cols')
    if set(body) != set(required):
        raise Fault('invalid_request',
                    f'Expected fields {list(required)}', 400)
    if not all(isinstance(body[key], str) for key in ('source', 'child', 'parent')):
        raise Fault('invalid_request', 'source, child and parent must be strings', 400)
    for key in ('child_cols', 'parent_cols'):
        if not isinstance(body[key], list) or not body[key] or not all(isinstance(v, str) for v in body[key]):
            raise Fault('invalid_request', f'{key} must be a non-empty list of column names', 400)
    if len(body['child_cols']) > 32 or len(body['parent_cols']) > 32:
        raise Fault('invalid_request', 'A draft foreign key supports at most 32 columns', 400)
    return dict(body)


def evaluate(conn, schema, proposal):
    normalized, catalog_findings, catalog_blockers, parent_collations = catalog_checks(conn, schema, proposal)
    row_findings, row_blockers, metrics = row_checks(conn, schema, normalized, parent_collations)
    verdict, recommendation = _verdict(catalog_blockers, row_blockers, metrics)
    return dict(proposal=normalized, verdict=verdict, recommendation=recommendation,
                catalog_findings=catalog_findings, row_findings=row_findings, metrics=metrics,
                catalog_blocking=catalog_blockers, row_blocking=row_blockers)

"""Handoff revisit materials and their comparison against the sealed delivery.

The receiver never sends a database file back.  A revisit package only carries
materials without row originals: schema text, counts and digests.  Every finding
is therefore either a provable logical equivalence, a locatable structural or
value change, or an explicit gap where the materials are insufficient.
"""

import re
import sqlite3
from contextlib import closing
from pathlib import Path

from .catalog import inspect
from .codec import Fault, digest, dumps, fingerprint, key, loads, quote, row_values

MATERIAL_FILES = ('manifest.json', 'file.json', 'schema.json', 'tables.json')
DIGEST_LEN = 16  # 128-bit digests are sufficient for local revisit materials
BUCKETS = (
    ('equivalent', '可证实的逻辑等价'),
    ('change', '可定位的结构或值变化'),
    ('unverified', '材料不足'),
)


def short(value):
    return digest(dumps(value).encode('ascii'))[:DIGEST_LEN]


def row_digest(values):
    return short(values)


def column_digest(value):
    return short(value)


def key_ref(token):
    """Digest of a canonical row-key token; materials never carry key literals."""
    return digest(token.encode('ascii'))[:DIGEST_LEN]


HEX_REF = re.compile(r'[0-9a-f]{%d}\Z' % DIGEST_LEN)


def valid_ref(ref):
    return bool(HEX_REF.fullmatch(ref or ''))


# ---------------------------------------------------------------------------
# Material generation on the receiver side (keelsplice.revisit_materials)
# ---------------------------------------------------------------------------

def materialize(database, anchor, *, note=''):
    """Build the four no-row-text material files for a delivered SQLite file."""
    database = Path(database)
    with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA query_only=ON')
        conn.execute('PRAGMA trusted_schema=OFF')
        integrity = [row[0] for row in conn.execute('PRAGMA main.integrity_check')]
        schema = inspect(conn)
        tables = {}
        for name, meta in schema['tables'].items():
            columns = [col['name'] for col in meta['columns']]
            rows = {}
            column_digests = {column: [] for column in columns}
            for row in conn.execute(f'SELECT * FROM {quote(name)}'):
                values = row_values(row, columns)
                token = key(row_values(row, meta['pk']))
                rows[key_ref(token)] = row_digest(values)
                for column, value in zip(columns, values):
                    column_digests[column].append(column_digest(value))
            tables[name] = {
                'count': len(rows),
                'row_digests': dict(sorted(rows.items())),
                'table_digest': short(rows),
                'column_digests': {column: short(pieces) for column, pieces in column_digests.items()},
            }
    file_material = {
        'sha256': digest(database.read_bytes()),
        'bytes': database.stat().st_size,
        'integrity_check': integrity,
        'sqlite_version': sqlite3.sqlite_version,
    }
    schema_material = {
        'schema_digest': fingerprint(schema),
        'tables': {name: {'ddl': meta['ddl'], 'pk': meta['pk'],
                          'columns': meta['columns'], 'without_rowid': meta['without_rowid'],
                          'strict': meta['strict']}
                   for name, meta in schema['tables'].items()},
        'edges': schema['edges'],
        'indexes': [_index_outline(item) for item in schema['indexes']],
    }
    tables_material = {'tables': tables}
    manifest = {
        'material_version': 1,
        'anchor': _anchor(anchor),
        'note': note,
        'files': {
            'file.json': short(file_material),
            'schema.json': short(schema_material),
            'tables.json': short(tables_material),
        },
    }
    return {
        'manifest.json': manifest,
        'file.json': file_material,
        'schema.json': schema_material,
        'tables.json': tables_material,
    }


def _index_outline(item):
    return {'name': item['name'], 'tbl_name': item['tbl_name'], 'sql': item['sql']}


def _anchor(anchor):
    if not isinstance(anchor, dict) or set(anchor) != {'capture_digest', 'sha256'}:
        raise Fault('invalid_anchor', 'Anchor needs capture_digest and sha256')
    if not all(isinstance(anchor[k], str) and len(anchor[k]) == 64 for k in anchor):
        raise Fault('invalid_anchor', 'Anchor digests must be 64-character hex strings')
    return dict(sorted(anchor.items()))


# ---------------------------------------------------------------------------
# Package intake and integrity
# ---------------------------------------------------------------------------

def open_package(body):
    if not isinstance(body, dict):
        raise Fault('invalid_revisit', '回访材料须是 JSON 对象。', 400)
    files = body.get('files')
    if not isinstance(files, dict) or set(files) != set(MATERIAL_FILES):
        raise Fault('invalid_revisit', f'材料包必须恰好包含 {", ".join(MATERIAL_FILES)}。', 400)
    materials = {}
    for name in MATERIAL_FILES:
        try:
            materials[name] = loads(files[name]) if isinstance(files[name], str) else files[name]
        except (TypeError, ValueError) as exc:
            raise Fault('invalid_revisit', f'{name} 不是有效的 JSON 材料。', 400) from exc
    manifest = materials['manifest.json']
    if not isinstance(manifest, dict) or not isinstance(manifest.get('files'), dict):
        raise Fault('invalid_revisit', 'manifest.json 缺少文件摘要清单。', 400)
    for name in ('file.json', 'schema.json', 'tables.json'):
        claimed = manifest['files'].get(name)
        actual = short(materials[name])
        if claimed != actual:
            raise Fault('material_tampered', f'{name} 的摘要与 manifest.json 记录不一致，材料不可信。', 409)
    return materials


def anchor_of(materials):
    anchor = materials['manifest.json'].get('anchor')
    if not isinstance(anchor, dict):
        raise Fault('invalid_anchor', 'manifest.json 缺少交付锚点。', 400)
    return _anchor(anchor)


# ---------------------------------------------------------------------------
# Sealed-side projections, computed independently from the delivery record
# ---------------------------------------------------------------------------

def sealed_tables(sealed):
    """Project sealed rows into the same digest-only shape as the materials."""
    tables = {}
    for name, rows in sealed['result']['rows'].items():
        meta = sealed['schema']['tables'][name]
        columns = [col['name'] for col in meta['columns']]
        row_digests = {}
        column_digests = {column: [] for column in columns}
        for token, stored in rows.items():
            row_digests[key_ref(token)] = row_digest(stored)
            for column, value in zip(columns, stored):
                column_digests[column].append(column_digest(value))
        tables[name] = {
            'count': len(rows),
            'row_digests': dict(sorted(row_digests.items())),
            'table_digest': short(dict(sorted(row_digests.items()))),
            'column_digests': {column: short(pieces) for column, pieces in column_digests.items()},
        }
    return tables


def sealed_key_index(sealed):
    """Map table -> key digest -> canonical plaintext token, held only server-side."""
    index = {}
    for name, rows in sealed['result']['rows'].items():
        index[name] = {key_ref(token): token for token in rows}
    return index


# ---------------------------------------------------------------------------
# Comparison engine
# ---------------------------------------------------------------------------

def _finding(bucket, subject, detail, evidence, recheck):
    return {'bucket': bucket, 'subject': subject, 'detail': detail,
            'material_files': sorted(evidence), 'recheck': recheck}


def compare(sealed, certificate, materials):
    """Return findings grouped into the three evidence buckets."""
    findings = []
    schema_material = materials['schema.json']
    tables_material = materials['tables.json']
    file_material = materials['file.json']
    delivered_tables = sealed_tables(sealed)
    key_index = sealed_key_index(sealed)
    actual_schema_digest = fingerprint(sealed['schema'])

    # --- File-level evidence: bytes versus the delivered certificate ------
    delivered_sha = certificate['sha256']
    observed_sha = file_material.get('sha256')
    if not isinstance(observed_sha, str) or len(observed_sha) != 64:
        findings.append(_finding('unverified', 'file.json', '现场库字节摘要缺失或格式无效，无法核对文件。',
                                 ('manifest.json', 'file.json'), '材料不足，无法独立复查。'))
    elif observed_sha == delivered_sha:
        findings.append(_finding('equivalent', 'database bytes', '现场库字节与交付证书完全一致。',
                                 ('file.json',), '与证书 SHA-256 相符。'))
    else:
        findings.append(_finding('unverified', 'file.json',
                                 '现场库字节摘要与交付证书不同；仅凭字节无法判断 VACUUM 还是内容改写，需结合结构与逐表材料。',
                                 ('manifest.json', 'file.json'), '服务端不持有现场库字节，无法独立复算该摘要。'))
    integrity = file_material.get('integrity_check')
    if integrity == ['ok']:
        findings.append(_finding('equivalent', 'integrity_check', '接收方报告 SQLite 完整性检查为 ok。',
                                 ('file.json',), '交付认证时完整性检查同为 ok。'))
    elif integrity is not None:
        findings.append(_finding('unverified', 'file.json',
                                 f'接收方完整性检查返回 {dumps(integrity)[:120]}，需要现场复查。',
                                 ('file.json',), '服务端无法接触现场库，不能独立复算。'))

    # --- Schema-level evidence --------------------------------------------
    observed_schema_digest = schema_material.get('schema_digest')
    if observed_schema_digest == actual_schema_digest:
        findings.append(_finding('equivalent', 'schema fingerprint',
                                 '现场模式摘要与封存模式摘要一致，表定义、列、外键和显式索引逻辑等价。',
                                 ('schema.json',), f'与封存摘要 {actual_schema_digest[:12]}… 相符。'))
    else:
        findings.extend(_schema_findings(sealed['schema'], schema_material, actual_schema_digest))

    # --- Table/row-level evidence ------------------------------------------
    observed_tables = tables_material.get('tables')
    if not isinstance(observed_tables, dict):
        findings.append(_finding('unverified', 'tables.json', '逐表材料缺失，无法定位任何行级变化。',
                                 ('manifest.json', 'tables.json'), '材料不足，无法独立复查。'))
        return _group(findings)

    for name, expected in delivered_tables.items():
        observed = observed_tables.get(name)
        if observed is None:
            findings.append(_finding(
                'change', f'table:{name}', '交付范围内的表在现场库逐表材料中缺失，表可能已被删除或改名。',
                ('schema.json', 'tables.json'),
                f'封存记录含 {expected["count"]} 行；可在现场对该表执行独立 PRAGMA/SELECT 复查。'))
            continue
        findings.extend(_table_findings(name, expected, observed, key_index.get(name, {})))

    extra = sorted(set(observed_tables) - set(delivered_tables))
    for name in extra:
        findings.append(_finding(
            'change', f'table:{name}', '现场库出现交付记录之外的表（交付只覆盖被选表与完整结构）。',
            ('schema.json', 'tables.json'), '可在现场核对该表的 DDL 与来源。'))

    # --- Byte-level verdict promotion --------------------------------------
    _promote_byte_verdict(findings, observed_sha, delivered_sha)
    return _group(findings)


def _promote_byte_verdict(findings, observed_sha, delivered_sha):
    """A VACUUM-only database keeps every logical material but changes bytes."""
    if observed_sha is None or observed_sha == delivered_sha:
        return
    logical = [item for item in findings if item['subject'] not in ('file.json', 'database bytes')]
    if logical and all(item['bucket'] == 'equivalent' for item in logical):
        for item in findings:
            if item['subject'] == 'file.json' and item['bucket'] == 'unverified':
                item['bucket'] = 'equivalent'
                item['detail'] = ('字节摘要与交付证书不同，但模式、逐表计数与逐行摘要全部一致：'
                                  '可证实为 VACUUM/重打包类逻辑等价改写，行原文未变。')
                item['recheck'] = '服务端已从封存记录独立重算全部逻辑摘要并相符。'


def _schema_findings(expected_schema, schema_material, actual_schema_digest):
    findings = []
    observed_tables = schema_material.get('tables')
    observed_edges = schema_material.get('edges')
    observed_indexes = schema_material.get('indexes')
    if not isinstance(observed_tables, dict):
        findings.append(_finding('unverified', 'schema.json', '模式材料形状无效，无法定位结构差异。',
                                 ('manifest.json', 'schema.json'), '材料不足，无法独立复查。'))
        return findings
    findings.append(_finding('change', 'schema fingerprint',
                             f'现场模式摘要 {str(schema_material.get("schema_digest"))[:12]}… 与封存摘要 '
                             f'{actual_schema_digest[:12]}… 不同。',
                             ('schema.json',), '服务端持有封存模式，可逐项对照下列定位结果。'))
    for name, meta in expected_schema['tables'].items():
        observed = observed_tables.get(name)
        if observed is None:
            findings.append(_finding('change', f'table:{name}:ddl', '表定义在现场模式材料中缺失。',
                                     ('schema.json',), '可在现场执行 PRAGMA table_xinfo 独立复查。'))
        elif observed.get('ddl') != meta['ddl']:
            findings.append(_finding('change', f'table:{name}:ddl',
                                     '表 DDL 不同，可能涉及列、生成列表达式或约束变化。',
                                     ('schema.json',), '封存 DDL 与现场 DDL 均可逐条对照。'))
        elif observed.get('columns') != meta['columns']:
            findings.append(_finding('change', f'table:{name}:columns',
                                     '列元数据（类型、亲和性、生成方式或约束）不同。',
                                     ('schema.json',), '可在现场用 PRAGMA table_xinfo 独立复查。'))
        else:
            findings.append(_finding('equivalent', f'table:{name}:ddl', '表定义与列元数据逻辑等价。',
                                     ('schema.json',), '与封存目录逐字段相符。'))
    for name in sorted(set(observed_tables) - set(expected_schema['tables'])):
        findings.append(_finding('change', f'table:{name}:ddl', '现场库新增了交付记录之外的表定义。',
                                 ('schema.json',), '可在现场 sqlite_schema 中独立复查。'))
    if isinstance(observed_edges, list):
        if observed_edges == expected_schema['edges']:
            findings.append(_finding('equivalent', 'foreign keys', '外键声明与封存目录一致。',
                                     ('schema.json',), '与封存外键清单逐条相符。'))
        else:
            findings.append(_finding('change', 'foreign keys', '外键声明存在差异，影响关系约束。',
                                     ('schema.json',), '可在现场用 PRAGMA foreign_key_list 独立复查。'))
    findings.extend(_index_findings(expected_schema['indexes'], observed_indexes))
    return findings


def _index_findings(expected_indexes, observed_indexes):
    if not isinstance(observed_indexes, list):
        return [_finding('unverified', 'schema.json:indexes', '索引材料缺失，无法判断索引变化。',
                         ('schema.json',), '材料不足，无法独立复查。')]
    expected = {item['name']: item for item in (_index_outline(i) for i in expected_indexes)}
    observed = {item['name']: item for item in observed_indexes if isinstance(item, dict) and 'name' in item}
    findings = []
    for name in sorted(set(expected) | set(observed)):
        if name not in observed:
            findings.append(_finding('change', f'index:{name}', '显式索引在现场库缺失。',
                                     ('schema.json',), '可在现场 sqlite_schema 独立复查。'))
        elif name not in expected:
            findings.append(_finding('change', f'index:{name}', '现场库新增显式索引。',
                                     ('schema.json',), '可在现场 sqlite_schema 独立复查。'))
        elif observed[name].get('sql') == expected[name]['sql']:
            findings.append(_finding('equivalent', f'index:{name}', '索引定义 SQL 与交付记录一致。',
                                     ('schema.json',), '与封存索引 SQL 逐字符相符。'))
        else:
            findings.append(_finding('change', f'index:{name}', '索引定义 SQL 不同（列、谓词或排序可能变化）。',
                                     ('schema.json',), '封存与现场的 CREATE INDEX 语句可直接对照。'))
    return findings


def _table_findings(name, expected, observed, key_index):
    findings = []
    evidence = ('tables.json',)
    expected_count = expected['count']
    observed_count = observed.get('count')
    if type(observed_count) is not int:
        findings.append(_finding('unverified', f'table:{name}:count', '缺少可信的行数材料。',
                                 evidence, '材料不足，无法独立复查。'))
        return findings
    observed_rows = observed.get('row_digests')
    if not isinstance(observed_rows, dict):
        if observed_count == expected_count and observed.get('table_digest') == expected['table_digest']:
            findings.append(_finding('equivalent', f'table:{name}',
                                     '行数与整表摘要一致；该材料未含逐行摘要，无法进一步定位。',
                                     evidence, '服务端已从封存行独立重算整表摘要并相符。'))
        else:
            findings.append(_finding('unverified', f'table:{name}',
                                     '整表摘要或行数不同，但材料未含逐行摘要，无法定位具体行。',
                                     evidence, '可在现场按主键执行独立 SELECT 复查。'))
        return findings

    expected_rows = expected['row_digests']
    if observed_count != expected_count or len(observed_rows) != observed_count:
        findings.append(_finding('change', f'table:{name}:count',
                                 f'行数不同：交付 {expected_count} 行，现场材料 {observed_count} 行。',
                                 evidence, '服务端封存计数可独立复核。'))
    removed_refs = sorted(set(expected_rows) - set(observed_rows))
    added_refs = sorted(set(observed_rows) - set(expected_rows))
    for ref in removed_refs:
        token = key_index.get(ref)
        location = f'主键 {loads(token)}' if token is not None else f'行键摘要 {ref}'
        findings.append(_finding('change', f'table:{name}:row:{ref}',
                                 f'交付行在现场缺失，{location}。', evidence,
                                 '服务端封存记录可独立确认该主键原本入选。'))
    for ref in added_refs:
        token = key_index.get(ref)
        location = f'行键摘要 {ref}（不在交付范围内）' if token is None else f'主键 {loads(token)}'
        findings.append(_finding('change', f'table:{name}:row:{ref}',
                                 f'现场出现交付之外的行，{location}。', evidence,
                                 '可在现场按该主键独立 SELECT 复查。'))
    modified_columns = _modified_columns(expected, observed)
    for ref in sorted(set(expected_rows) & set(observed_rows)):
        if expected_rows[ref] != observed_rows[ref]:
            token = key_index.get(ref)
            location = f'可定位到主键 {loads(token)}' if token is not None else f'行键摘要 {ref}'
            detail = f'行值变化，{location}。'
            if modified_columns:
                detail += ' 列摘要变化：' + '、'.join(modified_columns)
            findings.append(_finding('change', f'table:{name}:row:{ref}', detail, evidence,
                                     '服务端已从封存行独立重算该行摘要；可在现场按主键复查具体列（含 BLOB）。'))
    if not removed_refs and not added_refs and observed_count == expected_count and \
            observed_rows == expected_rows and observed.get('table_digest') == expected['table_digest']:
        findings.append(_finding('equivalent', f'table:{name}',
                                 '逐行主键摘要、逐列摘要与行数全部一致，行内容逻辑等价（含 BLOB 与生成列值）。',
                                 evidence, '服务端已从封存记录独立重算全部摘要并相符。'))
    return findings


def _modified_columns(expected, observed):
    expected_cols = expected.get('column_digests', {})
    observed_cols = observed.get('column_digests', {})
    if not isinstance(observed_cols, dict):
        return []
    return [column for column, digest_value in expected_cols.items()
            if observed_cols.get(column) != digest_value]


def _group(findings):
    grouped = {bucket: [] for bucket, _ in BUCKETS}
    for item in findings:
        grouped[item['bucket']].append(item)
    for bucket in grouped:
        grouped[bucket].sort(key=lambda item: (item['subject'], item['detail']))
    summary = {bucket: len(grouped[bucket]) for bucket in grouped}
    return {'buckets': grouped, 'summary': summary,
            'overall': 'equivalent' if not grouped['change'] and not grouped['unverified']
            else ('change' if grouped['change'] else 'unverified')}


def verify_anchor(anchor, certificate):
    if anchor['sha256'] != certificate['sha256']:
        raise Fault('anchor_mismatch',
                    '材料锚点的文件 SHA-256 与该交付证书不一致，材料不属于这份交付。', 409)
    if anchor['capture_digest'] != certificate['capture_digest']:
        raise Fault('anchor_mismatch',
                    '材料锚点的捕获摘要与该交付证书不一致，材料不属于这份交付。', 409)


def recheck(sealed, certificate, materials, subject):
    """Independently recompute the evidence behind one stored finding."""
    target = None
    report = compare(sealed, certificate, materials)
    for bucket, items in report['buckets'].items():
        for item in items:
            if item['subject'] == subject:
                target = item
                break
    if target is None:
        raise Fault('unknown_finding', '复查结论编号不存在。', 404)
    independent = _independent_evidence(subject, sealed, certificate, materials)
    return {'subject': subject, 'bucket': target['bucket'], 'stored_detail': target['detail'],
            'material_files': target['material_files'], 'independent_evidence': independent,
            'holds': independent['holds']}


def _evidence(holds, basis, **facts):
    return {'holds': holds, 'basis': basis, **facts}


def _independent_evidence(subject, sealed, certificate, materials):
    """Re-derive the same conclusion from sealed records, never trusting stored text."""
    schema_material = materials['schema.json']
    tables_material = materials.get('tables.json', {})
    sealed_digest = fingerprint(sealed['schema'])

    if subject == 'schema fingerprint':
        observed = schema_material.get('schema_digest')
        return _evidence(observed == sealed_digest,
                         '服务端从封存模式独立重算模式指纹，再与现场 schema.json 指纹对照。',
                         sealed_schema_digest=sealed_digest, observed_schema_digest=observed)

    if subject in ('database bytes', 'file.json'):
        observed = materials['file.json'].get('sha256')
        return _evidence(observed == certificate['sha256'],
                         '服务端只持有交付证书摘要；现场字节摘要的真伪以 manifest 摘要链为界。',
                         certificate_sha256=certificate['sha256'], observed_sha256=observed)

    if subject == 'integrity_check':
        return _evidence(None, '完整性检查只能在现场库执行，服务端无法独立复算，需管理员现场确认。')

    if subject == 'foreign keys':
        observed = schema_material.get('edges')
        return _evidence(observed == sealed['schema']['edges'],
                         '服务端从封存目录重算外键清单并与现场 edges 对照。',
                         sealed_edges=sealed['schema']['edges'], observed_edges=observed)

    if subject.startswith('index:'):
        name = subject[len('index:'):]
        sealed_index = next((_index_outline(i) for i in sealed['schema']['indexes']
                             if i['name'] == name), None)
        observed_index = next((i for i in (schema_material.get('indexes') or [])
                               if isinstance(i, dict) and i.get('name') == name), None)
        holds = (sealed_index is not None and observed_index is not None
                 and sealed_index.get('sql') == observed_index.get('sql'))
        return _evidence(holds, '服务端从封存索引定义重算 CREATE INDEX 文本并与现场索引对照。',
                         sealed_index=sealed_index, observed_index=observed_index)

    # Table- and row-level subjects share the prefix table:<name>
    if subject.startswith('table:'):
        return _table_evidence(subject, sealed, tables_material, schema_material)

    if subject in ('schema.json', 'schema.json:indexes', 'tables.json'):
        return _evidence(None, '该结论表示对应材料缺失或形状无效，服务端无法在缺少材料时独立复算，需补全材料或现场复查。')

    return _evidence(None, '该结论的独立复查规则未登记，需现场判断。')


def _table_evidence(subject, sealed, tables_material, schema_material):
    parts = subject.split(':')
    name = parts[1]
    kind = parts[2] if len(parts) > 2 else ''
    sealed_tables_map = sealed_tables(sealed)
    if name not in sealed_tables_map:
        return _evidence(None, '该表不在封存交付范围内，服务端无封存行可复算，需现场核对来源。', table=name)
    expected = sealed_tables_map[name]
    observed = (tables_material.get('tables') or {}).get(name)

    if kind == 'count':
        observed_count = observed.get('count') if isinstance(observed, dict) else None
        return _evidence(observed_count == expected['count'],
                         '服务端从封存行独立计数并与现场 tables.json 行数对照。',
                         sealed_count=expected['count'], observed_count=observed_count)

    if kind == 'row':
        ref = parts[3]
        if not valid_ref(ref):
            raise Fault('invalid_revisit', '结论编号中的行键摘要无效。', 400)
        key_index = sealed_key_index(sealed).get(name, {})
        sealed_row_digests = expected['row_digests']
        observed_rows = observed.get('row_digests') if isinstance(observed, dict) else None
        token = key_index.get(ref)
        key_display = loads(token) if token is not None else None
        if not isinstance(observed_rows, dict):
            return _evidence(None, '现场材料缺少逐行摘要，无法独立定位该行，需现场按主键 SELECT 复查。',
                             table=name, key=key_display)
        sealed_present = ref in sealed_row_digests
        observed_present = ref in observed_rows
        if sealed_present and observed_present:
            holds = sealed_row_digests[ref] == observed_rows[ref]
            basis = '服务端从封存行重算该行的整行摘要（不含行原文外发），与现场行摘要对照。'
        elif sealed_present and not observed_present:
            holds = False
            basis = '封存记录确认该主键原本入选；现场逐行摘要中缺失，可独立证实行被移除。'
        elif not sealed_present and observed_present:
            holds = False
            basis = '封存记录不含该主键；现场逐行摘要出现它，可独立证实为新增行。'
        else:
            holds = None
            basis = '两侧逐行摘要都不含该行键摘要，无法独立复算其状态。'
        return _evidence(holds, basis, table=name, key=key_display, key_ref=ref,
                         sealed_row_digest=sealed_row_digests.get(ref),
                         observed_row_digest=observed_rows.get(ref))

    if kind in ('ddl', 'columns'):
        sealed_table = sealed['schema']['tables'][name]
        observed_table = (schema_material.get('tables') or {}).get(name)
        if not isinstance(observed_table, dict):
            return _evidence(None, '现场材料缺少该表定义，无法独立对照，需现场 PRAGMA 复查。', table=name)
        if kind == 'ddl':
            holds = observed_table.get('ddl') == sealed_table['ddl']
            basis = '服务端从封存目录取出表 DDL 文本，与现场 schema.json 的 DDL 逐字符对照。'
            return _evidence(holds, basis, table=name, sealed_ddl=sealed_table['ddl'],
                             observed_ddl=observed_table.get('ddl'))
        holds = observed_table.get('columns') == sealed_table['columns']
        return _evidence(holds, '服务端从封存目录取出列元数据，与现场 table_xinfo 材料逐字段对照。',
                         table=name, sealed_columns=sealed_table['columns'],
                         observed_columns=observed_table.get('columns'))

    # Whole-table equivalence or weak/unverified table finding
    if not isinstance(observed, dict):
        return _evidence(None, '现场缺少该表的逐表材料，服务端无法独立对照，需补全材料或现场复查。', table=name)
    holds = (observed.get('row_digests') == expected['row_digests']
             and observed.get('count') == expected['count']
             and observed.get('table_digest') == expected['table_digest'])
    return _evidence(holds, '服务端从封存行重算逐行摘要、整表摘要与计数，整体与现场 tables.json 对照。',
                     table=name, sealed_count=expected['count'],
                     sealed_table_digest=expected['table_digest'],
                     observed_table_digest=observed.get('table_digest'))

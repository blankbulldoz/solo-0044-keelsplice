"""Rebuild the cross-version reconciliation statement for two statistics groups.

Group A holds a certified sub-library captured from the v1 source schema and
group B one captured from the v2 schema.  The v2 schema only adds a column to
``frame`` but also switches the composite primary key of ``substrate`` from
``COLLATE NOCASE`` to ``COLLATE BINARY``, so the built-in compare endpoint
refuses the pair with ``409 schema_conflict``.  This script regenerates both
certified fixtures through the real HTTP service, records the refusal, runs
representative statistics on the delivered files through independent
read-only connections, and writes the reconciliation statement together with
machine-checkable evidence.

Run from the repository root:

    python3 -m examples.reconcile_handoff [output_dir]
"""

import json
import sqlite3
import sys
from contextlib import closing
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread

from keelsplice.api import create_server

from .walkthrough import call, seed

# v2 source: same business data as examples/microscope.sql, except
#   1. frame gains a column (purely additive change);
#   2. substrate keeps the composite primary key (region, code) but switches
#      both components from COLLATE NOCASE to COLLATE BINARY, so spellings
#      that used to fold into one identity are now distinct identities;
#   3. assay S-02 has a corrected length (5.5 -> 5.75) and assay S-04 has its
#      foreign key spelling normalized ('west','basalt' -> 'West','Basalt')
#      so it still resolves under BINARY comparison.
V2_SQL = '''
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
CREATE TABLE substrate (
    region TEXT COLLATE BINARY NOT NULL,
    code TEXT COLLATE BINARY NOT NULL,
    texture BLOB NOT NULL,
    PRIMARY KEY (region, code)
) WITHOUT ROWID;
CREATE TABLE cassette (
    serial INTEGER PRIMARY KEY,
    prior INTEGER REFERENCES cassette,
    label TEXT NOT NULL
);
CREATE TABLE assay (
    mark TEXT PRIMARY KEY NOT NULL,
    region TEXT,
    code TEXT,
    cassette INTEGER NOT NULL REFERENCES cassette,
    length_um REAL NOT NULL,
    doubled REAL GENERATED ALWAYS AS (length_um * 2) STORED,
    FOREIGN KEY (region, code) REFERENCES substrate
);
CREATE TABLE frame (
    frame_id INTEGER PRIMARY KEY,
    mark TEXT REFERENCES assay,
    note TEXT NOT NULL,
    caption TEXT
);
CREATE INDEX assay_substrate_lookup ON assay(region,code) WHERE code IS NOT NULL;
CREATE INDEX frame_mark_lookup ON frame(mark);
BEGIN;
PRAGMA defer_foreign_keys=ON;
INSERT INTO substrate VALUES
    ('East','Mica',x'0000ff'), ('east','MICA',x'00ff00'),
    ('EAST','mica',x'ff0000'), ('West','Basalt',x'fe');
INSERT INTO cassette VALUES (11,12,'opal'), (12,11,'agate'), (13,NULL,'garnet');
INSERT INTO assay(mark,region,code,cassette,length_um) VALUES
    ('S-01','east','MICA',11,3.25), ('S-02','EAST','mica',12,5.75),
    ('S-03','West',NULL,13,2.5), ('S-04','West','Basalt',13,9.0);
INSERT INTO frame(frame_id,mark,note,caption) VALUES
    (101,'S-01','front','verified'), (102,'S-02','detail',NULL),
    (103,'S-02','side','reimage'), (104,NULL,'blank',NULL);
COMMIT;
'''

# Representative statistics and probes.  Each entry: (id, title, sql, scope).
# scope 'both' runs on each delivered fixture; 'v2' is a metric that only the
# new schema can answer (the old file is expected to raise an error).
QUERIES = [
    ('Q-INTEG', '完整性校验', 'PRAGMA integrity_check', 'both'),
    ('Q-FKCHECK', '外键一致性校验', 'PRAGMA foreign_key_check', 'both'),
    ('Q-DDL', '全部表定义', "SELECT name, sql FROM sqlite_schema WHERE type='table' ORDER BY name", 'both'),
    ('Q-FRAME-COLS', 'frame 列清单（新增列证据）', 'PRAGMA table_info(frame)', 'both'),
    ('Q-ROWCOUNTS', '分表行数',
     "SELECT 'substrate' AS tab, count(*) AS n FROM substrate"
     " UNION ALL SELECT 'assay', count(*) FROM assay"
     " UNION ALL SELECT 'cassette', count(*) FROM cassette"
     " UNION ALL SELECT 'frame', count(*) FROM frame", 'both'),
    ('Q-LEN-AGG', 'assay.length_um 聚合',
     'SELECT count(*) AS n, total(length_um) AS total_um, avg(length_um) AS avg_um,'
     ' min(length_um) AS min_um, max(length_um) AS max_um FROM assay', 'both'),
    ('Q-CASSETTE', '各 cassette 的 assay 数',
     'SELECT c.serial, c.label, count(a.mark) AS assays FROM cassette c'
     ' LEFT JOIN assay a ON a.cassette = c.serial GROUP BY c.serial, c.label ORDER BY c.serial', 'both'),
    ('Q-FRAME-PER-ASSAY', '各 assay 的 frame 数',
     'SELECT a.mark, count(f.frame_id) AS frames FROM assay a'
     ' LEFT JOIN frame f ON f.mark = a.mark GROUP BY a.mark ORDER BY a.mark', 'both'),
    ('Q-PER-SUBSTRATE', '各 substrate 身份的 assay 数',
     # Parent column on the left so the substrate collation governs the join,
     # matching the identity semantics the workbench itself uses.
     'SELECT s.region, s.code, count(a.mark) AS assays FROM substrate s'
     ' LEFT JOIN assay a ON s.region = a.region AND s.code = a.code'
     ' GROUP BY s.region, s.code ORDER BY s.region, s.code', 'both'),
    ('Q-SUBSTRATE-ROWS', 'substrate 身份与 texture 清单',
     'SELECT region, code, hex(texture) AS texture_hex FROM substrate ORDER BY region, code', 'both'),
    ('Q-ASSAY-ROWS', 'assay 行级内容（含外键拼写）',
     'SELECT mark, region, code, cassette, length_um, doubled FROM assay ORDER BY mark', 'both'),
    ('Q-FRAME-ROWS', 'frame 旧列行级内容',
     'SELECT frame_id, mark, note FROM frame ORDER BY frame_id', 'both'),
    ('Q-PROBE-NOCASE', '排序规则探针：EaSt/MiCa 命中数',
     "SELECT count(*) AS n FROM substrate WHERE region = 'EaSt' AND code = 'MiCa'", 'both'),
    ('Q-PROBE-FOLD', '折叠旁证：East/Mica 族 assay 总数',
     "SELECT count(*) AS n FROM assay WHERE region = 'east' COLLATE NOCASE"
     " AND code = 'mica' COLLATE NOCASE", 'both'),
    ('Q-CAPTION-NONNULL', 'v2 独有口径：caption 非空 frame 数',
     'SELECT count(*) AS n FROM frame WHERE caption IS NOT NULL', 'v2'),
]

FIXTURES = {'microscope_v1': 'group_a_v1.sqlite', 'microscope_v2': 'group_b_v2.sqlite'}
GROUP = {'microscope_v1': 'A 组（v1 结构）', 'microscope_v2': 'B 组（v2 结构）'}


def produce(base):
    """Run the real service: recipes, captures, build, certify, download, compare."""
    catalogs = {alias: call(base, f'/sources/{alias}/catalog') for alias in FIXTURES}

    def edge(alias, child, parent):
        return next(e for e in catalogs[alias]['schema']['edges']
                    if e['child'] == child and e['parent'] == parent)

    roots = {
        'microscope_v1': [{'table': 'substrate', 'key': ['East', 'Mica']},
                          {'table': 'substrate', 'key': ['West', 'Basalt']}],
        # v2 下 East/Mica 族已拆成三个实存身份，配方按各版实存键逐一填写。
        'microscope_v2': [{'table': 'substrate', 'key': ['East', 'Mica']},
                          {'table': 'substrate', 'key': ['east', 'MICA']},
                          {'table': 'substrate', 'key': ['EAST', 'mica']},
                          {'table': 'substrate', 'key': ['West', 'Basalt']}],
    }
    produced = {}
    for number, alias in enumerate(FIXTURES, start=1):
        follow = [{'child': 'assay', 'id': edge(alias, 'assay', 'substrate')['id']},
                  {'child': 'frame', 'id': edge(alias, 'frame', 'assay')['id']}]
        version = call(base, '/recipes/swatch/versions',
                       {'source': alias, 'roots': roots[alias], 'follow_children': follow,
                        'row_limit': 50}, 201)
        assert version['version'] == number
        cap = call(base, '/captures', {'name': 'swatch', 'version': number}, 201)
        cid = cap['id']
        assert call(base, f'/captures/{cid}/build', {})['state'] == 'built'
        certificate = call(base, f'/captures/{cid}/certify', {})['certificate']
        data, etag = call(base, f'/captures/{cid}/database')
        assert sha256(data).hexdigest() == etag == certificate['sha256']
        produced[alias] = {'capture_id': cid, 'recipe_version': number,
                           'certificate': certificate, 'data': data}
    pair = {'left': produced['microscope_v1']['capture_id'],
            'right': produced['microscope_v2']['capture_id']}
    rejection = call(base, '/compare', pair, 409)
    assert rejection['error'] == 'schema_conflict'
    produced['rejection'] = {'request': pair, 'status': 409, 'body': rejection}
    return produced


def run_queries(path):
    """Open a delivered fixture read-only and run every representative query."""
    results = {}
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as conn:
        for qid, _title, sql, _scope in QUERIES:
            try:
                cursor = conn.execute(sql)
                columns = [item[0] for item in cursor.description]
                results[qid] = {'columns': columns, 'rows': [list(row) for row in cursor.fetchall()]}
            except sqlite3.Error as exc:
                results[qid] = {'error': str(exc)}
    return results


def rows(result, qid):
    entry = result[qid]
    assert 'rows' in entry, (qid, entry)
    return [dict(zip(entry['columns'], row)) for row in entry['rows']]


def cell(value):
    return f'`{value!r}`' if isinstance(value, float) else f'`{value}`'


def reconcile(out, produced):
    fixtures = out / 'fixtures'
    evidence = out / 'evidence'
    fixtures.mkdir(parents=True, exist_ok=True)
    evidence.mkdir(parents=True, exist_ok=True)

    stats = {}
    for alias, item in produced.items():
        if alias == 'rejection':
            continue
        name = FIXTURES[alias]
        path = fixtures / name
        path.write_bytes(item['data'])
        wrapper = {'capture_id': item['capture_id'],
                   'recipe': {'name': 'swatch', 'version': item['recipe_version']},
                   'source_alias': alias, 'fixture_file': name,
                   'certificate': item['certificate']}
        (fixtures / name.replace('.sqlite', '.certificate.json')).write_text(
            json.dumps(wrapper, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        stats[alias] = run_queries(path)

    (evidence / 'compare_rejection.json').write_text(
        json.dumps(produced['rejection'], ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    (evidence / 'queries.sql').write_text('\n\n'.join(
        f'-- {qid}：{title}' + ('（仅 v2 可执行）' if scope == 'v2' else '') + f'\n{sql};'
        for qid, title, sql, scope in QUERIES) + '\n', encoding='utf-8')
    for alias in FIXTURES:
        payload = {'fixture': FIXTURES[alias], 'sha256': produced[alias]['certificate']['sha256'],
                   'results': stats[alias]}
        (evidence / f'stats_{FIXTURES[alias].replace(".sqlite", "")}.json').write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')

    a, b = stats['microscope_v1'], stats['microscope_v2']

    # --- structural exhibits -------------------------------------------------
    ddl_a = {r['name']: r['sql'] for r in rows(a, 'Q-DDL')}
    ddl_b = {r['name']: r['sql'] for r in rows(b, 'Q-DDL')}
    frame_cols_a = [r['name'] for r in rows(a, 'Q-FRAME-COLS')]
    frame_cols_b = [r['name'] for r in rows(b, 'Q-FRAME-COLS')]
    assert frame_cols_a == ['frame_id', 'mark', 'note']
    assert frame_cols_b == ['frame_id', 'mark', 'note', 'caption']
    assert 'COLLATE NOCASE' in ddl_a['substrate'] and 'COLLATE BINARY' in ddl_b['substrate']
    assert ddl_a['assay'] == ddl_b['assay'] and ddl_a['cassette'] == ddl_b['cassette']
    structure = {
        'frame': {'change': '仅新增列 caption TEXT；旧列与行内容不变',
                  'v1_columns': frame_cols_a, 'v2_columns': frame_cols_b,
                  'v1_ddl': ddl_a['frame'], 'v2_ddl': ddl_b['frame']},
        'substrate': {'change': '复合主键 (region, code) 排序规则 COLLATE NOCASE → COLLATE BINARY',
                      'v1_ddl': ddl_a['substrate'], 'v2_ddl': ddl_b['substrate']},
        'unchanged_tables': ['assay', 'cassette'],
    }
    (evidence / 'structure_diff.json').write_text(
        json.dumps(structure, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')

    # --- statistic values ----------------------------------------------------
    counts_a = {r['tab']: r['n'] for r in rows(a, 'Q-ROWCOUNTS')}
    counts_b = {r['tab']: r['n'] for r in rows(b, 'Q-ROWCOUNTS')}
    agg_a, agg_b = rows(a, 'Q-LEN-AGG')[0], rows(b, 'Q-LEN-AGG')[0]
    cassette_a = {r['serial']: r for r in rows(a, 'Q-CASSETTE')}
    cassette_b = {r['serial']: r for r in rows(b, 'Q-CASSETTE')}
    frames_a = {r['mark']: r['frames'] for r in rows(a, 'Q-FRAME-PER-ASSAY')}
    frames_b = {r['mark']: r['frames'] for r in rows(b, 'Q-FRAME-PER-ASSAY')}
    per_sub_a = {(r['region'], r['code']): r['assays'] for r in rows(a, 'Q-PER-SUBSTRATE')}
    per_sub_b = {(r['region'], r['code']): r['assays'] for r in rows(b, 'Q-PER-SUBSTRATE')}
    sub_rows_a = {(r['region'], r['code']): r['texture_hex'] for r in rows(a, 'Q-SUBSTRATE-ROWS')}
    sub_rows_b = {(r['region'], r['code']): r['texture_hex'] for r in rows(b, 'Q-SUBSTRATE-ROWS')}
    assay_a = {r['mark']: r for r in rows(a, 'Q-ASSAY-ROWS')}
    assay_b = {r['mark']: r for r in rows(b, 'Q-ASSAY-ROWS')}
    probe_a = rows(a, 'Q-PROBE-NOCASE')[0]['n']
    probe_b = rows(b, 'Q-PROBE-NOCASE')[0]['n']
    fold_a = rows(a, 'Q-PROBE-FOLD')[0]['n']
    fold_b = rows(b, 'Q-PROBE-FOLD')[0]['n']

    # --- expected-shape assertions (fail loudly rather than misclassify) -----
    assert counts_a == {'substrate': 2, 'assay': 3, 'cassette': 3, 'frame': 3}
    assert counts_b == {'substrate': 4, 'assay': 3, 'cassette': 3, 'frame': 3}
    assert produced['microscope_v1']['certificate']['counts'] == counts_a
    assert produced['microscope_v2']['certificate']['counts'] == counts_b
    assert rows(a, 'Q-INTEG')[0]['integrity_check'] == rows(b, 'Q-INTEG')[0]['integrity_check'] == 'ok'
    assert rows(a, 'Q-FKCHECK') == rows(b, 'Q-FKCHECK') == []
    assert assay_a['S-02']['length_um'] == 5.5 and assay_b['S-02']['length_um'] == 5.75
    assert assay_a['S-02']['doubled'] == 11.0 and assay_b['S-02']['doubled'] == 11.5
    assert (probe_a, probe_b) == (1, 0) and (fold_a, fold_b) == (2, 2)
    assert per_sub_a == {('East', 'Mica'): 2, ('West', 'Basalt'): 1}
    assert per_sub_b == {('East', 'Mica'): 0, ('east', 'MICA'): 1,
                         ('EAST', 'mica'): 1, ('West', 'Basalt'): 1}
    assert 'error' in a['Q-CAPTION-NONNULL'] and 'caption' in a['Q-CAPTION-NONNULL']['error']
    assert rows(b, 'Q-CAPTION-NONNULL')[0]['n'] == 2

    # --- bucket 1: mergeable --------------------------------------------------
    merged = [
        ('M01', 'assay 行数', "Q-ROWCOUNTS（tab='assay' 行）", counts_a['assay']),
        ('M02', 'cassette 行数', "Q-ROWCOUNTS（tab='cassette' 行）", counts_a['cassette']),
        ('M03', 'frame 行数', "Q-ROWCOUNTS（tab='frame' 行）", counts_a['frame']),
        ('M04', 'assay.length_um 最小值', 'Q-LEN-AGG（min_um 列）', agg_a['min_um']),
        ('M05', 'assay.length_um 最大值', 'Q-LEN-AGG（max_um 列）', agg_a['max_um']),
        ('M06', 'cassette 11（opal）的 assay 数', 'Q-CASSETTE（serial=11 行）', cassette_a[11]['assays']),
        ('M07', 'cassette 12（agate）的 assay 数', 'Q-CASSETTE（serial=12 行）', cassette_a[12]['assays']),
        ('M08', 'cassette 13（garnet）的 assay 数', 'Q-CASSETTE（serial=13 行）', cassette_a[13]['assays']),
        ('M09', 'assay S-01 的 frame 数', "Q-FRAME-PER-ASSAY（mark='S-01' 行）", frames_a['S-01']),
        ('M10', 'assay S-02 的 frame 数', "Q-FRAME-PER-ASSAY（mark='S-02' 行）", frames_a['S-02']),
        ('M11', 'assay S-04 的 frame 数', "Q-FRAME-PER-ASSAY（mark='S-04' 行）", frames_a['S-04']),
        ('M12', "substrate ('West','Basalt') 的 assay 数", "Q-PER-SUBSTRATE（region='West' 行）",
         per_sub_a[('West', 'Basalt')]),
        ('M13', "substrate ('West','Basalt') 的 texture", "Q-SUBSTRATE-ROWS（region='West' 行）",
         "x'" + sub_rows_a[('West', 'Basalt')] + "'"),
    ]
    checks = [(counts_a['assay'], counts_b['assay']), (counts_a['cassette'], counts_b['cassette']),
              (counts_a['frame'], counts_b['frame']), (agg_a['min_um'], agg_b['min_um']),
              (agg_a['max_um'], agg_b['max_um']),
              (cassette_a[11]['assays'], cassette_b[11]['assays']),
              (cassette_a[12]['assays'], cassette_b[12]['assays']),
              (cassette_a[13]['assays'], cassette_b[13]['assays']),
              (frames_a['S-01'], frames_b['S-01']), (frames_a['S-02'], frames_b['S-02']),
              (frames_a['S-04'], frames_b['S-04']),
              (per_sub_a[('West', 'Basalt')], per_sub_b[('West', 'Basalt')]),
              (sub_rows_a[('West', 'Basalt')], sub_rows_b[('West', 'Basalt')])]
    assert all(left == right for left, right in checks)

    # --- bucket 2: same identity, changed value -------------------------------
    changed = [
        ('C01', 'substrate 行数', "Q-ROWCOUNTS（tab='substrate' 行）",
         counts_a['substrate'], counts_b['substrate'],
         "v2 身份拆分新增 ('east','MICA')、('EAST','mica') 两行；行清单见 Q-SUBSTRATE-ROWS，身份影响见第 5 节"),
        ('C02', 'assay.length_um 合计', 'Q-LEN-AGG（total_um 列）',
         agg_a['total_um'], agg_b['total_um'],
         "S-02 长度 5.5 → 5.75；行级依据 Q-ASSAY-ROWS 中 mark='S-02' 行（生成列 doubled 11.0 → 11.5 同步）"),
        ('C03', 'assay.length_um 均值', 'Q-LEN-AGG（avg_um 列）',
         agg_a['avg_um'], agg_b['avg_um'], '同 C02'),
    ]

    # --- bucket 3: identity can no longer be aligned --------------------------
    east_family = [('East', 'Mica'), ('east', 'MICA'), ('EAST', 'mica')]

    # --- report ---------------------------------------------------------------
    cert_a, cert_b = (produced[alias]['certificate'] for alias in FIXTURES)
    line = []
    line.append('# 跨版子库对账表：A 组（v1 结构）× B 组（v2 结构）')
    line.append('')
    line.append(f"生成时间（UTC）：{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')}；"
                '由 `python3 -m examples.reconcile_handoff` 重新生成可整体复算。')
    line.append('')
    line.append('## 1. 交付材料清单')
    line.append('')
    line.append('| 组别 | 文件 | schema_digest | SHA-256 | 字节 | 捕获 ID | 配方 |')
    line.append('| --- | --- | --- | --- | --- | --- | --- |')
    for alias, cert in (('microscope_v1', cert_a), ('microscope_v2', cert_b)):
        item = produced[alias]
        line.append(f"| {GROUP[alias]} | `fixtures/{FIXTURES[alias]}` | `{cert['schema_digest'][:16]}…` "
                    f"| `{cert['sha256'][:16]}…` | {cert['bytes']} | `{item['capture_id']}` "
                    f"| swatch v{item['recipe_version']} |")
    line.append('')
    line.append('每份 `fixtures/*.certificate.json` 记录捕获 ID、配方版本与完整认证摘要。'
                '接收方先核对文件 SHA-256，再用独立 SQLite 连接只读打开复算（见第 7 节）。')
    line.append('')
    line.append('## 2. 结构差异与原比较接口拒绝实录')
    line.append('')
    line.append('两版子库存在两处结构差异（机读对照见 `evidence/structure_diff.json`，原始 DDL 见 Q-DDL）：')
    line.append('')
    line.append('1. `frame` **仅新增列** `caption TEXT`（Q-FRAME-COLS：'
                f"A 侧 {frame_cols_a}，B 侧 {frame_cols_b}），旧列与行内容不变；")
    line.append('2. `substrate` 复合主键 `(region, code)` **排序规则更换**：'
                '`COLLATE NOCASE` → `COLLATE BINARY`，大小写折叠身份不再成立。')
    line.append('')
    line.append('```sql')
    line.append('-- A 组（v1）substrate 定义')
    line.append(ddl_a['substrate'] + ';')
    line.append('-- B 组（v2）substrate 定义')
    line.append(ddl_b['substrate'] + ';')
    line.append('```')
    line.append('')
    line.append('因此原比较接口 `POST /compare` 按同模式前提拒绝两库：'
                f"HTTP {produced['rejection']['status']} "
                f"`{produced['rejection']['body']['error']}`"
                '（请求与响应实录见 `evidence/compare_rejection.json`）。'
                '本对账表改为在两份交付文件上直接对账。')
    line.append('')
    line.append('## 3. 可合并的统计（两边数值一致）')
    line.append('')
    line.append('| 编号 | 统计 | 口径（查询依据） | 共同值 |')
    line.append('| --- | --- | --- | --- |')
    for mid, title, basis, value in merged:
        line.append(f'| {mid} | {title} | {basis} | {cell(value)} |')
    line.append('')
    line.append('## 4. 值有变化的统计（身份可对齐、数值不同）')
    line.append('')
    line.append('| 编号 | 统计 | 口径（查询依据） | A 组（v1） | B 组（v2） | 差异原因与行级依据 |')
    line.append('| --- | --- | --- | --- | --- | --- |')
    for cid, title, basis, va, vb, why in changed:
        line.append(f'| {cid} | {title} | {basis} | {cell(va)} | {cell(vb)} | {why} |')
    line.append('')
    line.append('## 5. 身份不足以对齐的统计')
    line.append('')
    line.append('**X01　各 substrate 身份的 assay 数（East/Mica 族）**　口径：Q-PER-SUBSTRATE。')
    line.append('')
    line.append("| 身份 (region, code) | A 组（v1，NOCASE 折叠） | B 组（v2，BINARY 拆分） |")
    line.append('| --- | --- | --- |')
    for ident in east_family:
        va = per_sub_a.get(ident)
        vb = per_sub_b.get(ident)
        line.append(f"| `('{ident[0]}','{ident[1]}')` | "
                    + (f'**{va}**（折叠族合计）' if ident == ('East', 'Mica') else '—（折叠于上行）')
                    + f' | {vb} |')
    line.append('')
    line.append('v1 中 `(\'East\',\'Mica\')` 一个身份折叠了全部大小写拼写（S-01、S-02 均挂其下，'
                '见 Q-ASSAY-ROWS 的 region/code 列）；v2 改为 BINARY 后同一族拆成三个实存身份，'
                '旧身份与任一新身份之间不存在一一对应，**该族按身份的统计无法逐项对齐**。')
    line.append('')
    line.append('独立核对依据：')
    line.append('')
    line.append(f"- 排序规则探针 Q-PROBE-NOCASE（`region='EaSt' AND code='MiCa'` 命中数）："
                f'A 组 {probe_a}，B 组 {probe_b} —— 同一查询在两版语义不同；')
    line.append(f'- 折叠旁证 Q-PROBE-FOLD（显式 `COLLATE NOCASE` 折叠后的族 assay 总数）：'
                f'A 组 {fold_a}，B 组 {fold_b} —— 族合计一致，仅证明数据未丢失，不能恢复逐项对应；')
    line.append('- 身份清单 Q-SUBSTRATE-ROWS：A 组 2 个身份，B 组 4 个身份；')
    line.append('- 外键拼写 Q-ASSAY-ROWS：S-01 `(\'east\',\'MICA\')`、S-02 `(\'EAST\',\'mica\')` '
                '两侧行内容逐字节相同，但 v2 中改挂新身份；')
    line.append('- 结构证据 Q-DDL / `evidence/structure_diff.json`：排序规则 NOCASE → BINARY。')
    line.append('')
    line.append('## 6. 口径备注')
    line.append('')
    line.append('- `frame.caption` 为 v2 新增列，A 侧无此口径：Q-CAPTION-NONNULL 仅 B 侧可算'
                f"（B 侧 {rows(b, 'Q-CAPTION-NONNULL')[0]['n']} 行非空），"
                'A 侧执行报错 `no such column: caption`（见 stats 证据文件），不参与三桶分类；')
    line.append("- assay S-03（code 为 NULL）与 frame 104（mark 为 NULL）按闭包规则不入选，"
                '两版一致；本表为子库对子库，不对源库全量；')
    line.append("- assay S-04 外键拼写在 v2 规范化（`('west','basalt')` → `('West','Basalt')`），"
                "身份 ('West','Basalt') 两侧可对齐，相关统计见 M12、M13。")
    line.append('')
    line.append('## 7. 复验指引')
    line.append('')
    line.append('1. 完整性：`sha256sum fixtures/*.sqlite`，对照第 1 节与证书中的 SHA-256；')
    line.append('2. 可打开性：用独立 SQLite 连接只读打开两份 fixture，'
                'Q-INTEG 应返回 `ok`，Q-FKCHECK 应为空（两侧均已实测）；')
    line.append('3. 逐项复算：`evidence/queries.sql` 中每条查询带 Q- 编号，'
                '在对应 fixture 上执行后与 `evidence/stats_group_a_v1.json` / '
                '`evidence/stats_group_b_v2.json` 中同编号结果逐行比对，'
                '即可独立核对本表每一项；')
    line.append('4. 整体重算：在仓库根目录执行 `python3 -m examples.reconcile_handoff`，'
                '会重新走「配方 → 封存 → 构建 → 认证 → 下载」全流程并重建本目录。')
    line.append('')
    (out / 'RECONCILIATION.md').write_text('\n'.join(line), encoding='utf-8')
    return len(merged), len(changed), 1


def main():
    out = Path(sys.argv[1] if len(sys.argv) > 1 else 'reconciliation').resolve()
    with TemporaryDirectory(prefix='keelsplice-reconcile-') as directory:
        root = Path(directory)
        sources = {}
        v1 = root / 'source_v1.sqlite'
        seed(v1)
        sources['microscope_v1'] = v1
        v2 = root / 'source_v2.sqlite'
        with closing(sqlite3.connect(v2)) as conn:
            conn.executescript(V2_SQL)
        sources['microscope_v2'] = v2
        with create_server(('127.0.0.1', 0), root / 'repo.sqlite', sources) as server:
            thread = Thread(target=server.serve_forever, kwargs={'poll_interval': 0.05})
            thread.start()
            try:
                produced = produce(f'http://127.0.0.1:{server.server_port}')
            finally:
                server.shutdown()
                thread.join()
    merged, changed, identity = reconcile(out, produced)
    print(f'对账材料已生成：{out}')
    print(f'  可合并 {merged} 项 / 值有变化 {changed} 项 / 身份不足以对齐 {identity} 项')
    print('  原比较接口拒绝实录：evidence/compare_rejection.json（409 schema_conflict）')


if __name__ == '__main__':
    main()

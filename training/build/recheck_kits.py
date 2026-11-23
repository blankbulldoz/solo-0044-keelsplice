"""盲审训练卷的独立复核工具（不依赖 Keelsplice 工作台进程）。

用法（在仓库根目录，路径以参数传入，建议隔离模式运行）：
    python3 -I training/build/recheck_kits.py --kit training/generated/alpha
    python3 -I training/build/recheck_kits.py --kit training/generated/b1 \
        --kit training/generated/b2 --pair training/generated/b1 training/generated/b2

脚本只用标准库打开各交接包内的独立 SQLite 与证书材料，输出学员可以
亲自观察到的事实，不给出“应该交接/暂缓”的结论——结论由学员在答卷中
依据这些证据自行作出，评分判据见 training/grading/。
"""

import argparse
import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path

CANONICAL = dict(ensure_ascii=True, sort_keys=True, separators=(',', ':'))


def canonical_digest(value):
    return hashlib.sha256(json.dumps(value, **CANONICAL).encode('ascii')).hexdigest()


def read_json(file):
    return json.loads(Path(file).read_text())


def file_sha256(file):
    return hashlib.sha256(Path(file).read_bytes()).hexdigest()


def open_ro(file):
    return sqlite3.connect(Path(file).resolve().as_uri() + '?mode=ro', uri=True)


def table_names(conn):
    return [row[0] for row in conn.execute(
        "SELECT name FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]


def logical_fingerprint(file):
    """逻辑内容指纹：表、列与按主键排序的行，与物理页布局无关。"""
    with closing(open_ro(file)) as conn:
        conn.row_factory = sqlite3.Row
        structure, rows = {}, {}
        for table in table_names(conn):
            cols = [r['name'] for r in conn.execute(f'PRAGMA table_xinfo("{table}")')]
            structure[table] = cols
            rows[table] = sorted(map(list, conn.execute(f'SELECT * FROM "{table}"')))
    return canonical_digest({'columns': structure, 'rows': rows})


def check_kit(kit_dir):
    kit_dir = Path(kit_dir)
    findings = {'kit': kit_dir.name, 'checks': []}

    def record(item, ok, detail):
        findings['checks'].append({'check': item, 'ok': bool(ok), 'evidence': detail})

    manifest_file = kit_dir / 'handoff_manifest.json'
    manifest = read_json(manifest_file) if manifest_file.exists() else {}
    record('handoff_manifest_present', manifest_file.exists(), manifest)

    cert_file = kit_dir / 'certificate.json'
    deliverable = kit_dir / 'deliverable.sqlite'
    state_copy = kit_dir / 'state_copy.sqlite'
    cert = read_json(cert_file) if cert_file.exists() else None
    record('certificate_file_present', cert is not None,
           {'path': cert_file.name} if cert else '缺少 certificate.json')
    record('deliverable_file_present', deliverable.exists(),
           {'path': deliverable.name} if deliverable.exists() else '缺少 deliverable.sqlite')

    # 1) 文件字节与证书 SHA-256。
    if cert and deliverable.exists():
        actual_sha = file_sha256(deliverable)
        record('file_sha256_matches_certificate', actual_sha == cert['sha256'],
               {'recomputed': actual_sha, 'certificate': cert['sha256']})
        record('certificate_basic_fields',
               all(k in cert for k in ('sha256', 'bytes', 'schema_digest', 'capture_digest', 'counts')),
               {k: cert.get(k) for k in ('bytes', 'schema_digest', 'capture_digest', 'counts')})

    # 2) 独立 SQLite 完整性与外键。
    if deliverable.exists():
        with closing(open_ro(deliverable)) as conn:
            integrity = conn.execute('PRAGMA integrity_check').fetchall()
            fk_violations = conn.execute('PRAGMA foreign_key_check').fetchall()
            page_size = conn.execute('PRAGMA page_size').fetchone()[0]
            project_rows = [r[0] for r in conn.execute(
                "SELECT proj_id FROM project ORDER BY proj_id")] if 'project' in table_names(conn) else []
        record('sqlite_integrity_ok', integrity == [('ok',)], {'integrity_check': integrity})
        record('foreign_key_check_clean', fk_violations == [], {'violations': fk_violations})
        findings['physical'] = {'page_size': page_size, 'project_rows': project_rows}

    # 3) 状态库封存记录指纹 vs 证书 capture_digest（捕获归属核对）。
    with closing(open_ro(state_copy)) as conn:
        conn.row_factory = sqlite3.Row
        cap_rows = [dict(r) for r in conn.execute(
            "SELECT id, name, version, state, sealed, certificate FROM captures ORDER BY rowid")]
        recipe_rows = [dict(r) for r in conn.execute(
            "SELECT name, version, active FROM recipes ORDER BY rowid")]
    findings['staged_captures'] = [
        {'id': r['id'], 'name': r['name'], 'version': r['version'], 'state': r['state']} for r in cap_rows]
    findings['recipes'] = recipe_rows
    record('exactly_one_staged_capture', len(cap_rows) == 1, {'count': len(cap_rows)})

    if len(cap_rows) == 1:
        staged = cap_rows[0]
        sealed = json.loads(staged['sealed'])
        sealed_fp = canonical_digest(sealed)
        sealed_roots = sealed['result']['roots']
        findings['sealed'] = {'state': staged['state'], 'roots': sealed_roots,
                              'recomputed_capture_digest': sealed_fp,
                              'summary_counts': sealed['result'].get('rows') and {
                                  t: len(rows) for t, rows in sealed['result']['rows'].items()}}
        record('staged_state_matches_certified_claim',
               staged['state'] == 'certified' or not manifest.get('claims_certified'),
               {'db_state': staged['state'], 'manifest_claims_certified': manifest.get('claims_certified')})
        if cert:
            record('sealed_fingerprint_matches_certificate',
                   sealed_fp == cert['capture_digest'],
                   {'sealed_recomputed': sealed_fp, 'certificate_capture_digest': cert['capture_digest']})
            stored_cert = json.loads(staged['certificate']) if staged['certificate'] else None
            record('stored_certificate_equals_handed_certificate',
                   stored_cert == cert, {'identical': stored_cert == cert})
        # 4) 声明根、封存根、子库实际行三者是否一致。
        # manifest 的 claimed_root 是单个根元组，封存 roots 是根元组列表。
        claimed = manifest.get('claimed_root')
        record('claimed_root_matches_sealed_root',
               claimed is not None and claimed in sealed_roots,
               {'claimed': claimed, 'sealed': sealed_roots})
        if deliverable.exists():
            actual = [['project', [p]] for p in findings['physical']['project_rows']]
            record('sealed_root_present_in_deliverable',
                   any(sealed_roots and sealed_roots[0] == a for a in actual),
               {'sealed_root': sealed_roots, 'deliverable_project_rows': actual})

    # 5) gamma 这类未完成包：状态机是否仍阻止下载/认证。
    if not deliverable.exists():
        only = cap_rows[0] if cap_rows else None
        record('sealed_only_no_artifact', bool(only and only['state'] == 'sealed'),
               {'state': only['state'] if only else None})
    return findings


def compare_pair(left_dir, right_dir):
    left, right = Path(left_dir), Path(right_dir)
    l_file, r_file = left / 'deliverable.sqlite', right / 'deliverable.sqlite'
    l_cert, r_cert = read_json(left / 'certificate.json'), read_json(right / 'certificate.json')
    result = {
        'pair': [left.name, right.name],
        'file_bytes_identical': file_sha256(l_file) == file_sha256(r_file),
        'left_sha256': file_sha256(l_file),
        'right_sha256': file_sha256(r_file),
        'same_capture_digest': l_cert['capture_digest'] == r_cert['capture_digest'],
        'same_schema_digest': l_cert['schema_digest'] == r_cert['schema_digest'],
        'same_logical_fingerprint': logical_fingerprint(l_file) == logical_fingerprint(r_file),
        'logical_fingerprints': {
            left.name: logical_fingerprint(l_file), right.name: logical_fingerprint(r_file)},
        'page_sizes': {},
        'counts_identical': l_cert['counts'] == r_cert['counts'],
    }
    for d, file in ((left.name, l_file), (right.name, r_file)):
        with closing(open_ro(file)) as conn:
            result['page_sizes'][d] = conn.execute('PRAGMA page_size').fetchone()[0]
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--kit', action='append', default=[], help='交接包目录，可重复传入')
    parser.add_argument('--pair', nargs=2, metavar=('LEFT', 'RIGHT'), action='append', default=[],
                        help='要两两对比的两个交接包目录，可重复传入')
    parser.add_argument('--out', help='把结构化结论写入该 JSON 文件')
    args = parser.parse_args()
    if not args.kit and not args.pair:
        parser.error('至少传入一个 --kit 或一组 --pair')

    report = {'kits': [check_kit(d) for d in args.kit],
              'pairs': [compare_pair(a, b) for a, b in args.pair]}
    text = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    print(text)
    if args.out:
        Path(args.out).write_text(text + '\n')


if __name__ == '__main__':
    main()

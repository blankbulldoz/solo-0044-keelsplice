"""初始化可反复使用的盲审训练素材。

用法（在仓库根目录）：
    python3 training/build/build_kits.py [输出目录]

脚本用真实的 Keelsplice 工作台 API 登记配方、封存、构建并认证子库，
再把成果组装成相互独立的交接包。脚本只写输出目录，默认输出到
training/generated，该目录被 .gitignore 忽略，评分材料不随产物入库。
"""

import json
import shutil
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from keelsplice import bundle  # noqa: E402
from keelsplice.codec import digest, dumps, fingerprint  # noqa: E402
from keelsplice.service import Workbench  # noqa: E402
from keelsplice.storage import Repository  # noqa: E402

SOURCE_ALIAS = 'registry'
SQL_FILE = Path(__file__).with_name('training_source.sql')
EDGE_SITE = {'child': 'site', 'id': 0}
# reading 表有两个外键：id=0 指向 staff(reviewer_id)，id=1 指向 site(site_id)。
EDGE_READING = {'child': 'reading', 'id': 1}
ROW_LIMIT = 100


def seed_source(file):
    with closing(sqlite3.connect(file)) as conn:
        conn.executescript(SQL_FILE.read_text())


def recipe(alias, root, edges):
    return {'source': alias, 'roots': [{'table': 'project', 'key': [root]}],
            'follow_children': edges, 'row_limit': ROW_LIMIT}


RECIPES = {
    'alpha-pull': recipe(SOURCE_ALIAS, 'P1', [EDGE_SITE, EDGE_READING]),
    'beta-pull': recipe(SOURCE_ALIAS, 'P3', [EDGE_SITE, EDGE_READING]),
    'gamma-pull': recipe(SOURCE_ALIAS, 'P1', [EDGE_SITE]),
    'b-pull': recipe(SOURCE_ALIAS, 'P2', [EDGE_SITE, EDGE_READING]),
}


def fresh_workbench(workdir, source_file, state_name):
    state = workdir / state_name
    repo = Repository(state, {SOURCE_ALIAS: source_file})
    return Workbench(repo), state


def register(bench, name):
    return bench.register(name, RECIPES[name])


def certify_capture(bench, name, version=1):
    cap = bench.capture({'name': name, 'version': version})
    bench.build(cap['id'])
    certified = bench.certify(cap['id'])
    return cap['id'], certified


def write_json(file, value):
    file.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + '\n')


def snapshot_record(bench, capture_id):
    """学员可见的捕获记录视图（不含内部封存原文，原文在状态库内）。"""
    return bench.status(capture_id)


def page_size(file):
    with closing(sqlite3.connect(file.as_uri() + '?mode=ro', uri=True)) as conn:
        return conn.execute('PRAGMA page_size').fetchone()[0]


def rearrange_pages(work, artifact_bytes, target_file):
    """只改物理页大小，不改任何逻辑内容；等价于接收方的合法 VACUUM 重排。"""
    target_file.write_bytes(artifact_bytes)
    with closing(sqlite3.connect(target_file)) as conn:
        current = conn.execute('PRAGMA page_size').fetchone()[0]
        new_size = 8192 if current == 4096 else 4096
        conn.execute(f'PRAGMA page_size={new_size}')
        conn.execute('VACUUM')
    return new_size


def logical_fingerprint(file):
    """对逻辑内容（表名 -> 主键 -> 行值）做稳定摘要，与文件物理布局无关。"""
    with closing(sqlite3.connect(file.as_uri() + '?mode=ro', uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        tables = [row[0] for row in conn.execute(
            "SELECT name FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        content = {}
        for table in tables:
            cols = [row['name'] for row in conn.execute(f'PRAGMA table_xinfo("{table}")')]
            content[table] = sorted(map(list, conn.execute(f'SELECT * FROM "{table}"')))
        return fingerprint({'columns': {t: [c for c in cols] for t in tables}, 'rows': content})


def build_kit(workdir, kit_id, note, files):
    kit = workdir / kit_id
    kit.mkdir(parents=True, exist_ok=True)
    for name, data in files.items():
        target = kit / name
        if isinstance(data, Path):
            shutil.copy2(data, target)
        else:
            write_json(target, data)
    manifest = {'kit': kit_id, 'prepared_note': note,
                'files': sorted(p.name for p in kit.iterdir() if p.name != 'kit_manifest.json')}
    write_json(kit / 'kit_manifest.json', manifest)
    return kit


def main():
    out = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else REPO_ROOT / 'training' / 'generated'
    if out.exists():
        shutil.rmtree(out)
    work = out / '_workspace'
    work.mkdir(parents=True)
    source_file = work / 'training_source.sqlite'
    seed_source(source_file)

    # ---- alpha：完全合规的已认证捕获（P1 闭包，跟随 site 与 reading）----
    bench, alpha_state = fresh_workbench(work, source_file, 'alpha_state.sqlite')
    register(bench, 'alpha-pull')
    alpha_id, _ = certify_capture(bench, 'alpha-pull')
    alpha_record = bench.repository.load_capture(alpha_id, with_artifact=True)
    alpha_artifact = alpha_record['artifact']
    alpha_cert = alpha_record['certificate']
    alpha_sealed = alpha_record['sealed']
    alpha_snapshot = snapshot_record(bench, alpha_id)

    # ---- beta：先合规产出，再在组装时制造归属错误 ----
    bench_b, beta_state = fresh_workbench(work, source_file, 'beta_state.sqlite')
    register(bench_b, 'alpha-pull')
    register(bench_b, 'beta-pull')
    alpha_in_beta, _ = certify_capture(bench_b, 'alpha-pull')
    beta_id, _ = certify_capture(bench_b, 'beta-pull')
    # 把 beta 状态库中的 alpha 捕获记录删掉，只留下被调包的 beta 记录。
    with closing(sqlite3.connect(beta_state)) as conn:
        with conn:
            conn.execute('DELETE FROM captures WHERE id=?', (alpha_in_beta,))
            tampered_sealed = dict(alpha_sealed)
            # 封存行集合仍是 P1 的闭包，但根归属被改写成 beta 配方声明的 P3。
            tampered_sealed['result'] = dict(alpha_sealed['result'])
            tampered_sealed['result']['roots'] = [['project', ['P3']]]
            conn.execute('UPDATE captures SET sealed=?, state=?, artifact=?, certificate=? WHERE id=?',
                         (dumps(tampered_sealed), 'certified', alpha_artifact, dumps(alpha_cert), beta_id))
    beta_snapshot = snapshot_record(bench_b, beta_id)

    # ---- gamma：只封存，不构建、不认证 ----
    bench_g, gamma_state = fresh_workbench(work, source_file, 'gamma_state.sqlite')
    register(bench_g, 'gamma-pull')
    gamma_cap = bench_g.capture({'name': 'gamma-pull', 'version': 1})
    gamma_id = gamma_cap['id']
    gamma_snapshot = snapshot_record(bench_g, gamma_id)

    # ---- b1：合规认证（默认页布局）----
    bench_1, b1_state = fresh_workbench(work, source_file, 'b1_state.sqlite')
    register(bench_1, 'b-pull')
    b1_id, _ = certify_capture(bench_1, 'b-pull')
    b1_record = bench_1.repository.load_capture(b1_id, with_artifact=True)
    b1_artifact = b1_record['artifact']
    b1_cert = b1_record['certificate']
    b1_sealed = b1_record['sealed']
    b1_snapshot = snapshot_record(bench_1, b1_id)

    # ---- b2：同一捕获的合法物理重排，重新独立认证 ----
    bench_2, b2_state = fresh_workbench(work, source_file, 'b2_state.sqlite')
    register(bench_2, 'b-pull')
    b2_id = bench_2.capture({'name': 'b-pull', 'version': 1})['id']
    bench_2.build(b2_id)
    built_record = bench_2.repository.load_capture(b2_id, with_artifact=True)
    assert fingerprint(built_record['sealed']) == fingerprint(b1_sealed)
    rearranged = work / 'b2_rearranged.sqlite'
    new_page = rearrange_pages(work, built_record['artifact'], rearranged)
    b2_artifact = rearranged.read_bytes()
    b2_cert = bundle.certify(b2_artifact, built_record['sealed'])
    with closing(sqlite3.connect(b2_state)) as conn:
        with conn:
            conn.execute("UPDATE captures SET state='certified', artifact=?, certificate=? WHERE id=?",
                         (b2_artifact, dumps(b2_cert), b2_id))
    b2_snapshot = snapshot_record(bench_2, b2_id)

    # ---- 组装交接包 ----
    common_note = '交接包内为独立 SQLite 与证书材料，请先盲判再复核。'
    build_kit(work, 'alpha', common_note, {
        'deliverable.sqlite': _bytes_path(work, 'alpha_deliverable.sqlite', alpha_artifact),
        'certificate.json': alpha_cert,
        'capture_snapshot.json': alpha_snapshot,
        'state_copy.sqlite': alpha_state,
        'source_snapshot.sqlite': source_file,
        'handoff_manifest.json': {'claimed_recipe': 'alpha-pull', 'claimed_root': ['project', ['P1']],
                                  'claims_certified': True, 'capture_id': alpha_id},
    })
    build_kit(work, 'beta', common_note, {
        'deliverable.sqlite': _bytes_path(work, 'beta_deliverable.sqlite', alpha_artifact),
        'certificate.json': alpha_cert,
        'capture_snapshot.json': beta_snapshot,
        'state_copy.sqlite': beta_state,
        'source_snapshot.sqlite': source_file,
        'handoff_manifest.json': {'claimed_recipe': 'beta-pull', 'claimed_root': ['project', ['P3']],
                                  'claims_certified': True, 'capture_id': beta_id},
    })
    build_kit(work, 'b1', common_note, {
        'deliverable.sqlite': _bytes_path(work, 'b1_deliverable.sqlite', b1_artifact),
        'certificate.json': b1_cert,
        'capture_snapshot.json': b1_snapshot,
        'state_copy.sqlite': b1_state,
        'source_snapshot.sqlite': source_file,
        'handoff_manifest.json': {'claimed_recipe': 'b-pull', 'claimed_root': ['project', ['P2']],
                                  'claims_certified': True, 'capture_id': b1_id},
    })
    build_kit(work, 'b2', common_note, {
        'deliverable.sqlite': _bytes_path(work, 'b2_deliverable.sqlite', b2_artifact),
        'certificate.json': b2_cert,
        'capture_snapshot.json': b2_snapshot,
        'state_copy.sqlite': b2_state,
        'source_snapshot.sqlite': source_file,
        'handoff_manifest.json': {'claimed_recipe': 'b-pull', 'claimed_root': ['project', ['P2']],
                                  'claims_certified': True, 'capture_id': b2_id,
                                  'rearrangement_note': f'page_size={new_page}'},
    })
    build_kit(work, 'gamma', '该包没有子库文件，只有封存记录与源快照。', {
        'capture_snapshot.json': gamma_snapshot,
        'state_copy.sqlite': gamma_state,
        'source_snapshot.sqlite': source_file,
        'handoff_manifest.json': {'claimed_recipe': 'gamma-pull', 'claimed_root': ['project', ['P1']],
                                  'claims_certified': False, 'capture_id': gamma_id},
    })

    # ---- 自检：确保植入的事实真实成立（不写入任何交接包）----
    _self_check(work, alpha_artifact, alpha_sealed, alpha_cert, b1_artifact, b2_artifact,
                b1_cert, b2_cert, b1_sealed, bench_g, gamma_id, new_page)

    for kit_id in ('alpha', 'beta', 'b1', 'b2', 'gamma'):
        shutil.copytree(work / kit_id, out / kit_id, dirs_exist_ok=True)
    index = {
        'pairs': [{'pair': 'A', 'kits': ['alpha', 'beta'], 'description': '两个显示已认证的子库，文件看起来相同'},
                  {'pair': 'B', 'kits': ['b1', 'b2'], 'description': '两个已认证子库，文件摘要不同'}],
        'solo': [{'kit': 'gamma', 'description': '没有子库文件的封存记录'}],
        'blind_phase_allowed_files': ['kit_manifest.json', 'handoff_manifest.json', 'capture_snapshot.json',
                                      'certificate.json', 'deliverable.sqlite', 'source_snapshot.sqlite',
                                      'state_copy.sqlite'],
        'note': '第一阶段盲判不要打开 training/grading，也不要运行 build 或 recheck 脚本；先自行判断，再按 handbook/recheck_guide.md 复核。',
    }
    write_json(out / 'index.json', index)
    (out / 'README.md').write_text(
        '# 本目录由构建脚本生成\n\n运行 `python3 training/build/build_kits.py` 可随时重建。\n'
        '盲审从 `index.json` 开始；评分判据在 `../grading/`，复核脚本在 `../build/recheck_kits.py`。\n')
    shutil.rmtree(work)
    print(f'训练素材已初始化：{out}')


def _bytes_path(work, name, data):
    file = work / name
    file.write_bytes(data)
    return file


def _self_check(work, alpha_artifact, alpha_sealed, alpha_cert, b1_artifact, b2_artifact,
                b1_cert, b2_cert, b1_sealed, bench_g, gamma_id, new_page):
    alpha_sha = digest(alpha_artifact)
    assert alpha_cert['sha256'] == alpha_sha
    # beta 的证书捕获指纹属于 alpha 的封存原文。
    assert alpha_cert['capture_digest'] == fingerprint(alpha_sealed)
    # 第一对字节相同。
    assert digest(alpha_artifact) == alpha_sha
    # 第二对字节不同，但逻辑内容与捕获指纹相同。
    assert digest(b1_artifact) != digest(b2_artifact)
    assert b1_cert['capture_digest'] == b2_cert['capture_digest'] == fingerprint(b1_sealed)
    assert b1_cert['schema_digest'] == b2_cert['schema_digest']
    b1_file, b2_file = work / 'check_b1.sqlite', work / 'check_b2.sqlite'
    b1_file.write_bytes(b1_artifact)
    b2_file.write_bytes(b2_artifact)
    assert logical_fingerprint(b1_file) == logical_fingerprint(b2_file)
    assert page_size(b1_file) != page_size(b2_file) == new_page
    for file in (b1_file, b2_file):
        with closing(sqlite3.connect(file.as_uri() + '?mode=ro', uri=True)) as conn:
            assert conn.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
            assert conn.execute('PRAGMA foreign_key_check').fetchall() == []
    # gamma 仍在 sealed，认证会被状态机拒绝。
    from keelsplice.codec import Fault
    try:
        bench_g.certify(gamma_id)
    except Fault as exc:
        assert exc.code == 'fixture_not_built'
    else:
        raise AssertionError('gamma 不应允许直接认证')
    print('构建自检通过：陷阱事实与重排等价性均成立。')


if __name__ == '__main__':
    main()

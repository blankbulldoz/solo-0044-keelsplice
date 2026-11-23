"""关系转投最小复现：可直接重放的源、配方、操作顺序与捕获对比。

场景：同一条子对象 member/m-1 的外键从父对象 team-a 转投到 team-b，
两次试算的总行数保持不变（均为 2 行）。脚本通过真实的 Keelsplice HTTP
接口完成登记、试算、封存、追踪、对比、构建与认证，并把所有响应保存为
供人检查的 JSON。另含一组“仅值有变”的对照和六组删除关键事实后的失败
演示，用来区分“确实换了关系”和“只是值有变”。

运行：
    python3 -m examples.relay.replay_relay [输出目录]

数据全部来自本目录 relay.sql 的合成内容（CC0 公有领域），不含客户数据。
每次运行使用临时源库与临时状态库；捕获 ID 是随机不透明字符串，每次不同，
其余结构与断言结果确定性一致。
"""

import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from keelsplice.api import create_server

HERE = Path(__file__).resolve().parent
SEED_SQL = (HERE / 'relay.sql').read_text(encoding='utf-8')

NOFK_SQL = """
CREATE TABLE team (
    id   TEXT PRIMARY KEY,
    name TEXT NOT NULL
);
CREATE TABLE member (
    id      TEXT PRIMARY KEY,
    team_id TEXT NOT NULL,
    skill   REAL NOT NULL
);
INSERT INTO team VALUES ('team-a', 'Alpha 班组'), ('team-b', 'Bravo 班组');
INSERT INTO member VALUES ('m-1', 'team-a', 1.5);
"""


def sql_connect(file):
    conn = sqlite3.connect(file)
    conn.execute('PRAGMA foreign_keys=ON')
    return conn


def seed(file, script=SEED_SQL):
    with closing(sqlite3.connect(file)) as conn:
        conn.executescript(script)


def source_snapshot(file):
    """用独立 SQLite 连接读源表内容，作为人可读的源状态证据。"""
    with closing(sqlite3.connect(file)) as conn:
        return {
            'team': [list(row) for row in conn.execute('SELECT id, name FROM team ORDER BY id')],
            'member': [list(row) for row in conn.execute('SELECT id, team_id, skill FROM member ORDER BY id')],
        }


def call(base, route, body=None, status=200):
    headers = {'Content-Type': 'application/json'}
    data = json.dumps(body).encode('utf-8') if body is not None else None
    try:
        response = urlopen(Request(base + route, data, headers), timeout=5)
    except HTTPError as exc:
        response = exc
    with response:
        payload = response.read()
        assert response.status == status, (route, response.status, payload[:500])
        if response.headers.get_content_type() == 'application/vnd.sqlite3':
            return payload, response.headers['ETag'].strip('"')
        return json.loads(payload)


def main():
    outdir = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else HERE / 'captures'
    outdir.mkdir(parents=True, exist_ok=True)
    notes = {}

    def save(name, payload):
        (outdir / f'{name}.json').write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')

    with TemporaryDirectory(prefix='keelsplice-relay-') as directory:
        root = Path(directory)
        relay_db = root / 'relay.sqlite'
        nofk_db = root / 'nofk.sqlite'
        drift_db = root / 'drift.sqlite'
        rekey_db = root / 'rekey.sqlite'
        orphan_db = root / 'orphan.sqlite'

        seed(relay_db)
        seed(nofk_db, NOFK_SQL)
        seed(drift_db)
        with closing(sqlite3.connect(drift_db)) as conn:
            conn.execute('ALTER TABLE member ADD COLUMN note TEXT')
            conn.execute("UPDATE member SET note='模式漂移行'")
        seed(rekey_db)
        seed(orphan_db)

        sources = {'relay': relay_db, 'nofk': nofk_db, 'drift': drift_db, 'rekey': rekey_db}
        server = create_server(('127.0.0.1', 0), root / 'state.sqlite', sources)
        thread = Thread(target=server.serve_forever, kwargs={'poll_interval': 0.05})
        thread.start()
        try:
            base = f'http://127.0.0.1:{server.server_port}'
            request = lambda route, body=None, status=200: call(base, route, body, status)

            # 1) 关系目录：确认 member.team_id -> team.id 这条外键边。
            catalog = request('/sources/relay/catalog')
            edges = catalog['schema']['edges']
            edge = next(e for e in edges if e['child'] == 'member' and e['parent'] == 'team')
            assert edge == {'child': 'member', 'id': 0, 'parent': 'team',
                            'child_cols': ['team_id'], 'parent_cols': ['id'],
                            'on_update': 'NO ACTION', 'on_delete': 'NO ACTION'}, edge
            save('catalog_edge', {'schema_digest': catalog['schema_digest'], 'edge': edge,
                                  'tables': sorted(catalog['schema']['tables'])})
            schema_digest = catalog['schema_digest']

            # 2) 登记配方：根为子对象 m-1，不选任何反向子边；父行由外键义务自动纳入。
            recipe_body = {'source': 'relay',
                           'roots': [{'table': 'member', 'key': ['m-1']}],
                           'row_limit': 10}
            recipe = request('/recipes/relay-sample/versions', recipe_body, 201)
            assert recipe['version'] == 1 and recipe['schema_digest'] == schema_digest
            save('recipe', recipe)
            lookup = {'name': 'relay-sample', 'version': 1}

            # 3) 状态 A 试算并封存（关系：m-1 -> team-a）。
            save('source_state_a', source_snapshot(relay_db))
            preview_a = request('/preview', lookup)
            capture_a = request('/captures', lookup, 201)
            save('preview_a', preview_a)
            save('capture_a', capture_a)
            assert preview_a['summary']['rows'] == 2
            assert preview_a['summary']['counts'] == {'member': 1, 'team': 1}
            assert preview_a['summary']['witnesses'] == 1
            assert capture_a['roots'] == [['member', ['m-1']]]

            # 4) 同一模式下改写外键：关系转投 team-b（操作序列的唯一关键变更）。
            with closing(sql_connect(relay_db)) as conn:
                with conn:
                    conn.execute("UPDATE member SET team_id='team-b' WHERE id='m-1'")
            save('source_state_b', source_snapshot(relay_db))
            preview_b = request('/preview', lookup)
            capture_b = request('/captures', lookup, 201)
            save('preview_b', preview_b)
            save('capture_b', capture_b)
            assert preview_b['summary'] == preview_a['summary'], '两次试算总行数必须不变'
            assert capture_b['schema_digest'] == schema_digest, '模式未漂移，摘要必须一致'

            # 5) 对照状态 C：关系回到 team-a，只改非外键值 skill —— “只是值有变”。
            with closing(sql_connect(relay_db)) as conn:
                with conn:
                    conn.execute("UPDATE member SET team_id='team-a', skill=2.25 WHERE id='m-1'")
            save('source_state_c', source_snapshot(relay_db))
            preview_c = request('/preview', lookup)
            capture_c = request('/captures', lookup, 201)
            save('preview_c', preview_c)
            save('capture_c', capture_c)
            assert preview_c['summary'] == preview_a['summary']

            # 6) 行证据：同一根在两次封存中的必需父行不同；旧父行在 B 中不存在。
            trace_a = request(f"/captures/{capture_a['id']}/trace",
                              {'table': 'member', 'key': ['m-1']})
            trace_b = request(f"/captures/{capture_b['id']}/trace",
                              {'table': 'member', 'key': ['m-1']})
            missing = request(f"/captures/{capture_b['id']}/trace",
                              {'table': 'team', 'key': ['team-a']}, 404)
            save('trace_a_member', trace_a)
            save('trace_b_member', trace_b)
            save('trace_b_team_a_not_found', missing)
            parent_of = lambda traced: traced['dependencies'][0]['target']
            assert parent_of(trace_a) == ['team', ['team-a']]
            assert parent_of(trace_b) == ['team', ['team-b']]
            assert trace_a['causes'] == [] and trace_a['roots'] == [0]
            assert missing['error'] == 'row_not_captured'

            # 7) 捕获对比：A/B 是关系转投；A/C 只有 member 值变化；自比全空。
            compare_ab = request('/compare', {'left': capture_a['id'], 'right': capture_b['id']})
            compare_ac = request('/compare', {'left': capture_a['id'], 'right': capture_c['id']})
            compare_same = request('/compare', {'left': capture_a['id'], 'right': capture_a['id']})
            save('compare_a_b', compare_ab)
            save('compare_a_c', compare_ac)
            save('compare_same', compare_same)
            assert compare_ab['changes']['team'] == {
                'added': [['team-b']], 'removed': [['team-a']], 'modified': []}
            assert compare_ab['changes']['member'] == {
                'added': [], 'removed': [], 'modified': [['m-1']]}
            assert compare_ac['changes']['team'] == {
                'added': [], 'removed': [], 'modified': []}
            assert compare_ac['changes']['member'] == {
                'added': [], 'removed': [], 'modified': [['m-1']]}
            assert all(not any(section for section in table.values())
                       for table in compare_same['changes'].values())
            notes['discriminator'] = (
                'team 表出现 added/removed 且 member 主键不变即关系转投；'
                '只有 member.modified、team 无增减即仅值有变。')

            # 8) 构建并认证两个封存库，用独立 SQLite 连接复核外键与连接结果。
            verification = {}
            for label, cap, expected_parent in (
                    ('a', capture_a, 'team-a'), ('b', capture_b, 'team-b')):
                cid = cap['id']
                built = request(f'/captures/{cid}/build', {})
                certified = request(f'/captures/{cid}/certify', {})
                assert built['state'] == 'built' and certified['state'] == 'certified'
                data, etag = request(f'/captures/{cid}/database')
                fixture = root / f'fixture_{label}.sqlite'
                fixture.write_bytes(data)
                with closing(sqlite3.connect(fixture.as_uri() + '?mode=ro', uri=True)) as check:
                    violations = check.execute('PRAGMA foreign_key_check').fetchall()
                    join_row = check.execute(
                        'SELECT m.id, t.id FROM member m JOIN team t ON m.team_id = t.id').fetchone()
                assert violations == [] and join_row == ('m-1', expected_parent)
                verification[label] = {'certificate': certified['certificate'],
                                       'etag_matches_sha256': etag == certified['certificate']['sha256'],
                                       'foreign_key_check': violations,
                                       'parent_join_row': list(join_row)}
                save(f'build_{label}', verification[label])

            # ---- 删除关键事实的失败演示（任一成立都会让原观察无法成立） ----

            # N1 模式漂移：源加列后模式摘要不同，跨模式对比被拒绝。
            drift_recipe = request('/recipes/drift-sample/versions',
                                   {'source': 'drift',
                                    'roots': [{'table': 'member', 'key': ['m-1']}],
                                    'row_limit': 10}, 201)
            drift_capture = request('/captures',
                                    {'name': 'drift-sample', 'version': drift_recipe['version']}, 201)
            drift_conflict = request('/compare',
                                     {'left': capture_a['id'], 'right': drift_capture['id']}, 409)
            save('negative_drift', drift_conflict)
            assert drift_conflict['error'] == 'schema_conflict'

            # N2 删除外键声明：无 FK 的同源数据里，闭包不再纳入任何父行。
            nofk_catalog = request('/sources/nofk/catalog')
            assert nofk_catalog['schema']['edges'] == []
            nofk_recipe = request('/recipes/nofk-sample/versions',
                                  {'source': 'nofk',
                                   'roots': [{'table': 'member', 'key': ['m-1']}],
                                   'row_limit': 10}, 201)
            nofk_preview = request('/preview',
                                   {'name': 'nofk-sample', 'version': nofk_recipe['version']})
            save('negative_no_fk', {'edges': [], 'preview_summary': nofk_preview['summary']})
            assert nofk_preview['summary']['counts'] == {'member': 1, 'team': 0}

            # N3 删除目标父行事实：转投到不存在的父对象会被外键拒绝，状态 B 无法建立。
            with closing(sql_connect(orphan_db)) as conn:
                try:
                    conn.execute("UPDATE member SET team_id='team-x' WHERE id='m-1'")
                    raise AssertionError('缺少目标父行时外键必须拒绝写入')
                except sqlite3.IntegrityError as exc:
                    save('negative_orphan_target', {'integrity_error': str(exc)})

            # N4 删除来源父行事实：m-1 仍引用 team-a 时，父行不允许删除。
            with closing(sql_connect(orphan_db)) as conn:
                try:
                    conn.execute("DELETE FROM team WHERE id='team-a'")
                    raise AssertionError('被引用的父行必须受外键保护')
                except sqlite3.IntegrityError as exc:
                    save('negative_delete_parent', {'integrity_error': str(exc)})

            # N5 行预算事实：预算低于闭包行数（2）时试算直接 row_budget，拿不到观察。
            request('/recipes/tight-sample/versions',
                    {'source': 'relay',
                     'roots': [{'table': 'member', 'key': ['m-1']}],
                     'row_limit': 1}, 201)
            tight = request('/preview', {'name': 'tight-sample', 'version': 1}, 422)
            save('negative_row_budget', tight)
            assert tight['error'] == 'row_budget'

            # N6 子对象身份事实：删除 m-1 再插入 m-2 是身份更替，不是同一对象转投。
            ra = request('/recipes/rekey-a/versions',
                         {'source': 'rekey',
                          'roots': [{'table': 'member', 'key': ['m-1']}],
                          'row_limit': 10}, 201)
            rekey_a = request('/captures', {'name': 'rekey-a', 'version': ra['version']}, 201)
            with closing(sql_connect(rekey_db)) as conn:
                with conn:
                    conn.execute("DELETE FROM member WHERE id='m-1'")
                    conn.execute("INSERT INTO member VALUES ('m-2', 'team-b', 1.5)")
            rb = request('/recipes/rekey-b/versions',
                         {'source': 'rekey',
                          'roots': [{'table': 'member', 'key': ['m-2']}],
                          'row_limit': 10}, 201)
            rekey_b = request('/captures', {'name': 'rekey-b', 'version': rb['version']}, 201)
            rekey_diff = request('/compare', {'left': rekey_a['id'], 'right': rekey_b['id']})
            save('negative_rekey_compare', rekey_diff)
            assert rekey_diff['changes']['member']['removed'] == [['m-1']]
            assert rekey_diff['changes']['member']['added'] == [['m-2']]
            assert rekey_diff['changes']['member']['modified'] == []
            notes['identity_warning'] = (
                'member 同时出现 added/removed 表示主键身份已变，不能表述为“m-1 转投”；'
                '主案例中 m-1 始终是 modified，才是同一对象的关系转投。')

            save('run_summary', {
                'outdir': str(outdir),
                'schema_digest': schema_digest,
                'capture_ids': {'a': capture_a['id'], 'b': capture_b['id'], 'c': capture_c['id']},
                'preview_rows_unchanged': preview_a['summary'] == preview_b['summary'] == preview_c['summary'],
                'assertions': 'all passed',
                'notes': notes,
            })
        finally:
            server.shutdown()
            thread.join()

    print(f'OK: 全部断言通过，人可读证据已写入 {outdir}')


if __name__ == '__main__':
    main()

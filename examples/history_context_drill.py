"""复现历史上下文两种保存位置在结构漂移与数据变化下的恢复假象。

配套决策记录：docs/decisions/2026-11-24-history-context-placement.md。

演练对真实 HTTP 服务注入一次结构漂移（外键子句重排，SQLite 按子句逆序编
号，位置 token 0:assay 改指另一条外键）和一次数据变化（行改属另一父键，
结构摘要不变），验证两类"看起来恢复成功但证据不成立"：

- 案例一（多标签页草稿 × 结构漂移）：浏览器草稿只存表单字段，恢复进新编辑
  页时隐藏摘要被静默换成新值，提交全绿但外键语义已偏离原判断（C1-C5）。
- 案例二（旧捕获对照 × 数据变化）：服务端会话落库派生计数，恢复校验（捕获
  存在 + 摘要一致）全绿，但缓存计数已不反映当前源（C6-C7）。

断言失败意味着系统行为已变化，需同步更新决策记录中的对应条款。
"""

import json
import re
import sqlite3
from contextlib import closing, contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

from keelsplice.api import create_server

from .walkthrough import seed

ROOTS = '[{"table":"substrate","key":["east","MICA"]}]'
DRAFT_COUNTS = {'substrate': 1, 'cassette': 2, 'assay': 2, 'frame': 0}
DRIFTED_COUNTS = {'substrate': 1, 'cassette': 0, 'assay': 0, 'frame': 0}
MOVED_COUNTS = {'substrate': 1, 'cassette': 2, 'assay': 1, 'frame': 0}

# 重建 assay：两条外键都改写为表级子句并交换顺序。SQLite 按子句逆序分配外键
# id，迁移后 0:assay 指向 cassette 自引用，substrate 复合外键降为 id 1。
DRIFT_SQL = '''
PRAGMA foreign_keys=OFF;
BEGIN;
CREATE TABLE assay_migrated (
    mark TEXT PRIMARY KEY NOT NULL,
    region TEXT,
    code TEXT,
    cassette INTEGER NOT NULL,
    length_um REAL NOT NULL,
    doubled REAL GENERATED ALWAYS AS (length_um * 2) STORED,
    FOREIGN KEY (region, code) REFERENCES substrate,
    FOREIGN KEY (cassette) REFERENCES cassette
);
INSERT INTO assay_migrated (mark, region, code, cassette, length_um)
    SELECT mark, region, code, cassette, length_um FROM assay;
DROP TABLE assay;
ALTER TABLE assay_migrated RENAME TO assay;
CREATE INDEX assay_substrate_lookup ON assay(region,code) WHERE code IS NOT NULL;
COMMIT;
'''


def open_page(base, path, payload=None, expected=200):
    data = None if payload is None else urlencode(payload, doseq=True).encode('ascii')
    headers = {} if data is None else {'Origin': base}
    request = Request(base + path, data=data, headers=headers)
    try:
        response = urlopen(request, timeout=5)
    except HTTPError as exc:
        response = exc
    with response:
        assert response.status == expected, (path, response.status, response.read())
        return urlsplit(response.url).path, response.read().decode('utf-8')


def api(base, route, body=None, status=200):
    data = None if body is None else json.dumps(body).encode('utf-8')
    request = Request(base + route, data, {'Content-Type': 'application/json'})
    try:
        response = urlopen(request, timeout=5)
    except HTTPError as exc:
        response = exc
    with response:
        payload = response.read()
        assert response.status == status, (route, response.status, payload[:500])
        return json.loads(payload)


@contextmanager
def running(root):
    source = root / 'source.sqlite'
    seed(source)
    with create_server(('127.0.0.1', 0), root / 'state.sqlite', {'microscope': source}) as server:
        thread = Thread(target=server.serve_forever, kwargs={'poll_interval': 0.05})
        thread.start()
        try:
            yield f'http://127.0.0.1:{server.server_port}', source
        finally:
            server.shutdown()
            thread.join()


def editor_digest(base):
    _, page = open_page(base, '/workbench/recipes/new')
    return re.search(r'name="digest" value="([0-9a-f]{64})"', page)[1]


def update_source(source, sql):
    with closing(sqlite3.connect(source)) as conn:
        conn.executescript(sql)


def cell_browser_draft_schema_drift(root):
    print('== 案例一：多标签页草稿 × 结构漂移（浏览器保存） ==')
    with running(root) as (base, source):
        # 两个标签页先后打开编辑页，看到同一份目录（摘要 D0）。
        digest0 = editor_digest(base)
        assert digest0 == editor_digest(base)
        catalog0 = api(base, '/sources/microscope/catalog')
        edge0 = next(e for e in catalog0['schema']['edges'] if e['child'] == 'assay' and e['id'] == 0)
        assert edge0['parent'] == 'substrate' and edge0['child_cols'] == ['region', 'code']
        print(f"  D0={digest0[:12]}… 0:assay 在 D0 下 = substrate→assay")

        # 浏览器草稿只存表单字段（与 localStorage 能力一致）；操作员意图另行记为地面真值。
        draft = {'source': 'microscope', 'name': 'mica-swatch', 'row_limit': '20',
                 'edge': ['0:assay'], 'roots': ROOTS}
        intent = {'parent': 'substrate', 'child': 'assay', 'child_cols': ['region', 'code']}

        # 地面真值：用已登记配方固定草稿时代语义，记录当时闭包计数。
        api(base, '/recipes/witness/versions',
            {'source': 'microscope', 'roots': json.loads(ROOTS),
             'follow_children': [{'child': 'assay', 'id': 0}], 'row_limit': 20}, 201)
        before = api(base, '/preview', {'name': 'witness', 'version': 1})
        assert before['summary']['counts'] == DRAFT_COUNTS
        print(f'  草稿时代闭包计数 {before["summary"]["counts"]}')

        # 注入结构漂移。
        update_source(source, DRIFT_SQL)
        catalog1 = api(base, '/sources/microscope/catalog')
        digest1 = catalog1['schema_digest']
        flipped = {e['id']: e['parent'] for e in catalog1['schema']['edges'] if e['child'] == 'assay'}
        assert digest1 != digest0 and flipped[0] == 'cassette' and flipped[1] == 'substrate'
        print(f'  D1={digest1[:12]}… 漂移后 0:assay=cassette→assay，1:assay=substrate→assay  [C1]')

        # 防线现状：过期标签页按原样提交被 409 拦下；已登记配方试算被 409 拦下。
        _, stale = open_page(base, '/workbench/recipes/new', draft | {'digest': digest0}, 409)
        assert '目录已变化' in stale
        assert api(base, '/preview', {'name': 'witness', 'version': 1}, 409)['error'] == 'schema_conflict'
        print('  过期标签页提交 409（browser_schema）；已登记配方试算 409（schema_conflict）  [C2][C4]')

        # 恢复路径：新编辑页隐藏摘要已是 D1，草稿字段回填，提交到封存全绿。
        assert editor_digest(base) == digest1
        path, page = open_page(base, '/workbench/recipes/new', draft | {'digest': digest1})
        assert path.endswith('/workbench/recipes/mica-swatch/1') and '投用中' in page
        registered = api(base, '/recipes/mica-swatch/versions/1')
        assert registered['recipe']['follow_children'] == [{'child': 'assay', 'id': 0}]
        target = flipped[registered['recipe']['follow_children'][0]['id']]
        assert target != intent['parent'], 'C3: 恢复后的外键语义应已偏离原判断'
        restored = api(base, '/preview', {'name': 'mica-swatch', 'version': 1})
        assert restored['summary']['counts'] == DRIFTED_COUNTS
        assert api(base, '/captures', {'name': 'mica-swatch', 'version': 1}, 201)['state'] == 'sealed'
        print(f'  恢复提交→登记→试算→封存全绿，但 0:assay 已指向 {target}，'
              f'闭包计数变为 {restored["summary"]["counts"]}  [C3 假成功]')

        # 对照：服务端草稿（语义身份 + 原始摘要）恢复时响亮失败并可重定位。
        remap = next(e for e in catalog1['schema']['edges']
                     if (e['parent'], e['child'], e['child_cols'])
                     == (intent['parent'], intent['child'], intent['child_cols']))
        assert digest0 != digest1 and remap['id'] == 1
        print(f"  对照：服务端草稿恢复时摘要不等（响亮），语义身份重定位 token 0→{remap['id']}  [C5]")


def cell_server_session_data_change(root):
    print('== 案例二：旧捕获对照 × 数据变化（服务端保存） ==')
    with running(root) as (base, source):
        catalog = api(base, '/sources/microscope/catalog')
        digest = catalog['schema_digest']
        edge = next(e for e in catalog['schema']['edges']
                    if e['child'] == 'assay' and e['parent'] == 'substrate')
        api(base, '/recipes/swatch/versions',
            {'source': 'microscope', 'roots': json.loads(ROOTS),
             'follow_children': [{'child': 'assay', 'id': edge['id']}], 'row_limit': 20}, 201)

        cap_a = api(base, '/captures', {'name': 'swatch', 'version': 1}, 201)
        assert cap_a['summary']['counts'] == DRAFT_COUNTS
        update_source(source, "UPDATE assay SET length_um=7 WHERE mark='S-01'")
        cap_b = api(base, '/captures', {'name': 'swatch', 'version': 1}, 201)
        diff = api(base, '/compare', {'left': cap_a['id'], 'right': cap_b['id']})
        assert diff['changes']['assay']['modified'] == [['S-01']]

        # 服务端历史落库：对比会话连同当时的"当前源计数"派生缓存。
        history = root / 'history.sqlite'
        cached = api(base, '/preview', {'name': 'swatch', 'version': 1})['summary']['counts']
        assert cached == DRAFT_COUNTS
        with closing(sqlite3.connect(history)) as conn, conn:
            conn.execute('CREATE TABLE sessions (note TEXT, left_id TEXT, right_id TEXT,'
                         ' schema_digest TEXT, cached_counts TEXT, conclusion TEXT)')
            conn.execute('INSERT INTO sessions VALUES (?,?,?,?,?,?)',
                         ('B 为 7µm 修正候选', cap_a['id'], cap_b['id'], digest,
                          json.dumps(cached, sort_keys=True), 'S-01 length_um 3.25→7'))

        # 注入数据变化：S-01 改属另一父键（结构不动，摘要不变）。
        update_source(source, "UPDATE assay SET region='West', code='Basalt' WHERE mark='S-01'")

        # 恢复路径：落库会话通过全部恢复校验，但派生缓存已不成立。
        with closing(sqlite3.connect(history)) as conn:
            note, left, right, kept_digest, cached_counts, conclusion = \
                conn.execute('SELECT * FROM sessions').fetchone()
        checks = {'左捕获存在': api(base, f'/captures/{left}')['id'] == left,
                  '右捕获存在': api(base, f'/captures/{right}')['id'] == right,
                  '摘要一致': api(base, '/sources/microscope/catalog')['schema_digest'] == kept_digest}
        assert all(checks.values())
        fresh = api(base, '/preview', {'name': 'swatch', 'version': 1})['summary']['counts']
        assert json.loads(cached_counts) != fresh and fresh == MOVED_COUNTS
        cap_c = api(base, '/captures', {'name': 'swatch', 'version': 1}, 201)
        moved = api(base, '/compare', {'left': cap_b['id'], 'right': cap_c['id']})
        assert moved['changes']['assay']['removed'] == [['S-01']]
        print(f'  恢复校验 {checks} 全绿，但缓存计数 {json.loads(cached_counts)}'
              f' ≠ 当前源计数 {fresh}，B×C 对比显示 S-01 已移出闭包  [C6 假成功]')

        # 对照：浏览器引用式历史只存不透明引用，重放时全部重新求值。
        replay = api(base, '/compare', {'left': left, 'right': right})
        assert replay == diff
        assert api(base, f'/captures/{left}')['summary']['counts'] == DRAFT_COUNTS
        print('  对照：引用式历史重放 A×B 逐字一致，当前源状态重新求值即刻可见；'
              f'会话注释（{note}／{conclusion}）只存在于服务端落库记录  [C7]')


def main():
    with TemporaryDirectory(prefix='keelsplice-history-') as directory:
        root = Path(directory)
        (root / 'cell-1').mkdir()
        (root / 'cell-2').mkdir()
        cell_browser_draft_schema_drift(root / 'cell-1')
        cell_server_session_data_change(root / 'cell-2')
    print('历史上下文演练通过：结构漂移与数据变化下，两种保存位置的假成功恢复均按预期复现（C1-C7）')


if __name__ == '__main__':
    main()

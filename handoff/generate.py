"""生成「残缺台账交接」场景：真实工作台状态、可公开台账与核对报告。

用法：python3 handoff/generate.py [--force]

剧本通过 keelsplice 真实 HTTP 服务（JSON 接口与浏览器页面）在 handoff/ 下建立
持久状态库与源库，按两组同名配方的多次交接制造捕获、构建、认证与下载，再把
核对结论写成 ledger.md（台账公开抄本）与 reconciliation.md（核对报告）。
所有写进报告的事实（页面路径、短编号唯一性、证书唯一性、矛盾点）都在生成时
对真实状态逐条验证。
"""

from __future__ import annotations

import json
import sqlite3
import sys
from contextlib import closing
from hashlib import sha256
from pathlib import Path
from threading import Thread
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from examples.walkthrough import seed  # noqa: E402
from keelsplice import bundle  # noqa: E402
from keelsplice.api import create_server  # noqa: E402

HANDOFF = ROOT / 'handoff'
SOURCE = HANDOFF / 'microscope.sqlite'
STATE = HANDOFF / 'state.sqlite'
DOWNLOADS = HANDOFF / 'downloads'

CHECKS = []


def note(text):
    CHECKS.append(text)
    print('✓', text)


def call(base, route, body=None, status=200):
    """JSON 接口（与 examples/walkthrough.py 相同形状）。"""
    headers = {'Content-Type': 'application/json'}
    data = None if body is None else json.dumps(body).encode('utf-8')
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


def open_page(base, path, payload=None, expected=200):
    """浏览器表单（与 examples/browser_walkthrough.py 相同形状）。"""
    data = None if payload is None else urlencode(payload).encode('ascii')
    headers = {} if data is None else {'Origin': base}
    try:
        response = urlopen(Request(base + path, data, headers), timeout=5)
    except HTTPError as exc:
        response = exc
    with response:
        body = response.read()
        assert response.status == expected, (path, response.status, body[:500])
        return response.headers, body


def html(base, path, payload=None, expected=200):
    headers, body = open_page(base, path, payload, expected)
    assert headers.get_content_type() == 'text/html', path
    return body.decode('utf-8')


def reset():
    for file in (SOURCE, STATE):
        for suffix in ('', '-wal', '-shm', '-journal'):
            Path(str(file) + suffix).unlink(missing_ok=True)
    if DOWNLOADS.exists():
        for file in DOWNLOADS.iterdir():
            file.unlink()
    DOWNLOADS.mkdir(exist_ok=True)


def scenario(base):
    """按交接剧本驱动真实服务，返回写文档所需的事实。"""
    facts = {}

    catalog = call(base, '/sources/microscope/catalog')
    edge = next(e for e in catalog['schema']['edges'] if e['child'] == 'assay' and e['parent'] == 'substrate')
    facts['edge_id'] = edge['id']
    body = {'source': 'microscope', 'roots': [{'table': 'substrate', 'key': ['east', 'MICA']}],
            'follow_children': [{'child': 'assay', 'id': edge['id']}], 'row_limit': 20}

    v1 = call(base, '/recipes/swatch/versions', body, 201)
    assert v1['version'] == 1
    call(base, '/recipes/swatch/versions', body | {'row_limit': 1}, 201)
    retired = call(base, '/recipes/swatch/versions/2/retire', {})
    assert retired['active'] is False
    assert call(base, '/preview', {'name': 'swatch', 'version': 2}, 409)['error'] == 'retired_recipe'
    note('swatch v1 登记投用；v2（上限 1）登记后即停用，试算返回 retired_recipe')

    preview = call(base, '/preview', {'name': 'swatch', 'version': 1})
    assert preview['summary']['rows'] == 5 and preview['summary']['witnesses'] == 8
    assert preview['roots'] == [['substrate', ['East', 'Mica']]]
    facts['summary'] = preview['summary']
    facts['old_schema'] = preview['schema_digest']
    note('v1 试算：5 行 / 8 见证，实存根 ["East","Mica"]（输入拼写 east/MICA 被 NOCASE 归一）')

    cap_a = call(base, '/captures', {'name': 'swatch', 'version': 1}, 201)
    facts['A'] = cap_a['id']
    assert cap_a['state'] == 'sealed' and cap_a['roots'] == [['substrate', ['East', 'Mica']]]
    assert call(base, f'/captures/{cap_a["id"]}/build', {})['state'] == 'built'
    certified = call(base, f'/captures/{cap_a["id"]}/certify', {})
    facts['cert'] = certified['certificate']
    data, etag = call(base, f'/captures/{cap_a["id"]}/database')
    assert sha256(data).hexdigest() == etag == facts['cert']['sha256']
    fixture = DOWNLOADS / f'keelsplice-{cap_a["id"]}.sqlite'
    fixture.write_bytes(data)
    facts['fixture'] = fixture.name
    note(f'捕获 A（swatch v1，S-01=3.25）已构建并认证，证书 SHA-256 {etag[:12]}…，子库交付 downloads/{fixture.name}')

    with closing(sqlite3.connect(SOURCE)) as conn, conn:
        conn.execute("UPDATE assay SET length_um=4.75 WHERE mark='S-01'")
    cap_b = call(base, '/captures', {'name': 'swatch', 'version': 1}, 201)
    facts['B'] = cap_b['id']
    diff = call(base, '/compare', {'left': cap_a['id'], 'right': cap_b['id']})
    assert diff['changes']['assay']['modified'] == [['S-01']]
    note('源库 S-01 改长 3.25→4.75 后，v1 再封存得捕获 B；对比 A/B 仅 assay 修改 ["S-01"]')

    with closing(sqlite3.connect(SOURCE)) as conn, conn:
        conn.execute('ALTER TABLE frame ADD COLUMN caption TEXT')
    assert call(base, '/preview', {'name': 'swatch', 'version': 1}, 409)['error'] == 'schema_conflict'
    call(base, '/recipes/swatch/versions', body, 201)
    cap_c = call(base, '/captures', {'name': 'swatch', 'version': 3}, 201)
    facts['C'] = cap_c['id']
    assert call(base, '/compare', {'left': cap_a['id'], 'right': cap_c['id']}, 409)['error'] == 'schema_conflict'
    facts['new_schema'] = call(base, f'/captures/{cap_c["id"]}')['schema_digest']
    assert facts['new_schema'] != facts['old_schema']
    note('frame 加列后 v1 试算报 schema_conflict；登记 v3 封存得捕获 C，模式摘要已变')

    east = {'source': 'microscope', 'follow_children': [{'child': 'assay', 'id': edge['id']}], 'row_limit': 20}
    call(base, '/recipes/mica-watch/versions', east | {'roots': [{'table': 'substrate', 'key': ['east', 'MICA']}]}, 201)
    call(base, '/recipes/mica-watch/versions', east | {'roots': [{'table': 'substrate', 'key': ['East', 'Mica']}]}, 201)
    cap_d = call(base, '/captures', {'name': 'mica-watch', 'version': 1}, 201)
    cap_e = call(base, '/captures', {'name': 'mica-watch', 'version': 2}, 201)
    facts['D'], facts['E'] = cap_d['id'], cap_e['id']
    assert cap_d['roots'] == cap_e['roots'] == [['substrate', ['East', 'Mica']]]
    same = call(base, '/compare', {'left': cap_d['id'], 'right': cap_e['id']})
    assert all(not kind for table in same['changes'].values() for kind in table.values())
    note('mica-watch v1/v2 根拼写不同（east/MICA 与 East/Mica），捕获 D、E 实存根相同且行内容一致')
    return facts


def check_state(facts):
    """用只读连接直接核对状态库，验证台账各行结论。"""
    with closing(sqlite3.connect(STATE.as_uri() + '?mode=ro', uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        recipes = conn.execute('SELECT * FROM recipes ORDER BY name, version').fetchall()
        captures = conn.execute('SELECT * FROM captures ORDER BY rowid').fetchall()
    assert [(r['name'], r['version']) for r in recipes] == [
        ('mica-watch', 1), ('mica-watch', 2), ('swatch', 1), ('swatch', 2), ('swatch', 3)]
    assert [r['active'] for r in recipes if r['name'] == 'swatch'] == [1, 0, 1]
    by_id = {r['id']: r for r in captures}
    assert [by_id[facts[k]]['state'] for k in 'ABCDE'] == ['certified', 'sealed', 'sealed', 'sealed', 'sealed']
    note('状态库直查：5 个配方版本（swatch v2 已停用）、5 次捕获（仅 A 已认证）')

    shorts = {k: facts[k][:8] for k in 'ABCDE'}
    for k, short in shorts.items():
        hits = [r['id'] for r in captures if r['id'].startswith(short)]
        assert hits == [facts[k]], (k, hits)
    note('台账所用的 8 位短编号在 captures 表中各自唯一')

    certs = [json.loads(r['certificate']) for r in captures if r['certificate']]
    assert len(certs) == 1 and certs[0]['sha256'] == facts['cert']['sha256']
    note('证书集合只有 A1 一份，完整 SHA-256 在库内唯一')

    def sealed(row):
        return json.loads(by_id[facts[row]]['sealed'])

    def candidates(name, key):
        out = []
        for r in captures:
            if r['name'] != name:
                continue
            roots = json.loads(r['sealed'])['result']['roots']
            if ['substrate', key] in roots:
                out.append(r['id'])
        return out

    actual = ['East', 'Mica']
    assert sorted(candidates('swatch', actual)) == sorted([facts['A'], facts['B'], facts['C']])
    assert sorted(candidates('mica-watch', actual)) == sorted([facts['D'], facts['E']])
    note('A3 行（swatch + 实存根）候选为 A、B、C 三个；B1 行（mica-watch + 实存根）候选为 D、E 两个')

    assert by_id[facts['B']]['version'] == 1
    assert not [r for r in captures if r['name'] == 'swatch' and r['version'] == 2]
    assert by_id[facts['E']]['version'] == 2
    assert sealed('A')['result']['roots'] == [['substrate', ['East', 'Mica']]]
    note('A4/A6/B3 的矛盾点成立：B 属 v1 且 v2 无捕获；A 的实存根不是 West/Basalt；E 属 v2')

    artifacts = {k: bundle.build(sealed(k)) for k in 'ABCDE'}
    assert sha256(artifacts['A']).hexdigest() == facts['cert']['sha256']
    assert artifacts['C'] == artifacts['D'] == artifacts['E']
    assert len({artifacts[k] for k in 'ABCDE'}) == 3
    note('重建字节核对：A 的构建与证书一致；C、D、E 内容相同（若认证将共享同一文件摘要）；B 与三者均不同')


def check_pages(base, facts):
    """验证报告里写出的每一条页面路径。"""
    for path in ['/workbench', '/workbench/recipes', '/workbench/captures', '/workbench/compare']:
        html(base, path)
    for name, number in [('swatch', 1), ('swatch', 2), ('swatch', 3), ('mica-watch', 1), ('mica-watch', 2)]:
        html(base, f'/workbench/recipes/{name}/{number}')
    for k in 'ABCDE':
        html(base, f'/workbench/captures/{facts[k]}')
    note('报告列出的目录、配方、捕获页面全部返回 200')

    v2_page = html(base, '/workbench/recipes/swatch/2')
    assert '已停用' in v2_page
    v1_page = html(base, '/workbench/recipes/swatch/1')
    assert 'east' in v1_page and 'MICA' in v1_page
    e_page = html(base, f'/workbench/captures/{facts["E"]}')
    assert '版本 2' in e_page
    b_page = html(base, f'/workbench/captures/{facts["B"]}')
    assert '版本 1' in b_page
    note('页面证据：v2 显示已停用；v1 登记根为输入拼写 east/MICA；B 属版本 1、E 属版本 2')

    headers, data = open_page(base, f'/workbench/captures/{facts["A"]}/database')
    assert headers.get_content_type() == 'application/vnd.sqlite3'
    assert headers['ETag'].strip('"') == facts['cert']['sha256'] == sha256(data).hexdigest()
    assert data == (DOWNLOADS / facts['fixture']).read_bytes()
    html(base, f'/workbench/captures/{facts["B"]}/database', expected=409)
    note('A 的下载页 ETag 与证书一致且字节等于交付文件；B 未认证，下载页 409')

    trace = {'table': 'substrate', 'key': '["East","Mica"]'}
    evidence = html(base, f'/workbench/captures/{facts["A"]}/trace', trace)
    assert '直接入选原因' in evidence and 'S-01' in evidence
    miss = html(base, f'/workbench/captures/{facts["A"]}/trace',
                {'table': 'substrate', 'key': '["east","MICA"]'}, expected=404)
    assert '实存主键' in miss
    west = html(base, f'/workbench/captures/{facts["A"]}/trace',
                {'table': 'substrate', 'key': '["West","Basalt"]'}, expected=404)
    assert '实存主键' in west
    note('追踪页：实存键可查证据；输入拼写 east/MICA 与 West/Basalt 均返回「没有这个实存主键」')

    compare = html(base, '/workbench/compare', {'left': facts['A'], 'right': facts['B']})
    assert '修改' in compare and 'S-01' in compare
    same = html(base, '/workbench/compare', {'left': facts['D'], 'right': facts['E']})
    assert '行内容一致' in same
    conflict = html(base, '/workbench/compare', {'left': facts['A'], 'right': facts['C']}, expected=409)
    assert '模式摘要' in conflict
    note('对比页：A/B 显示修改 S-01；D/E 显示行内容一致；A/C 返回 409 模式摘要不一致')


def check_fixture(facts):
    fixture = DOWNLOADS / facts['fixture']
    with closing(sqlite3.connect(fixture.as_uri() + '?mode=ro', uri=True)) as conn:
        assert conn.execute('PRAGMA foreign_key_check').fetchall() == []
        assert conn.execute("SELECT length_um FROM assay WHERE mark='S-01'").fetchone() == (3.25,)
        assert conn.execute('SELECT count(*) FROM frame').fetchone() == (0,)
    with closing(sqlite3.connect(SOURCE.as_uri() + '?mode=ro', uri=True)) as conn:
        assert conn.execute("SELECT length_um FROM assay WHERE mark='S-01'").fetchone() == (4.75,)
    note('交付子库独立复核：外键干净、S-01 仍是封存时的 3.25；源库现状已是 4.75（封存不受源更新影响）')


def write_ledger(facts):
    A8, B8, C8, E8 = (facts[k][:8] for k in 'ABCE')
    cert = facts['cert']
    rows_a = [
        ('A1', '1', A8, '`["East","Mica"]`', '5', '8',
         f'SHA-256 `{cert["sha256"]}`（{cert["bytes"]} 字节）', '认证子库已随第一批交付'),
        ('A2', '1', B8, '`["East", …]`（code 缺抄）', '5', '8', '—', 'S-01 改长后补封'),
        ('A3', '—', '—', '`["East","Mica"]`', '5', '8', '—', '只记得是 swatch 的根'),
        ('A4', '2', B8, '—', '—', '—', '—', 'v2 停用前抢封的一次'),
        ('A5', '3', C8, '—', '5', '8', '—', 'frame 加列后按 v3 封存，勿与旧捕获对比'),
        ('A6', '1', A8, '`["West","Basalt"]`', '—', '—', '—', '西区 Basalt 的根'),
    ]
    rows_b = [
        ('B1', '—', '—', '`["East","Mica"]`', '5', '8', '—', '两版交接的是同一根'),
        ('B2', '2', E8, '`[…, "Mica"]`（region 缺抄）', '5', '8', '—', '只抄到 code'),
        ('B3', '1', E8, '—', '—', '—', '—', '第一版的封存'),
    ]

    def table(rows):
        head = '| 行 | 版本 | 捕获短编号 | 根键抄录 | 入选行 | 关系见证 | 证书抄录 | 备注 |\n| --- | --- | --- | --- | --- | --- | --- | --- |'
        body = ['| {} | {} | {} | {} | {} | {} | {} | {} |'.format(
            row, ver, f'`{short}`' if short != '—' else short, key, n, w, cert_, memo)
            for row, ver, short, key, n, w, cert_, memo in rows]
        return '\n'.join([head, *body])

    text = f"""# 交接台账（公开抄本）

> 本抄本可公开：捕获编号只留前 8 位，封存模式摘要与工作台页面地址从略，复合主键有缺抄。
> 随包核对材料：`state.sqlite`（工作台状态库）、`microscope.sqlite`（源库现状）、`downloads/`（已认证子库一份）。
> 同名配方多次交接：swatch 三版（v1 投用、v2 已停用、v3 模式变更后登记），mica-watch 两版（根的键拼写不同）。

## A 组 · swatch

{table(rows_a)}

## B 组 · mica-watch

{table(rows_b)}
"""
    (HANDOFF / 'ledger.md').write_text(text, encoding='utf-8')


def write_reconciliation(facts):
    A, B, C, D, E = (facts[k] for k in 'ABCDE')
    A8, B8, C8, D8, E8 = (facts[k][:8] for k in 'ABCDE')
    cert = facts['cert']
    old12, new12 = facts['old_schema'][:12], facts['new_schema'][:12]
    text = f"""# 台账核对报告

对 `ledger.md`（公开抄本）逐行核对的结果。核对以真实工作台状态为准：
`state.sqlite`（recipes 5 版 / captures 5 次 / 证书 1 份）与 `microscope.sqlite`（源库现状）。
本报告列出的页面路径与结论在生成时已逐条对运行中的工作台验证（见 `generate.py` 输出）。

## 1. 材料与启动

```sh
python3 -m keelsplice --repository handoff/state.sqlite \\
  --source microscope=handoff/microscope.sqlite --port 8794
# 打开 http://127.0.0.1:8794/workbench ，下列页面路径均相对此地址
```

交接时序（generate.py 剧本）：登记 swatch v1（根拼写 `east/MICA`，反向 assay，上限 20）→
登记 swatch v2（上限 1）并即停用 → v1 封存得捕获 A 并构建、认证、交付 → 源库 S-01 改长
3.25→4.75，v1 再封存得捕获 B → 源库 frame 加列 caption，登记 swatch v3 封存得捕获 C →
登记 mica-watch v1（拼写 `["east","MICA"]`）、v2（拼写 `["East","Mica"]`）分别封存得 D、E。

## 2. 权威来源与核对方法

权威顺序：状态库 recipes / captures 表与证书 ＞ 源库与关系目录（NOCASE 排序、主键列顺序）＞ 台账抄录。
每个台账行独立核对：先用行内线索在状态库中确定候选集，再按 §7 的规则补全缺抄；
候选唯一且其余抄录一致才算「可唯一核对」。

## 3. 结论总览

| 行 | 结论 | 定案依据 |
| --- | --- | --- |
| A1 | 可唯一核对 | 完整证书在证书集合中唯一 → 捕获 `{A8}`…（短编号亦唯一） |
| A2 | 可唯一核对 | 短编号 `{B8}` 全库唯一且与配方版本一致；缺抄的 code 按 §7 补全 |
| A3 | 多种解释 | 候选捕获 A / B / C 三个都符合行内线索 |
| A4 | 证据矛盾 | 短编号 `{B8}` 属 v1；swatch v2 已停用且没有任何捕获 |
| A5 | 可唯一核对 | 短编号 `{C8}` 全库唯一且与 v3 一致 |
| A6 | 证据矛盾 | 短编号 `{A8}` 所指捕获的实存根是 `["East","Mica"]`，与抄录的 `["West","Basalt"]` 不符 |
| B1 | 多种解释 | 捕获 D、E 命中相同看起来的键，行内没有任何区分线索 |
| B2 | 可唯一核对 | 短编号 `{E8}` 全库唯一；缺抄的 region 按 §7 补全 |
| B3 | 证据矛盾 | 短编号 `{E8}` 属 mica-watch v2，与抄录的 v1 不符 |

## 4. 可唯一核对的行（真实页面路径）

### A1 —— 同组中凭完整证书定位的一行

台账抄了完整证书 SHA-256 `{cert['sha256']}`（{cert['bytes']} 字节）。
状态库中只有捕获 A 持有证书，且其 `certificate.sha256` 与抄录一致 → 唯一定位。
交付文件 `downloads/{facts['fixture']}` 的字节摘要与证书相同，文件归属随之确定。

- 捕获详情（含证书）：`/workbench/captures/{A}`
- 认证子库下载（ETag 即证书摘要）：`/workbench/captures/{A}/database`
- 源配方：`/workbench/recipes/swatch/1`

### A2 —— 短编号唯一，部分键可补全

短编号 `{B8}` 在 captures 表全库唯一，归属 swatch v1，与抄录一致。
缺抄的 code 按 §7 规则补全：`["East", …]` 是主键 (region, code) 的前缀，源库中
region='East'（NOCASE）只有一行 → 补全为 `["East","Mica"]`，与捕获 B 的实存根一致。

- 捕获详情：`/workbench/captures/{B}`

### A5 —— 只留短编号的一行

短编号 `{C8}` 全库唯一，归属 swatch v3。捕获页「源库实存根」栏直接给出
`["East","Mica"]`，无需键抄录。备注「勿与旧捕获对比」与状态一致：v3 绑定的模式摘要
`{new12}`… 与 v1 的 `{old12}`… 不同，对比 A/C 返回 409。

- 捕获详情：`/workbench/captures/{C}`
- 源配方：`/workbench/recipes/swatch/3`

### B2 —— 短编号唯一，非前缀片段可补全

短编号 `{E8}` 全库唯一，归属 mica-watch v2。`[…, "Mica"]` 不是主键前缀，
但源库中 code='Mica'（NOCASE）只有一行，且与所属配方登记的根互相印证 →
补全为 `["East","Mica"]`。

- 捕获详情：`/workbench/captures/{E}`
- 源配方：`/workbench/recipes/mica-watch/2`

## 5. 仍有多种解释的行

### A3 —— 三个候选（swatch，根 `["East","Mica"]`，5 行 / 8 见证）

| 候选 | 配方版本 | 状态 | 模式摘要 | 内容区分点 |
| --- | --- | --- | --- | --- |
| `{A8}`… | swatch v1 | 已认证 | `{old12}`… | S-01 封存时为 3.25，持有证书 |
| `{B8}`… | swatch v1 | 已封存 | `{old12}`… | S-01 封存时为 4.75 |
| `{C8}`… | swatch v3 | 已封存 | `{new12}`… | frame 已加 caption 列 |

行内没有编号、版本、证书或内容线索，三个候选全部符合。即使假定「版本 1」仍剩
A、B 两个。对比页能区分 A/B（assay 修改 `["S-01"]`），但台账没有抄内容，对比结果
无法回挂到这一行。要定案需要补抄：短编号、或证书摘要、或版本加内容线索。

### B1 —— 两个捕获命中相同看起来的键，仍没有唯一归属

| 候选 | 配方版本 | 登记根拼写 | 实存根 |
| --- | --- | --- | --- |
| `{D8}`… | mica-watch v1 | `["east","MICA"]` | `["East","Mica"]` |
| `{E8}`… | mica-watch v2 | `["East","Mica"]` | `["East","Mica"]` |

substrate 主键是 NOCASE，两个版本的输入拼写命中同一实存行；两次封存的实存根、
逐表计数、行内容完全一致（对比页显示「这两个捕获的行内容一致」）。台账抄的键与
两个捕获的实存根看起来相同，键线索对两者同时成立。生成时已验证：C、D、E 的封存
内容相同，即使补做认证，重建子库字节也相同，证书文件摘要同样无法区分 D、E。
唯一能把这一行定下来的线索是捕获编号或配方版本，而 B1 两者都缺 → 没有唯一归属。

## 6. 证据矛盾的行

### A4 —— 「v2 停用前抢封的一次」不成立

短编号 `{B8}` 在状态库中属 swatch **v1**（捕获页 `/workbench/captures/{B}` 显示
「swatch · 版本 1」）；配方页 `/workbench/recipes/swatch/2` 显示「已停用」，且
captures 表中不存在 version=2 的行。编号归属与版本抄录、备注叙述三处互相矛盾。

### A6 —— 根抄录与编号所指的捕获不符

短编号 `{A8}` 指向捕获 A，其实存根为 `["East","Mica"]`。在
`/workbench/captures/{A}` 的追踪表单提交 `["West","Basalt"]` 返回
「封存结果中没有这个实存主键」。源库确有 West/Basalt 行，但它不在此次捕获的闭包内。

### B3 —— 「第一版的封存」张冠李戴

短编号 `{E8}` 属 mica-watch **v2**（捕获页 `/workbench/captures/{E}` 显示
「mica-watch · 版本 2」）；v1 的封存是 `{D8}`…（`/workbench/captures/{D}`）。

## 7. 允许用于补全的跨行线索

允许（全部落回权威状态，不落在台账他行）：

1. **短编号前缀唯一性**：短编号在 captures 表全库前缀匹配唯一，且行内其余抄录
   与该捕获一致时，可补全为完整编号（本库 5 个 8 位短编号已验证互不相同）。
2. **捕获 → 配方归属**：captures.(name, version) 外键到 recipes，可核对
   「配方 + 版本 + 编号」三元组，并取出配方登记的根拼写与行上限。
3. **目录的排序规则与主键顺序**：substrate 主键 (region, code) 为 NOCASE，
   输入拼写 `east/MICA`、`East/Mica` 等都归一到实存键 `["East","Mica"]`；
   部分键只在「按主键顺序的前缀」（如 `["East", …]`）或「片段在源库中唯一匹配」
   （如 code='Mica' 只有一行）且与所属配方登记根互相印证时才允许补全。
4. **证书链**：完整证书 SHA-256 ↔ 证书字节数 ↔ 下载文件字节 ↔ capture_digest。
   证书在证书集合中唯一时才可用来定位（本库已验证唯一）；注意证书绑定的是内容，
   内容相同的捕获（C、D、E）若认证会共享同一文件摘要。
5. **封存模式摘要分组**：schema_digest 相同才可对比、才可谈「同内容」；不同则
   排除「可对比」，但不排除「归属」（A3 的候选 C 模式不同，仍是合法候选）。
6. **对比结果**：同模式捕获对的行级差异（A/B 修改 S-01）或「行内容一致」（D/E）
   用来说明候选之间的可区分性，不直接决定归属。

不允许：

- 用台账他行的抄录补全本行归属。台账整体是被核对对象；他行即使已确认，也只能
  把结论落回状态库再核，不能行间「借」证据（例如不得因 A1 已确认 A 有证书，
  就把 A3 归给 A）。
- 把输入拼写当实存键直接追踪。追踪只认实存键：`["east","MICA"]` 在捕获 A 的
  追踪表单返回「封存结果中没有这个实存主键」；拼写归一必须经目录规则。
- 凭行数、见证数或行内容相同合并或拆分捕获（A、B 计数相同仍是两次捕获；
  D、E 内容相同仍是两次捕获）。
- 凭备注叙述、交接批次或时间先后定归属。
- 短编号不唯一时凭其他线索「就近」认领（本库未出现，出现时该行只能降级为待补）。

## 8. 复核命令

```sh
# 状态库直查（只读）
python3 - <<'EOF'
import sqlite3
conn = sqlite3.connect('file:handoff/state.sqlite?mode=ro', uri=True)
print(conn.execute('SELECT substr(id,1,8), name, version, state FROM captures ORDER BY rowid').fetchall())
print(conn.execute('SELECT name, version, active, substr(schema_digest,1,12) FROM recipes ORDER BY name, version').fetchall())
print(conn.execute("SELECT substr(id,1,8), json_extract(certificate,'$.sha256') FROM captures WHERE certificate IS NOT NULL").fetchall())
EOF

# 交付文件对证书（应等于 A1 抄录的 SHA-256）
python3 -c "import hashlib,pathlib;print(hashlib.sha256(pathlib.Path('handoff/downloads/{facts['fixture']}').read_bytes()).hexdigest())"
```

页面清单（生成时已验证返回 200）：`/workbench`、`/workbench/recipes`、
`/workbench/captures`、`/workbench/compare`、`/workbench/recipes/swatch/1..3`、
`/workbench/recipes/mica-watch/1..2`，以及 §4 各捕获详情页。
追踪与对比是 POST 表单：追踪在捕获详情页提交表名与 JSON 主键元组（如
`substrate` + `["East","Mica"]`）；对比在 `/workbench/compare` 选择两个完整捕获编号。
"""
    (HANDOFF / 'reconciliation.md').write_text(text, encoding='utf-8')


def write_readme(facts):
    text = f"""# 残缺台账交接包（Keelsplice）

接班人收到的材料与核对结论。两组同名配方多次交接，台账公开抄本有缺抄与错抄。

## 文件

- `ledger.md` —— 收到的台账公开抄本（短编号、缺抄的复合键、一条完整证书）
- `reconciliation.md` —— 核对报告：可唯一核对 / 多种解释 / 证据矛盾，
  附真实页面路径与允许用于补全的跨行线索
- `state.sqlite` —— 工作台状态库（5 个配方版本、5 次捕获、1 份证书）
- `microscope.sqlite` —— 源库现状（S-01 已改长 4.75、frame 已加 caption 列）
- `downloads/{facts['fixture']}` —— 已认证交付子库（SHA-256 同台账 A1 行证书）
- `generate.py` —— 生成本包的剧本脚本

## 启动工作台核对

```sh
python3 -m keelsplice --repository handoff/state.sqlite \\
  --source microscope=handoff/microscope.sqlite --port 8794
# 浏览器打开 http://127.0.0.1:8794/workbench
```

## 重新生成本包

```sh
python3 handoff/generate.py --force
```

捕获编号与证书摘要每次生成都会变化，文档随状态一起重写。
"""
    (HANDOFF / 'README.md').write_text(text, encoding='utf-8')


def main():
    if STATE.exists() and '--force' not in sys.argv:
        raise SystemExit('handoff/state.sqlite 已存在；加 --force 重建')
    reset()
    seed(SOURCE)
    with create_server(('127.0.0.1', 0), STATE, {'microscope': SOURCE}) as server:
        thread = Thread(target=server.serve_forever, kwargs={'poll_interval': 0.05})
        thread.start()
        base = f'http://127.0.0.1:{server.server_port}'
        try:
            facts = scenario(base)
            check_state(facts)
            check_pages(base, facts)
            check_fixture(facts)
        finally:
            server.shutdown()
            thread.join()
    with closing(sqlite3.connect(SOURCE)) as conn:
        conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        conn.execute('PRAGMA journal_mode=DELETE')
    write_ledger(facts)
    write_reconciliation(facts)
    write_readme(facts)
    print(f'\n完成：{len(CHECKS)} 项核对全部通过；台账 9 行 = 可唯一核对 4 + 多种解释 2 + 证据矛盾 3')


if __name__ == '__main__':
    main()

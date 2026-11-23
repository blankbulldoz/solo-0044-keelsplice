#!/usr/bin/env python3
"""争议交接对照复跑器（数据组 vs 验证组：反向边是否必要）。

在全新临时目录中用 examples/microscope.sql 播种源库，经真实 JSON HTTP 接口
登记 A/B/C/D 四组配方（每组含 follow_children 开/关两个反事实变体），封存
捕获、抽取行集合与直接见证，再用独立只读 SQLite 连接做第三方核对，最后与
仓库内冻结契约 frozen-contract.json 逐项比对。

用法：
  python3 -I examples/dispute_handoff/replay_dispute.py                 # 复跑并对照冻结契约
  python3 -I examples/dispute_handoff/replay_dispute.py --out <目录>    # 同时保留全部实物
  python3 -I examples/dispute_handoff/replay_dispute.py --freeze        # 复跑通过后重写冻结契约

退出码 0 表示全部断言与冻结契约一致；非 0 表示漂移或断言失败。
"""

import argparse
import hashlib
import json
import sqlite3
import sys
import time
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from urllib.error import HTTPError
from urllib.request import Request, urlopen

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT))

from keelsplice.api import create_server  # noqa: E402
from examples.walkthrough import seed  # noqa: E402

CONTRACT_PATH = HERE / 'frozen-contract.json'
ROW_LIMIT = 50


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, 'rb') as handle:
        for chunk in iter(lambda: handle.read(1 << 16), b''):
            h.update(chunk)
    return h.hexdigest()


def call(base, route, body=None, status=200):
    received, payload = raw(base, route, body)
    if received != status:
        raise AssertionError(f'{route} 期望 {status}，实际 {received}: {json.dumps(payload)[:300]}')
    return payload


def raw(base, route, body=None):
    headers = {'Content-Type': 'application/json'}
    data = json.dumps(body).encode('utf-8') if body is not None else None
    try:
        response = urlopen(Request(base + route, data, headers), timeout=10)
    except HTTPError as exc:
        response = exc
    with response:
        return response.status, json.loads(response.read())


def trace_one(base, capture_id, probe):
    """探测一行；未纳入时返回 404 的错误码作为“缺席”实物，而非抛异常。"""
    status, payload = raw(base, f'/captures/{capture_id}/trace', probe)
    assert status in (200, 404), (status, payload)
    return {'http_status': status, 'body': payload}


def edge_id(catalog, child, parent):
    matches = [e for e in catalog['schema']['edges'] if e['child'] == child and e['parent'] == parent]
    assert len(matches) == 1, f'目录中 {child}->{parent} 边不唯一: {matches}'
    return matches[0]['id']


def compact_catalog(catalog):
    return {
        'schema_digest': catalog['schema_digest'],
        'edges': [
            {k: edge[k] for k in ('id', 'child', 'parent', 'child_cols', 'parent_cols')}
            for edge in catalog['schema']['edges']
        ],
        'primary_keys': {name: meta['pk'] for name, meta in catalog['schema']['tables'].items()},
    }


def independent_sql_checks(source):
    """不经工作台的第三方只读核对，作为两组都能接受的实物依据。"""
    uri = source.as_uri() + '?mode=ro'
    with closing(sqlite3.connect(uri, uri=True)) as conn:
        east_mica = [r[0] for r in conn.execute(
            "SELECT mark FROM assay WHERE region='East' COLLATE NOCASE "
            "AND code='Mica' COLLATE NOCASE ORDER BY mark")]
        cassette_prior = conn.execute('SELECT serial, prior FROM cassette ORDER BY serial').fetchall()
        s03 = conn.execute("SELECT region, code, cassette FROM assay WHERE mark='S-03'").fetchone()
        frame_rows = conn.execute('SELECT frame_id, mark FROM frame ORDER BY frame_id').fetchall()
        page_size = conn.execute('PRAGMA page_size').fetchone()[0]
    return {
        'assay_rows_matching_east_mica_nocase': east_mica,
        'cassette_serial_prior_pairs': [list(pair) for pair in cassette_prior],
        'assay_s03_region_code_cassette': list(s03),
        'frame_id_mark_pairs': [list(row) for row in frame_rows],
        'sqlite_page_size': page_size,
    }


def run_variant(base, tag, roots, follow):
    body = {'source': 'microscope', 'roots': roots,
            'follow_children': follow, 'row_limit': ROW_LIMIT}
    version = call(base, f'/recipes/{tag}/versions', body, 201)['version']
    preview = call(base, '/preview', {'name': tag, 'version': version})
    capture = call(base, '/captures', {'name': tag, 'version': version}, 201)
    endpoint = '/captures/' + capture['id']
    call(base, endpoint + '/build', {})
    certificate = call(base, endpoint + '/certify', {})['certificate']
    return {
        'recipe_version': version,
        'capture_id': capture['id'],
        'summary': preview['summary'],
        'roots': capture['roots'],
        'certificate': certificate,
    }


def traces(base, capture_id, probes):
    return {label: trace_one(base, capture_id, probe) for label, probe in probes.items()}


def scenarios(base, catalog):
    assay_sub = edge_id(catalog, 'assay', 'substrate')
    cass_self = edge_id(catalog, 'cassette', 'cassette')

    substrate_root = [{'table': 'substrate', 'key': ['east', 'MICA']}]
    s01 = {'table': 'assay', 'key': ['S-01']}
    s02 = {'table': 'assay', 'key': ['S-02']}
    s03 = {'table': 'assay', 'key': ['S-03']}
    cassette_11 = [{'table': 'cassette', 'key': [11]}]

    definitions = [
        {
            'id': 'A',
            'title': '单根 substrate(East,Mica)，争议边 assay→substrate 反向开关',
            'roots': substrate_root,
            'follow_on': [{'child': 'assay', 'id': assay_sub}],
            'probes': {
                'assay_S-01': s01, 'assay_S-02': s02,
                'substrate_East_Mica': {'table': 'substrate', 'key': ['East', 'Mica']},
                'cassette_11': {'table': 'cassette', 'key': [11]},
                'cassette_12': {'table': 'cassette', 'key': [12]},
            },
        },
        {
            'id': 'B',
            'title': '双根 substrate(East,Mica)+assay(S-01)，同一争议边反向开关（另一个根改变结论）',
            'roots': [substrate_root[0], s01],
            'follow_on': [{'child': 'assay', 'id': assay_sub}],
            'probes': {'assay_S-01': s01, 'assay_S-02': s02,
                       'substrate_East_Mica': {'table': 'substrate', 'key': ['East', 'Mica']}},
        },
        {
            'id': 'C',
            'title': '根 cassette(11)，自引用边 cassette→cassette 反向开关（环改变结论）',
            'roots': cassette_11,
            'follow_on': [{'child': 'cassette', 'id': cass_self}],
            'probes': {
                'cassette_11': {'table': 'cassette', 'key': [11]},
                'cassette_12': {'table': 'cassette', 'key': [12]},
            },
        },
        {
            'id': 'D',
            'title': '根 assay(S-03)，复合外键含 NULL，不选任何反向边（见证边界补充）',
            'roots': [s03],
            'follow_on': [],
            'probes': {'assay_S-03': s03, 'cassette_13': {'table': 'cassette', 'key': [13]}},
        },
    ]

    results = []
    for definition in definitions:
        on = run_variant(base, f"sc{definition['id'].lower()}on", definition['roots'], definition['follow_on'])
        off = run_variant(base, f"sc{definition['id'].lower()}off", definition['roots'], [])
        on['traces'] = traces(base, on['capture_id'], definition['probes'])
        off['traces'] = traces(base, off['capture_id'], definition['probes'])
        results.append({'id': definition['id'], 'title': definition['title'],
                        'roots_recipe': definition['roots'],
                        'follow_children_on': definition['follow_on'], 'on': on, 'off': off})
    return results


def captured(trace_record):
    return trace_record['http_status'] == 200


def evaluate(scenario):
    """对每条争议行给出可断言结论：必要 / 非必要（注明机制）/ 当前仅有见证。"""
    sid = scenario['id']
    on, off = scenario['on']['traces'], scenario['off']['traces']
    rows = []
    if sid == 'A':
        for mark in ('S-01', 'S-02'):
            label = f'assay_{mark}'
            assert captured(on[label]) and not captured(off[label])
            rows.append({
                'target': ['assay', [mark]],
                'present_with_reverse': True, 'present_without_reverse': False,
                'verdict': 'necessary',
                'basis': '关闭反向后 trace 返回 404；该根下 assay 只可能由反向 child 见证进入',
            })
        assert captured(on['cassette_11']) and not captured(off['cassette_11'])
        rows.append({
            'target': ['cassette', [11]],
            'present_with_reverse': True, 'present_without_reverse': False,
            'verdict': 'necessary_transitively',
            'basis': 'cassette 由 assay 的父依赖带入；没有反向就没有 assay，连带消失（父见证存在不代表反向无关）',
        })
        assert captured(on['substrate_East_Mica']) and captured(off['substrate_East_Mica'])
    elif sid == 'B':
        assert captured(on['assay_S-01']) and captured(off['assay_S-01'])
        assert on['assay_S-01']['body']['roots'] == [1]
        rows.append({
            'target': ['assay', ['S-01']],
            'present_with_reverse': True, 'present_without_reverse': True,
            'verdict': 'not_necessary_root_witness',
            'basis': 'S-01 本身是第 2 个根（trace.roots=[1]）；关掉反向仍在，只是丢失 child 见证',
        })
        assert captured(on['assay_S-02']) and not captured(off['assay_S-02'])
        rows.append({
            'target': ['assay', ['S-02']],
            'present_with_reverse': True, 'present_without_reverse': False,
            'verdict': 'necessary',
            'basis': 'S-02 不是根，关闭反向后 404；同一条边对不同目标行结论不同',
        })
    elif sid == 'C':
        assert captured(on['cassette_12']) and captured(off['cassette_12'])
        on_causes = on['cassette_12']['body']['causes']
        off_causes = off['cassette_12']['body']['causes']
        assert {arc['direction'] for arc in on_causes} == {'child', 'parent'}
        assert [arc['direction'] for arc in off_causes] == ['parent']
        rows.append({
            'target': ['cassette', [12]],
            'present_with_reverse': True, 'present_without_reverse': True,
            'verdict': 'not_necessary_cycle_parent_closure',
            'basis': '11.prior=12 的父向闭包无论反向开关都纳入 12；环上反向只多一条 child 见证',
        })
    elif sid == 'D':
        assert captured(on['assay_S-03']) and on['assay_S-03']['body']['causes'] == []
        assert [d['edge'] for d in on['assay_S-03']['body']['dependencies']] == [['assay', 1]]
        rows.append({
            'target': ['assay', ['S-03']],
            'present_with_reverse': True, 'present_without_reverse': True,
            'verdict': 'root_only_witness_boundary',
            'basis': '根行 causes=[]；复合外键 code 分量为 NULL，按约定无 substrate 父行，只能断言当前无直接纳入见证',
        })
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', help='保留源库、状态库与逐场景证据 JSON 的目录')
    parser.add_argument('--freeze', action='store_true', help='复跑通过后重写冻结契约')
    args = parser.parse_args()

    keep = TemporaryDirectory(prefix='keelsplice-dispute-')
    work = Path(args.out) if args.out else Path(keep.name)
    work.mkdir(parents=True, exist_ok=True)
    source = work / 'microscope.sqlite'
    repository = work / 'state.sqlite'
    seed(source)
    source_sha = sha256_file(source)

    server = create_server(('127.0.0.1', 0), repository, {'microscope': source})
    server.RequestHandlerClass.log_message = lambda *args: None
    thread = Thread(target=server.serve_forever, kwargs={'poll_interval': 0.05})
    thread.start()
    base = f'http://127.0.0.1:{server.server_port}'
    try:
        catalog = call(base, '/sources/microscope/catalog')
        frozen_now = {
            'generated_at_utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
            'source': {
                'sql_file': 'examples/microscope.sql',
                'seeded_database_sha256': source_sha,
            },
            'catalog': compact_catalog(catalog),
            'independent_sql': independent_sql_checks(source),
            'scenarios': scenarios(base, catalog),
        }
    finally:
        server.shutdown()
        thread.join()

    for scenario in frozen_now['scenarios']:
        scenario['verdicts'] = evaluate(scenario)

    if args.out:
        (work / 'evidence.json').write_text(
            json.dumps(frozen_now, ensure_ascii=False, indent=2, sort_keys=True), encoding='utf-8')
        (work / 'README-run.txt').write_text(
            '本目录由 examples/dispute_handoff/replay_dispute.py 生成。\n'
            'microscope.sqlite 为冻结源库实物（SHA-256 见 evidence.json 与契约）；\n'
            'state.sqlite 为工作台状态库，内含全部配方版本与封存捕获，可用只读连接复核。\n',
            encoding='utf-8')

    if args.freeze:
        CONTRACT_PATH.write_text(
            json.dumps(frozen_now, ensure_ascii=False, indent=2, sort_keys=True) + '\n',
            encoding='utf-8')
        print(f'[freeze] 已重写 {CONTRACT_PATH}')
        return 0

    frozen_contract = json.loads(CONTRACT_PATH.read_text(encoding='utf-8'))
    # capture_id 是每次运行新生成的不透明标识，不参与冻结比对；证书 capture_digest 参与。
    def strip_runtime(node):
        if isinstance(node, dict):
            return {k: strip_runtime(v) for k, v in node.items() if k not in ('capture_id', 'generated_at_utc')}
        if isinstance(node, list):
            return [strip_runtime(v) for v in node]
        return node

    expected = strip_runtime(frozen_contract)
    actual = strip_runtime(frozen_now)
    if actual != expected:
        print('[FAIL] 复跑结果与冻结契约不一致，差异定位：', file=sys.stderr)
        _diff(expected, actual, (), file=sys.stderr)
        return 1
    print('[PASS] 8 个反事实变体、trace 见证、证书摘要与独立 SQL 核对均与冻结契约一致')
    for scenario in frozen_now['scenarios']:
        for verdict in scenario['verdicts']:
            print(f"  场景 {scenario['id']} {verdict['target'][0]}({verdict['target'][1]}) -> {verdict['verdict']}")
    if args.out:
        print(f'[keep] 实物与证据保留在 {work}')
    return 0


def _diff(expected, actual, path, file):
    if type(expected) is not type(actual):
        print(f'  类型差异 @ {".".join(map(str, path))}: {type(expected).__name__} != {type(actual).__name__}', file=file)
        return
    if isinstance(expected, dict):
        for key in sorted(set(expected) | set(actual)):
            if key not in expected:
                print(f'  新增字段 @ {".".join(map(str, path + (key,)))}: {actual[key]!r}', file=file)
            elif key not in actual:
                print(f'  缺失字段 @ {".".join(map(str, path + (key,)))}: {expected[key]!r}', file=file)
            else:
                _diff(expected[key], actual[key], path + (key,), file)
    elif isinstance(expected, list):
        if len(expected) != len(actual):
            print(f'  列表长度差异 @ {".".join(map(str, path))}: {len(expected)} != {len(actual)}', file=file)
        for index, (a, b) in enumerate(zip(expected, actual)):
            _diff(a, b, path + (index,), file)
    elif expected != actual:
        print(f'  值差异 @ {".".join(map(str, path))}: {expected!r} != {actual!r}', file=file)


if __name__ == '__main__':
    raise SystemExit(main())

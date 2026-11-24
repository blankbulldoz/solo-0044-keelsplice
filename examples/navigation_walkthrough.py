"""Verify 查看此行 navigation with a counterexample that defeats endpoint-only checks.

The loop.sql fixture closes over a two-row cycle (loop references itself) plus a
probe row pointing into the cycle. The stored beta row is reached by two arcs
whose direction and edge evidence differ: 配方反向纳入 through the cyclic loop
edge (departing row carries a long typed key) and 必需父关系 through the probe
edge. Expectations are built independently from the source file, cross-checked
against the JSON trace API, and then pinned to every rendered 查看此行 form.
"""

import hashlib
import json
import re
import sqlite3
from contextlib import closing
from html import unescape
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread

from keelsplice.api import create_server
from keelsplice.codec import dumps, encode

from .browser_walkthrough import html, open_page
from .walkthrough import call

CAUSES = '直接入选原因'
DEPENDENCIES = '指向的必需父行'
LABELS = {'parent': '必需父关系', 'child': '配方反向纳入'}

FACTS = re.compile(
    r'<dl class="facts"><div><dt>表</dt><dd><code>(?P<table>[^<]+)</code></dd></div>'
    r'<div><dt>实存主键</dt><dd><code>(?P<key>[^<]+)</code></dd></div>'
    r'<div><dt>根序号</dt><dd>(?P<roots>[^<]+)</dd></div></dl>')
ARC_ROW = re.compile(
    r'<tr><td>(?P<label>[^<]+)</td><td><code>(?P<edge_table>[^<]+)</code> #(?P<edge_id>[0-9]+)</td>'
    r'<td><code>(?P<row_table>[^<]+)</code> <code>(?P<row_key>[^<]+)</code></td>'
    r'<td><form method="post" action="(?P<action>[^"]+)">'
    r'<input type="hidden" name="table" value="(?P<form_table>[^"]*)">'
    r'<input type="hidden" name="key" value="(?P<form_key>[^"]*)">'
    r'<button class="quiet">查看此行</button></form></td></tr>')
ROOT_ROW = re.compile(
    r'<form class="inline" method="post" action="(?P<action>[^"]+)">'
    r'<input type="hidden" name="table" value="(?P<form_table>[^"]*)">'
    r'<input type="hidden" name="key" value="(?P<form_key>[^"]*)">'
    r'<span><code>(?P<row_table>[^<]+)</code> <code>(?P<row_key>[^<]+)</code></span>'
    r'<button class="quiet">追踪此根</button></form>')


def seed(file):
    with closing(sqlite3.connect(file)) as conn:
        conn.executescript(Path(__file__).with_name('loop.sql').read_text())


def source_facts(path):
    """Read the fixture directly so expectations never come from the service."""
    with closing(sqlite3.connect(path)) as conn:
        loops = conn.execute(
            'SELECT token, serial, ratio, prior_token, prior_serial, prior_ratio, note'
            ' FROM loop ORDER BY token').fetchall()
        probes = conn.execute('SELECT mark, loop_token, loop_serial, loop_ratio, reading FROM probe').fetchall()
    assert len(loops) == 2 and len(probes) == 1
    return loops, probes


def expected_arcs(alpha, beta, probe):
    """The six witnesses the fixture must produce, as an auditable literal spec."""
    arcs = [
        dict(origin=['loop', alpha], target=['loop', beta], edge=['loop', 0], direction='parent'),
        dict(origin=['loop', alpha], target=['loop', beta], edge=['loop', 0], direction='child'),
        dict(origin=['loop', beta], target=['loop', alpha], edge=['loop', 0], direction='parent'),
        dict(origin=['loop', beta], target=['loop', alpha], edge=['loop', 0], direction='child'),
        dict(origin=['loop', beta], target=['probe', probe], edge=['probe', 0], direction='child'),
        dict(origin=['probe', probe], target=['loop', beta], edge=['probe', 0], direction='parent'),
    ]
    return sorted(arcs, key=dumps)


def expected_trace(arcs, roots, address):
    """Reference projection of the documented trace contract over our own arcs."""
    return {
        'roots': [index for index, root in enumerate(roots) if root == address],
        'causes': [arc for arc in arcs if arc['target'] == address],
        'dependencies': [arc for arc in arcs if arc['origin'] == address and arc['direction'] == 'parent'],
    }


def arc_rows(page, heading):
    """Parse one evidence section into rows that each carry a submitted key."""
    part = page.split(f'<h2>{heading}</h2>', 1)[1].split('</section>', 1)[0]
    found = []
    for match in ARC_ROW.finditer(part):
        row = {name: unescape(value) for name, value in match.groupdict().items()}
        row['edge_id'] = int(row['edge_id'])
        row['section'] = heading
        found.append(row)
    assert found or '无直接记录。' in part, heading
    return found


def check_arc_rows(parsed, arcs, opposite, capture_id):
    """Pin departure-page edge identifiers and submitted keys to the model."""
    assert len(parsed) == len(arcs), (parsed, arcs)
    for row, arc in zip(parsed, arcs):
        assert row['label'] == LABELS[arc['direction']], (row, arc)
        assert [row['edge_table'], row['edge_id']] == arc['edge'], (row, arc)
        table, key = arc[opposite]
        assert (row['row_table'], row['row_key']) == (table, dumps(key)), (row, arc)
        assert row['action'] == f'/workbench/captures/{capture_id}/trace', row
        # 从该行提交的键必须就是所示对端行的实存类型化键
        assert (row['form_table'], row['form_key']) == (table, dumps(key)), (row, arc)


def check_evidence_page(page, capture_id, address, model):
    """Pin the arrival page identity and both evidence sections to the model."""
    facts = FACTS.search(page)
    assert facts, '证据页缺少事实表'
    assert unescape(facts['table']) == address[0]
    assert unescape(facts['key']) == dumps(address[1])
    occurrences = ', '.join(str(index + 1) for index in model['roots']) or '无'
    assert unescape(facts['roots']) == occurrences
    check_arc_rows(arc_rows(page, CAUSES), model['causes'], 'origin', capture_id)
    check_arc_rows(arc_rows(page, DEPENDENCIES), model['dependencies'], 'target', capture_id)


def follow(base, page, heading, label, edge):
    """Submit the 查看此行 form of one specific arc row on the departure page."""
    matches = [row for row in arc_rows(page, heading)
               if row['label'] == label and [row['edge_table'], row['edge_id']] == list(edge)]
    assert len(matches) == 1, (heading, label, edge, matches)
    row = matches[0]
    _, arrival = html(base, row['action'], {'table': row['form_table'], 'key': row['form_key']})
    return row, arrival


def endpoints_only(page):
    """What a verifier that only compares page endpoints can observe."""
    return {row['form_key'] for heading in (CAUSES, DEPENDENCIES) for row in arc_rows(page, heading)}


def run(base, source, root):
    loops, probes = source_facts(source)
    alpha_row, beta_row, probe_row = loops[0], loops[1], probes[0]
    alpha = [encode(value) for value in alpha_row[:3]]
    beta = [encode(value) for value in beta_row[:3]]
    probe = [probe_row[0]]
    arcs = expected_arcs(alpha, beta, probe)
    addresses = {'alpha': ['loop', alpha], 'beta': ['loop', beta], 'probe': ['probe', probe]}
    models = {name: expected_trace(arcs, [addresses['alpha']], address)
              for name, address in addresses.items()}

    # 反例结构自检：六条证据坍缩成四个端点对，只比较页面终点必然丢失证据
    endpoints = {(dumps(arc['origin']), dumps(arc['target'])) for arc in arcs}
    assert len(arcs) == 6 and len(endpoints) == 4

    _, directory = html(base, '/workbench?source=loop&table=loop')
    assert '自引用' in directory and 'token → serial → ratio' in directory

    _, editor = html(base, '/workbench/recipes/new?source=loop')
    digest = re.search(r'name="digest" value="([0-9a-f]{64})"', editor)[1]
    alias_root = [alpha_row[0].upper(), alpha_row[1], alpha[2]]
    form = {'source': 'loop', 'name': 'loop-swatch', 'digest': digest, 'row_limit': '50',
            'edge': ['0:loop', '0:probe'],
            'roots': json.dumps([{'table': 'loop', 'key': alias_root}])}
    version, page = html(base, '/workbench/recipes/new', form)
    assert version.endswith('/1') and '投用中' in page

    _, preview = html(base, version + '/preview', {})
    assert '未封存' in preview and '<code>loop</code></th><td>2</td>' in preview
    assert '<code>probe</code></th><td>1</td>' in preview and dumps(alpha) in unescape(preview)
    capture, detail = html(base, version + '/capture', {})
    assert '已封存' in detail and '<dt>入选行</dt><dd>3</dd>' in detail
    assert '<dt>关系见证</dt><dd>6</dd>' in detail
    capture_id = capture.rsplit('/', 1)[1]

    # 每一步的期望先独立锚定到 JSON 接口，再核对浏览器页面
    for name, address in addresses.items():
        traced = call(base, f'/captures/{capture_id}/trace', {'table': address[0], 'key': address[1]})
        assert traced['row'] == address and traced['roots'] == models[name]['roots']
        assert traced['causes'] == models[name]['causes']
        assert traced['dependencies'] == models[name]['dependencies']

    # 入口：捕获详情的「追踪此根」提交根实存键，到达 alpha 证据页
    roots_found = [{k: unescape(v) for k, v in match.groupdict().items()}
                   for match in ROOT_ROW.finditer(detail)]
    assert len(roots_found) == 1
    assert (roots_found[0]['form_table'], roots_found[0]['form_key']) == ('loop', dumps(alpha))
    _, page_alpha = html(base, roots_found[0]['action'],
                         {'table': roots_found[0]['form_table'], 'key': roots_found[0]['form_key']})
    check_evidence_page(page_alpha, capture_id, addresses['alpha'], models['alpha'])

    # 端点坍缩实证：alpha 页三个「查看此行」提交同一键、到达同一页，证据含义却不同
    assert endpoints_only(page_alpha) == {dumps(beta)}
    assert len({(row['section'], row['label'])
                for heading in (CAUSES, DEPENDENCIES) for row in arc_rows(page_alpha, heading)}) == 3

    # 被反例打破的验证：把方向标签改掉后，终点比较依旧“通过”，本验证立即拒绝
    mutated = page_alpha.replace(LABELS['child'], LABELS['parent'])
    assert endpoints_only(mutated) == endpoints_only(page_alpha)
    try:
        check_evidence_page(mutated, capture_id, addresses['alpha'], models['alpha'])
    except AssertionError:
        pass
    else:
        raise AssertionError('篡改了方向标签的页面不应通过核验')

    # 步骤一：穿环的配方反向纳入边，从 alpha 提交长类型化键到达 beta
    row, page_beta = follow(base, page_alpha, CAUSES, LABELS['child'], ('loop', 0))
    assert row['form_key'] == dumps(beta) and len(row['form_key']) > 60 and '"real"' in row['form_key']
    check_evidence_page(page_beta, capture_id, addresses['beta'], models['beta'])

    # 反例核心：两条边到达相同实存行 beta，方向与证据不同
    causes_beta = arc_rows(page_beta, CAUSES)
    via_cycle = [row for row in causes_beta
                 if row['label'] == LABELS['child'] and [row['edge_table'], row['edge_id']] == ['loop', 0]]
    via_probe = [row for row in causes_beta
                 if row['label'] == LABELS['parent'] and [row['edge_table'], row['edge_id']] == ['probe', 0]]
    assert len(via_cycle) == 1 and len(via_probe) == 1
    assert via_cycle[0]['form_key'] == dumps(alpha)  # 穿环一支提交的长类型化键
    assert via_probe[0]['form_key'] == dumps(probe)
    assert via_cycle[0]['form_key'] != via_probe[0]['form_key']

    # 步骤二：从 beta 沿 probe 的必需父关系证据到达 probe 行
    _, page_probe = follow(base, page_beta, CAUSES, LABELS['parent'], ('probe', 0))
    check_evidence_page(page_probe, capture_id, addresses['probe'], models['probe'])

    # 步骤三：从 probe 的必需父行回到 beta，同一证据页经不同边往返
    _, page_beta_again = follow(base, page_probe, DEPENDENCIES, LABELS['parent'], ('probe', 0))
    check_evidence_page(page_beta_again, capture_id, addresses['beta'], models['beta'])

    # 步骤四：从 beta 的必需父行回到 alpha，导航闭环
    _, page_alpha_again = follow(base, page_beta_again, DEPENDENCIES, LABELS['parent'], ('loop', 0))
    check_evidence_page(page_alpha_again, capture_id, addresses['alpha'], models['alpha'])

    # 构建并认证，记录证书记载的字节摘要
    assert '已构建' in html(base, capture + '/build', {})[1]
    assert '已认证' in html(base, capture + '/certify', {})[1]
    certificate = call(base, '/captures/' + capture_id)['certificate']

    # 错误键回传：probe 行保存的大写拼写不是捕获里的实存主键
    wrong_key = [probe_row[1], beta_row[1], beta[2]]
    _, error = html(base, capture + '/trace', {'table': 'loop', 'key': dumps(wrong_key)}, expected=404)
    assert '实存主键' in error

    # 从同一次捕获下载并独立核验字节，确认失败没有影响认证成果
    _, headers, data = open_page(base, capture + '/database')
    sha = hashlib.sha256(data).hexdigest()
    assert headers['ETag'] == f'"{sha}"'
    assert sha == certificate['sha256'] and len(data) == certificate['bytes']
    handoff = root / 'handoff.sqlite'
    handoff.write_bytes(data)
    with closing(sqlite3.connect(handoff.as_uri() + '?mode=ro', uri=True)) as conn:
        assert [row[0] for row in conn.execute('PRAGMA integrity_check')] == ['ok']
        assert conn.execute('PRAGMA foreign_key_check').fetchall() == []
        assert conn.execute('SELECT token, serial, ratio, prior_token, prior_serial, prior_ratio, note'
                            ' FROM loop ORDER BY token').fetchall() == loops
        assert conn.execute('SELECT mark, loop_token, loop_serial, loop_ratio, reading'
                            ' FROM probe').fetchall() == probes
        assert conn.execute('SELECT count(*) FROM loop').fetchone()[0] == certificate['counts']['loop']
        assert conn.execute('SELECT count(*) FROM probe').fetchone()[0] == certificate['counts']['probe']
    print('查看此行浏览器验证通过：逐步锚定出发页边标识、提交键与到达证据页；'
          '反例使端点比较失效；错误键回传未影响认证字节')


def main():
    with TemporaryDirectory(prefix='keelsplice-navigation-') as directory:
        root = Path(directory)
        source = root / 'loop.sqlite'
        seed(source)
        with create_server(('127.0.0.1', 0), root / 'state.sqlite', {'loop': source}) as server:
            thread = Thread(target=server.serve_forever)
            thread.start()
            try:
                run(f'http://127.0.0.1:{server.server_port}', source, root)
            finally:
                server.shutdown()
                thread.join()


if __name__ == '__main__':
    main()

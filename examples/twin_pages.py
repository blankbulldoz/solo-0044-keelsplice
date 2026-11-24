"""复现「两张一模一样的错误页」并录下能区分来源的浏览器提交与持久证据。

直接运行时在临时目录完整复现并自检；加 --record DIR 把浏览器提交、
错误页与持久证据写入 DIR。仓库内 examples/twin_pages/ 即由此生成，
诊断说明见 examples/twin_pages/DIAGNOSIS.md。
"""

import argparse
import json
import re
import sqlite3
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

from keelsplice.api import create_server

COMMITTED = Path(__file__).with_name('twin_pages')


def seed(file):
    with closing(sqlite3.connect(file)) as conn:
        conn.executescript(Path(__file__).with_name('twin_pages.sql').read_text())


def api(base, route, body=None, status=200):
    data = None if body is None else json.dumps(body).encode('utf-8')
    try:
        response = urlopen(Request(base + route, data, {'Content-Type': 'application/json'}), timeout=5)
    except HTTPError as exc:
        response = exc
    with response:
        payload = response.read()
        assert response.status == status, (route, response.status, payload[:300])
        return response.status, json.loads(payload)


class Browser:
    """Submit workbench forms exactly like the browser and keep the raw requests."""

    def __init__(self, base):
        self.base = base
        self.host = urlsplit(base).netloc
        self.submissions = []

    def get(self, path):
        with urlopen(self.base + path, timeout=5) as response:
            return response.read().decode('utf-8')

    def submit(self, name, note, path, fields):
        body = urlencode(fields, doseq=True)
        request = Request(self.base + path, body.encode('ascii'), {'Origin': self.base})
        try:
            response = urlopen(request, timeout=5)
        except HTTPError as exc:
            response = exc
        with response:
            page = response.read()
            record = (f'# {note}\nPOST {path} HTTP/1.1\nHost: {self.host}\nOrigin: {self.base}\n'
                      f'Content-Type: application/x-www-form-urlencoded\n\n{body}\n\n'
                      f'# 响应 {response.status}，最终地址 {urlsplit(response.url).path}\n')
            self.submissions.append((name, record))
            return response.status, urlsplit(response.url).path, page


def scenario(base, source, repo):
    browser = Browser(base)
    evidence, pages = {}, {}

    _, catalog = api(base, '/sources/twin/catalog')
    edge = next(edge for edge in catalog['schema']['edges'] if edge['child'] == 'vial')
    assert edge['parent'] == 'lot' and edge['parent_cols'] == ['region', 'code']
    editor = browser.get('/workbench/recipes/new?source=twin')
    digest = re.search(r'name="digest" value="([0-9a-f]{64})"', editor)[1]

    # 配方 v1：两个根是同一 NOCASE 实存记录的两种输入拼写。
    _, path, _ = browser.submit(
        '01-register-v1', '登记配方 v1：两个根 ["east","MICA"] 与 ["EAST","mica"] 是同一实存行的两种输入拼写',
        '/workbench/recipes/new',
        {'source': 'twin', 'name': 'twin-page', 'digest': digest, 'row_limit': '20',
         'edge': [f'{edge["id"]}:vial'],
         'roots': '[{"table":"lot","key":["east","MICA"]},{"table":"lot","key":["EAST","mica"]}]'})
    assert path == '/workbench/recipes/twin-page/1', path
    _, path, _ = browser.submit(
        '02-seal-capture-a', "封存捕获 A：收入 lot('East','Mica') 与 TEXT 行 vial('R1','7')",
        '/workbench/recipes/twin-page/1/capture', {})
    capture_a = path.rsplit('/', 1)[1]

    # 配方 v2：根是 INTEGER 行 vial('R1',7)，封存出的捕获 B 不含 TEXT 行。
    _, path, _ = browser.submit(
        '03-register-v2', '登记配方 v2：根是显示同为 R1 / 7 的 INTEGER 行 ["R1",7]',
        '/workbench/recipes/new',
        {'source': 'twin', 'name': 'twin-page', 'digest': digest, 'row_limit': '20',
         'roots': '[{"table":"vial","key":["R1",7]}]'})
    assert path == '/workbench/recipes/twin-page/2', path
    _, path, _ = browser.submit(
        '04-seal-capture-b', '封存捕获 B：只有 INTEGER 行，不含 TEXT 行，也不含 lot',
        '/workbench/recipes/twin-page/2/capture', {})
    capture_b = path.rsplit('/', 1)[1]

    def trace(name, note, capture, table, key, expect):
        status, _, page = browser.submit(name, note, f'/workbench/captures/{capture}/trace',
                                         {'table': table, 'key': key})
        assert status == expect, (name, status)
        return page

    stored = trace('05-trace-a-lot-stored', '用实存键 ["East","Mica"] 追踪捕获 A → 200，根序号 1 与 2',
                   capture_a, 'lot', '["East","Mica"]', 200)
    assert '1, 2' in stored.decode('utf-8')
    pages['error-1-nocase-spelling'] = trace(
        '06-trace-a-lot-input-spelling', '用户照登记拼写 ["east","MICA"] 追踪捕获 A → 404（错误页①）',
        capture_a, 'lot', '["east","MICA"]', 404)
    trace('07-trace-a-vial-text-slot', '用文本键 ["R1","7"] 追踪捕获 A → 200',
          capture_a, 'vial', '["R1","7"]', 200)
    pages['error-2-type-flip'] = trace(
        '08-trace-a-vial-integer-slot', '用户按显示数字填整数 ["R1",7] 追踪捕获 A → 404（错误页②）',
        capture_a, 'vial', '["R1",7]', 404)
    pages['error-3-row-absent'] = trace(
        '09-trace-b-vial-text-slot', '用文本键 ["R1","7"] 追踪捕获 B（不含该行）→ 404（错误页③）',
        capture_b, 'vial', '["R1","7"]', 404)
    trace('10-trace-b-vial-integer-slot', '用整数键 ["R1",7] 追踪捕获 B → 200',
          capture_b, 'vial', '["R1",7]', 200)

    assert len({pages[name] for name in pages}) == 1, '三个来源的错误页应当字节一致'

    def keep(name, route, body=None, status=200):
        code, payload = api(base, route, body, status)
        request = {'method': 'POST' if body is not None else 'GET', 'path': route}
        if body is not None:
            request['body'] = body
        evidence[name] = {'request': request, 'status': code, 'response': payload}
        return payload

    status_a = keep('capture-a', f'/captures/{capture_a}')
    status_b = keep('capture-b', f'/captures/{capture_b}')
    assert status_a['roots'] == [['lot', ['East', 'Mica']]] * 2
    assert status_a['summary'] == {'counts': {'lot': 1, 'vial': 1}, 'rows': 2, 'witnesses': 2, 'root_occurrences': 2}
    assert status_b['roots'] == [['vial', ['R1', 7]]]
    assert status_b['summary']['counts'] == {'lot': 0, 'vial': 1}

    traced = keep('trace-a-lot-stored', f'/captures/{capture_a}/trace', {'table': 'lot', 'key': ['East', 'Mica']})
    assert traced['roots'] == [0, 1]
    keep('trace-a-lot-input-spelling', f'/captures/{capture_a}/trace', {'table': 'lot', 'key': ['east', 'MICA']}, 404)
    keep('trace-a-vial-text-slot', f'/captures/{capture_a}/trace', {'table': 'vial', 'key': ['R1', '7']})
    keep('trace-a-vial-integer-slot', f'/captures/{capture_a}/trace', {'table': 'vial', 'key': ['R1', 7]}, 404)
    keep('trace-b-vial-text-slot', f'/captures/{capture_b}/trace', {'table': 'vial', 'key': ['R1', '7']}, 404)
    keep('trace-b-vial-integer-slot', f'/captures/{capture_b}/trace', {'table': 'vial', 'key': ['R1', 7]})

    with closing(sqlite3.connect(source.as_uri() + '?mode=ro', uri=True)) as conn:
        evidence['source-storage-classes'] = {
            'observation': '对源库执行 quote()/typeof()，把显示出的 7 与存储类分开',
            'queries': ['SELECT rack, quote(slot), typeof(slot), fill FROM vial ORDER BY typeof(slot)',
                        'SELECT quote(region), quote(code) FROM lot'],
            'vial': [{'rack': rack, 'slot_literal': literal, 'slot_type': kind, 'fill': fill}
                     for rack, literal, kind, fill in conn.execute(
                         'SELECT rack, quote(slot), typeof(slot), fill FROM vial ORDER BY typeof(slot)')],
            'lot': [{'region_literal': region, 'code_literal': code}
                    for region, code in conn.execute('SELECT quote(region), quote(code) FROM lot')]}
    rows = evidence['source-storage-classes']['vial']
    assert [(row['slot_literal'], row['slot_type']) for row in rows] == [('7', 'integer'), ("'7'", 'text')]

    with closing(sqlite3.connect(repo.as_uri() + '?mode=ro', uri=True)) as conn:
        for name, capture_id in (('sealed-a', capture_a), ('sealed-b', capture_b)):
            row = conn.execute('SELECT sealed FROM captures WHERE id=?', (capture_id,)).fetchone()
            evidence[name] = {'observation': '状态库 captures 表中持久保存的封存记录（sealed 列原样内容）',
                              'capture': capture_id, 'sealed': json.loads(row[0])}
    assert list(evidence['sealed-a']['sealed']['result']['rows']['vial']) == ['["R1","7"]']
    assert list(evidence['sealed-b']['sealed']['result']['rows']['vial']) == ['["R1",7]']

    return browser.submissions, pages, evidence


def write_artifacts(directory, submissions, pages, evidence):
    for group in ('submissions', 'pages', 'evidence'):
        (directory / group).mkdir(parents=True, exist_ok=True)
    for name, record in submissions:
        (directory / 'submissions' / f'{name}.http').write_text(record, encoding='utf-8')
    for name, page in pages.items():
        (directory / 'pages' / f'{name}.html').write_bytes(page)
    for name, payload in evidence.items():
        (directory / 'evidence' / f'{name}.json').write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + '\n', encoding='utf-8')


def compare_with_committed(pages):
    committed = COMMITTED / 'pages'
    if not committed.is_dir():
        return
    for name, page in pages.items():
        file = committed / f'{name}.html'
        assert file.is_file() and page == file.read_bytes(), f'{name}: 与仓库内已录证据不一致'


def main():
    parser = argparse.ArgumentParser(description='复现三张字节一致的 404 错误页并录下可区分来源的证据。')
    parser.add_argument('--record', metavar='DIR', help='把浏览器提交、错误页与持久证据写入 DIR')
    args = parser.parse_args()
    with TemporaryDirectory(prefix='keelsplice-twin-') as directory:
        root = Path(directory)
        source = root / 'source.sqlite'
        seed(source)
        with create_server(('127.0.0.1', 0), root / 'state.sqlite', {'twin': source}) as server:
            thread = Thread(target=server.serve_forever, kwargs={'poll_interval': 0.05})
            thread.start()
            try:
                artifacts = scenario(f'http://127.0.0.1:{server.server_port}', source, root / 'state.sqlite')
            finally:
                server.shutdown()
                thread.join()
        if args.record:
            write_artifacts(Path(args.record), *artifacts)
            print(f'已把浏览器提交、错误页与持久证据写入 {args.record}')
        else:
            compare_with_committed(artifacts[1])
    print('双胞胎错误页复现通过：三个来源 → 字节一致的 404；浏览器提交与持久证据可区分来源')


if __name__ == '__main__':
    main()

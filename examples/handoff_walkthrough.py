"""Drive one capture lifecycle through the JSON, HTML, and local entries.

``keelsplice.handoff.Handoff`` owns the build → certify → download decisions
against the repository file. This walkthrough runs three entries against one
repository so readers can see which facts live in the database and which
shapes are transport expression:

1. certify or download before build is rejected by every entry alike;
2. the JSON API, the HTML workbench, and an in-process caller interleave
   build and certify on the same captures;
3. retries after persistence — including a server restart on the same
   repository file — return the recorded certificate and identical bytes.
"""

import json
import re
import sqlite3
from contextlib import closing, contextmanager
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

from keelsplice.api import create_server
from keelsplice.codec import Fault
from keelsplice.handoff import Handoff
from keelsplice.storage import Repository

from .walkthrough import seed

CERTIFICATE = re.compile(r'文件 SHA-256</dt><dd><code>([0-9a-f]{64})</code>')


def api(base, route, body=None, status=200):
    data = None if body is None else json.dumps(body).encode('utf-8')
    request = Request(base + route, data, {'Content-Type': 'application/json'})
    try:
        response = urlopen(request, timeout=5)
    except HTTPError as exc:
        response = exc
    with response:
        payload = response.read()
        assert response.status == status, (route, response.status, payload[:300])
        if response.headers.get_content_type() == 'application/vnd.sqlite3':
            return payload, response.headers['ETag'].strip('"')
        return json.loads(payload)


def open_page(base, path, payload=None, expected=200):
    data = None if payload is None else urlencode(payload).encode('ascii')
    headers = {} if data is None else {'Origin': base}
    request = Request(base + path, data, headers)
    try:
        response = urlopen(request, timeout=5)
    except HTTPError as exc:
        response = exc
    with response:
        assert response.status == expected, (path, response.status, response.read()[:300])
        return urlsplit(response.url).path, response.headers, response.read()


def html(base, path, payload=None, expected=200):
    path, headers, body = open_page(base, path, payload, expected)
    assert headers.get_content_type() == 'text/html', path
    return path, body.decode('utf-8')


def facts(repo, capture_id):
    uri = repo.resolve().as_uri() + '?mode=ro'
    with closing(sqlite3.connect(uri, uri=True)) as conn:
        state, size, certificate = conn.execute(
            'SELECT state, length(artifact), certificate FROM captures WHERE id=?',
            (capture_id,)).fetchone()
    return state, size or 0, certificate


def show_facts(repo, capture_id):
    state, size, certificate = facts(repo, capture_id)
    saved = f'已写入（{len(certificate)} 字符）' if certificate else '未写入'
    print(f'  [数据库] captures 行：state={state}，artifact={size or "无"} 字节，certificate={saved}')


@contextmanager
def serve(repo, sources):
    with create_server(('127.0.0.1', 0), repo, sources) as server:
        thread = Thread(target=server.serve_forever, kwargs={'poll_interval': 0.05})
        thread.start()
        try:
            yield f'http://127.0.0.1:{server.server_port}'
        finally:
            server.shutdown()
            thread.join()


def prepare(base):
    catalog = api(base, '/sources/microscope/catalog')
    edge = next(edge for edge in catalog['schema']['edges']
                if edge['child'] == 'assay' and edge['parent'] == 'substrate')
    body = {'source': 'microscope', 'roots': [{'table': 'substrate', 'key': ['east', 'MICA']}],
            'follow_children': [{'child': 'assay', 'id': edge['id']}], 'row_limit': 20}
    version = api(base, '/recipes/swatch/versions', body, 201)
    capture = api(base, '/captures', {'name': 'swatch', 'version': version['version']}, 201)
    assert capture['state'] == 'sealed'
    print(f'准备：JSON 登记配方 swatch/{version["version"]} 并封存捕获 A（id={capture["id"][:12]}…）')
    return capture['id']


def certify_too_early(base, local, repo, capture_id):
    print('\n第一阶段：未构建时请求认证与下载 —— 三个入口撞上同一道门槛')
    show_facts(repo, capture_id)
    outcome = api(base, f'/captures/{capture_id}/certify', {}, 409)
    assert outcome['error'] == 'fixture_not_built'
    print(f'  [JSON] POST /captures/<A>/certify → 409 {outcome["error"]}')
    _, page = html(base, f'/workbench/captures/{capture_id}/certify', {}, 409)
    assert '需先构建子库' in page
    print('  [HTML] POST /workbench/captures/<A>/certify → 409 「需先构建子库。」')
    try:
        local.certify(capture_id)
        raise AssertionError('certify must be rejected before build')
    except Fault as exc:
        assert exc.code == 'fixture_not_built' and exc.status == 409
        print(f'  [本机] Handoff.certify(<A>) → Fault {exc.code}（HTTP {exc.status}）')
    outcome = api(base, f'/captures/{capture_id}/database', status=409)
    assert outcome['error'] == 'fixture_not_certified'
    print(f'  [JSON] GET /captures/<A>/database → 409 {outcome["error"]}')
    _, page = html(base, f'/workbench/captures/{capture_id}/database', expected=409)
    assert '需先认证子库' in page
    print('  [HTML] GET /workbench/captures/<A>/database → 409 「需先认证子库。」')
    try:
        local.download(capture_id)
        raise AssertionError('download must be rejected before certify')
    except Fault as exc:
        assert exc.code == 'fixture_not_certified' and exc.status == 409
        print(f'  [本机] Handoff.download(<A>) → Fault {exc.code}（HTTP {exc.status}）')
    assert facts(repo, capture_id) == ('sealed', 0, None)
    print('  [数据库] 六次拒绝均未改动该行：仍是 state=sealed、无字节、无证书')


def interleave_json_then_browser(base, local, repo, root, capture_id):
    print('\n第二阶段甲：JSON 构建、HTML 认证，交错推进同一捕获')
    built = api(base, f'/captures/{capture_id}/build', {})
    assert built['state'] == 'built' and built['certificate'] is None
    print('  [JSON] POST /captures/<A>/build → 200 state=built')
    show_facts(repo, capture_id)
    _, page = html(base, f'/workbench/captures/{capture_id}')
    assert '已构建' in page and '认证子库' in page
    print('  [HTML] GET 详情页 → 「已构建」，下一步按钮「认证子库」（同一行的另一种表达）')
    again = api(base, f'/captures/{capture_id}/build', {})
    assert again == built
    print('  [JSON] 立即重试 build → 响应逐字段相同（幂等，不重建）')
    path, page = html(base, f'/workbench/captures/{capture_id}/certify', {})
    assert path == f'/workbench/captures/{capture_id}' and '已认证' in page
    sha_html = CERTIFICATE.search(page)[1]
    print(f'  [HTML] POST …/certify → 303 → 「已认证」，证书 SHA-256 {sha_html[:16]}…')
    status = api(base, f'/captures/{capture_id}')
    assert status['state'] == 'certified' and status['certificate']['sha256'] == sha_html
    print('  [JSON] GET /captures/<A> → state=certified，证书与 HTML 页面所示一致')
    show_facts(repo, capture_id)
    certificate = status['certificate']
    data_json, etag = api(base, f'/captures/{capture_id}/database')
    assert etag == certificate['sha256'] == sha256(data_json).hexdigest()
    print(f'  [JSON] 下载 {len(data_json)} 字节，ETag 等于证书 SHA-256')
    _, headers, data_html = open_page(base, f'/workbench/captures/{capture_id}/database')
    assert headers['ETag'] == f'"{certificate["sha256"]}"' and data_html == data_json
    print('  [HTML] 下载字节与 JSON 逐位相同')
    data_local, sha_local = local.download(capture_id)
    assert data_local == data_json and sha_local == certificate['sha256']
    print('  [本机] Handoff.download → 同一字节与摘要')
    fixture = root / 'handoff-a.sqlite'
    fixture.write_bytes(data_json)
    with closing(sqlite3.connect(fixture.as_uri() + '?mode=ro', uri=True)) as conn:
        assert conn.execute('PRAGMA foreign_key_check').fetchall() == []
        row = conn.execute("SELECT length_um, doubled FROM assay WHERE mark='S-01'").fetchone()
        assert row == (3.25, 6.5)
    print(f'  [独立] 下载字节写盘后用新 SQLite 连接复核：外键 0 违规，S-01 长度 {row[0]}、生成列 {row[1]}')
    return certificate


def interleave_browser_local_json(base, local):
    print('\n第二阶段乙：HTML 封存、本机构建、JSON 认证 —— 新入口复用同一决策')
    path, page = html(base, '/workbench/recipes/swatch/1/capture', {})
    capture_id = path.rsplit('/', 1)[1]
    assert '已封存' in page
    print(f'  [HTML] POST /workbench/recipes/swatch/1/capture → 303 → 捕获 B（{capture_id[:12]}…）')
    record = local.build(capture_id)
    assert record['state'] == 'built'
    print('  [本机] Handoff.build(<B>) → state=built（不经过 HTTP 的同一决策）')
    certified = api(base, f'/captures/{capture_id}/certify', {})
    assert certified['state'] == 'certified'
    sha = certified['certificate']['sha256']
    print(f'  [JSON] POST /captures/<B>/certify → 200 state=certified，SHA-256 {sha[:16]}…')
    _, headers, data = open_page(base, f'/workbench/captures/{capture_id}/database')
    assert headers['ETag'] == f'"{sha}"' and sha256(data).hexdigest() == sha
    print('  [HTML] 下载捕获 B → 字节摘要等于 JSON 证书')


def retry_after_persistence(base, local, repo, capture_id, certificate, before):
    print('\n第三阶段：同一状态库文件上重启服务，重试已持久化的操作')
    after = facts(repo, capture_id)
    assert after == before
    print('  [数据库] 重启前后捕获 A 的行逐字段相同（state、字节、证书文本）')
    status = api(base, f'/captures/{capture_id}')
    assert status['state'] == 'certified' and status['certificate'] == certificate
    print('  [JSON] GET /captures/<A> → 证书逐字段等于重启前')
    assert api(base, f'/captures/{capture_id}/build', {}) == status
    print('  [JSON] 重试 build → 返回已持久化状态，不重建')
    assert api(base, f'/captures/{capture_id}/certify', {}) == status
    print('  [JSON] 重试 certify → 返回同一证书，不重算')
    _, page = html(base, f'/workbench/captures/{capture_id}/certify', {})
    assert CERTIFICATE.search(page)[1] == certificate['sha256']
    print('  [HTML] 重试 certify → 303 → 页面仍是同一 SHA-256')
    record = local.certify(capture_id)
    assert record['state'] == 'certified' and record['certificate'] == certificate
    print('  [本机] Handoff.certify 重试 → 同一证书记录')
    data, etag = api(base, f'/captures/{capture_id}/database')
    assert etag == certificate['sha256'] and sha256(data).hexdigest() == etag
    print('  [JSON] 重启后下载 → 字节摘要仍等于证书')


def main():
    print('交接边界演练：构建、认证、下载的状态决策集中在 keelsplice.handoff.Handoff')
    print('  数据库事实：captures 行的 state / artifact / certificate（条件写入，可安全重试）')
    print('  传输表达：JSON 响应、HTML 页面、下载响应头（各入口自行渲染同一事实）')
    with TemporaryDirectory(prefix='keelsplice-handoff-') as directory:
        root = Path(directory)
        source = root / 'microscope.sqlite'
        seed(source)
        repo = root / 'state.sqlite'
        sources = {'microscope': source}
        local = Handoff(Repository(repo, sources))
        with serve(repo, sources) as base:
            capture_a = prepare(base)
            certify_too_early(base, local, repo, capture_a)
            certificate_a = interleave_json_then_browser(base, local, repo, root, capture_a)
            interleave_browser_local_json(base, local)
            before = facts(repo, capture_a)
        print('\n  …关闭服务，仅保留状态库文件，随后以同一文件重启…')
        with serve(repo, sources) as base:
            retry_after_persistence(base, local, repo, capture_a, certificate_a, before)
    print('\n交接边界演练完成：captures 行承载全部状态事实；JSON、HTML 与本机入口只做各自的传输表达。')


if __name__ == '__main__':
    main()

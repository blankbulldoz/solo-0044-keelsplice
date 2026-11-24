"""Share one repository between the original HTTP entry and the WSGI entry.

The two servers run at the same time against the same repository file and the
same source database.  The walkthrough checks that:

* old JSON clients get byte-identical catalog and error responses from both;
* a recipe and capture sealed through one entry are readable through the other;
* browser forms keep the typed-key fields and the same-origin boundary;
* a fixture certified on one side downloads from both with one SHA-256;
* after switching back, the original entry keeps operating a capture sealed
  through the WSGI entry.
"""

import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from keelsplice.api import create_server
from keelsplice.wsgi import serve as serve_wsgi

from .walkthrough import call, seed

JSON = {'Content-Type': 'application/json'}


def open_page(base, path, payload=None, expected=200, origin=None, headers=None):
    data = None if payload is None else urlencode(payload, doseq=True).encode('ascii')
    merged = dict(headers or {})
    if data is not None and 'Content-Type' not in merged:
        merged['Content-Type'] = 'application/x-www-form-urlencoded'
    if data is not None:
        merged['Origin'] = origin or base
    request = Request(base + path, data=data, headers=merged)
    try:
        response = urlopen(request, timeout=10)
    except HTTPError as exc:
        response = exc
    with response:
        return response.status, dict(response.headers), response.read()


def raw(base, route, body, headers, expected):
    data = json.dumps(body).encode('utf-8') if body is not None else None
    request = Request(base + route, data=data, headers=headers)
    try:
        response = urlopen(request, timeout=10)
    except HTTPError as exc:
        response = exc
    with response:
        assert response.status == expected, (route, response.status, response.read()[:300])
        return response.status, response.headers.get('Content-Type'), response.read()


def walkthrough(legacy, hosted, source, root, state):
    # 1. The same catalog and the same error envelope on both entries.
    status, kind, old_catalog = raw(legacy, '/sources/microscope/catalog', None, {}, 200)
    assert (status, kind) == (200, 'application/json')
    status, kind, new_catalog = raw(hosted, '/sources/microscope/catalog', None, {}, 200)
    assert (status, kind, new_catalog) == (200, 'application/json', old_catalog)
    assert json.loads(old_catalog)['schema_digest'] == json.loads(new_catalog)['schema_digest']

    for base in (legacy, hosted):
        status, kind, body = raw(base, '/sources/absent/catalog', None, {}, 404)
        assert (status, kind) == (404, 'application/json')
        assert json.loads(body) == {'error': 'unknown_source', 'message': 'Source alias is not registered'}
        status, kind, body = raw(base, '/captures', {'name': 'ghost', 'version': 1}, JSON, 404)
        assert json.loads(body)['error'] == 'unknown_recipe'
    status, kind, a = raw(legacy, '/nope', None, {}, 404)
    status, kind, b = raw(hosted, '/nope', None, {}, 404)
    assert a == b and json.loads(a)['error'] == 'unknown_route'
    status, kind, a = raw(legacy, '/preview', [], JSON, 400)
    status, kind, b = raw(hosted, '/preview', [], JSON, 400)
    assert a == b and json.loads(a)['error'] == 'invalid_request'
    status, kind, a = raw(legacy, '/preview', {'name': 'x', 'version': 1}, {'Content-Type': 'text/plain'}, 415)
    status, kind, b = raw(hosted, '/preview', {'name': 'x', 'version': 1}, {'Content-Type': 'text/plain'}, 415)
    assert a == b and json.loads(a)['error'] == 'invalid_content_type'
    for base in (legacy, hosted):
        request = Request(base + '/sources/microscope/catalog', method='PUT')
        try:
            response = urlopen(request, timeout=10)
        except HTTPError as exc:
            response = exc
        with response:
            assert response.status == 501 and response.headers['Allow'] == 'GET, POST'
            assert json.loads(response.read())['error'] == 'invalid_method'
    print('两条入口的目录与错误结构一致')

    # 2. Register through the original entry, seal through the WSGI entry.
    body = {'source': 'microscope', 'roots': [{'table': 'substrate', 'key': ['east', 'MICA']}],
            'follow_children': [{'child': 'assay', 'id': 0}], 'row_limit': 20}
    version = call(legacy, '/recipes/shared/versions', body, 201)
    assert version['version'] == 1
    first = call(hosted, '/captures', {'name': 'shared', 'version': 1}, 201)
    assert first['state'] == 'sealed' and first['roots'] == [['substrate', ['East', 'Mica']]]
    assert call(legacy, '/captures/' + first['id'])['id'] == first['id']
    print('原入口登记的配方由 WSGI 入口封存，反向读取一致')

    # 3. Browser forms through the WSGI entry keep typed keys and same origin.
    status, headers, page = open_page(hosted, '/workbench/recipes/new')
    assert status == 200 and '主键列顺序' in page.decode('utf-8')
    digest = page.decode('utf-8').split('name="digest" value="')[1].split('"')[0]
    form = {'source': 'microscope', 'name': 'browser-shared', 'digest': digest, 'row_limit': '20',
            'edge': ['0:assay'], 'roots': '[{"table":"substrate","key":["West","BASALT"]}]'}
    status, _, body = open_page(hosted, '/workbench/recipes/new', form, expected=403, origin='http://localhost:1')
    assert '本机工作台' in body.decode('utf-8')
    status, headers, body = open_page(legacy, '/workbench/recipes/new', form)
    assert status == 200 and 'browser-shared' in body.decode('utf-8')
    status, headers, page = open_page(hosted, '/workbench/recipes/browser-shared/1')
    assert status == 200 and '投用中' in page.decode('utf-8')
    status, headers, page = open_page(hosted, '/workbench/captures/' + first['id'])
    detail = page.decode('utf-8')
    assert status == 200 and 'name="key" value="[&quot;East&quot;,&quot;Mica&quot;]"' in detail
    print('浏览器表单经 WSGI 入口提交，主键 JSON 与同源检查保持原样')

    # 4. Sealed on the WSGI entry, certified on the original one; both download.
    built = call(legacy, '/captures/' + first['id'] + '/build', {})
    assert built['state'] == 'built'
    certified = call(hosted, '/captures/' + first['id'] + '/certify', {})
    assert certified['state'] == 'certified'
    old_data, old_etag = call(legacy, '/captures/' + first['id'] + '/database')
    new_data, new_etag = call(hosted, '/captures/' + first['id'] + '/database')
    assert old_data == new_data
    assert old_etag == new_etag == certified['certificate']['sha256']
    assert hashlib.sha256(old_data).hexdigest() == old_etag
    handoff = root / 'shared-handoff.sqlite'
    handoff.write_bytes(new_data)
    with closing(sqlite3.connect(handoff.as_uri() + '?mode=ro', uri=True)) as conn:
        assert conn.execute('PRAGMA foreign_key_check').fetchall() == []
        assert conn.execute("SELECT length_um FROM assay WHERE mark='S-01'").fetchone() == (3.25,)
    print('两侧下载同一证书摘要，独立连接复核通过')

    # 5. A capture sealed through the WSGI entry stays usable after switching back.
    second = call(hosted, '/captures', {'name': 'shared', 'version': 1}, 201)
    assert second['id'] != first['id']
    traced = call(legacy, '/captures/' + second['id'] + '/trace',
                  {'table': 'substrate', 'key': ['East', 'Mica']})
    assert traced['roots'] == [0] and {arc['origin'][0] for arc in traced['causes']} == {'assay'}
    call(legacy, '/captures/' + second['id'] + '/build', {})
    settled = call(legacy, '/captures/' + second['id'] + '/certify', {})
    assert settled['state'] == 'certified'
    data, etag = call(legacy, '/captures/' + second['id'] + '/database')
    assert hashlib.sha256(data).hexdigest() == etag == settled['certificate']['sha256']
    diff = call(hosted, '/compare', {'left': first['id'], 'right': second['id']})
    assert all(not changes['added'] and not changes['removed'] for changes in diff['changes'].values())
    print('切回原入口后，WSGI 封存的新记录仍可追踪、构建、认证与下载')

    # 6. Both processes write the same repository file.
    with closing(sqlite3.connect(state)) as conn:
        rows = conn.execute('SELECT state FROM captures ORDER BY rowid').fetchall()
    assert [row[0] for row in rows] == ['certified', 'certified']
    print('同一持久仓库被两个入口共享，未产生重复或分叉记录')


def main():
    with TemporaryDirectory(prefix='keelsplice-deploy-') as directory:
        root = Path(directory)
        source = root / 'microscope.sqlite'
        state = root / 'state.sqlite'
        seed(source)
        sources = {'microscope': source}
        with create_server(('127.0.0.1', 0), state, sources) as legacy:
            hosted = serve_wsgi(('127.0.0.1', 0), state, sources)
            legacy_thread = Thread(target=legacy.serve_forever, kwargs={'poll_interval': 0.05})
            hosted_thread = Thread(target=hosted.serve_forever, kwargs={'poll_interval': 0.05})
            legacy_thread.start()
            hosted_thread.start()
            try:
                walkthrough(f'http://127.0.0.1:{legacy.server_port}',
                            f'http://127.0.0.1:{hosted.server_port}', source, root, state)
            finally:
                for server, thread in ((legacy, legacy_thread), (hosted, hosted_thread)):
                    server.shutdown()
                hosted.server_close()
                for thread in (legacy_thread, hosted_thread):
                    thread.join()
    print('部署切换演练通过：并行入口、持久仓库、类型化表单、证书摘要与新记录续办')


if __name__ == '__main__':
    main()

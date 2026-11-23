"""Exercise the production workbench with the disposable microscope database."""

import argparse
import hashlib
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

from .walkthrough import seed


def open_page(base, path, payload=None, expected=200, origin=None):
    data = None if payload is None else urlencode(payload, doseq=True).encode('ascii')
    headers = {} if data is None else {'Origin': origin or base}
    request = Request(base + path, data=data, headers=headers)
    try:
        response = urlopen(request, timeout=5)
    except HTTPError as exc:
        response = exc
    with response:
        assert response.status == expected, (path, response.status, response.read())
        return urlsplit(response.url).path, response.headers, response.read()


def html(base, path, payload=None, expected=200, origin=None):
    path, headers, body = open_page(base, path, payload, expected, origin)
    assert headers.get_content_type() == 'text/html', path
    return path, body.decode('utf-8')


def forms_with(body, label):
    from html import unescape
    from urllib.parse import unquote
    found = []
    for fragment in re.findall(r'<form .*?</form>', body, re.S):
        if label in fragment:
            found.append({name: unescape(unquote(value)) for name, value in
                          re.findall(r'name="([^"]+)" value="([^"]*)"', fragment)})
    return found


def run(base, source, root):
    path, directory = html(base, '/')
    assert path == '/workbench' and '外键关系' in directory
    _, table = html(base, '/workbench?source=microscope&table=substrate')
    assert 'region → code' in table and 'cassette' in table
    assert open_page(base, '/workbench/assets/workspace.css')[1].get_content_type() == 'text/css'
    assert '未注册' in html(base, '/workbench?source=missing', expected=404)[1]

    editor_url = '/workbench/recipes/new'
    _, editor = html(base, editor_url)
    digest = re.search(r'name="digest" value="([0-9a-f]{64})"', editor)[1]
    form = {'source': 'microscope', 'name': 'browser-swatch', 'digest': digest,
            'row_limit': '20', 'edge': ['0:assay'],
            'roots': '[{"table":"substrate","key":["east","MICA"]}]'}
    assert '目录已变化' in html(base, editor_url, form | {'digest': 'stale'}, expected=409)[1]
    assert '本机工作台' in html(base, editor_url, form, expected=403, origin='http://localhost:1')[1]
    _, error = html(base, editor_url, form | {'roots': 'bad'}, expected=400)
    assert '有效的 JSON' in error and 'browser-swatch' in error
    version, page = html(base, editor_url, form)
    assert version.endswith('/1') and '投用中' in page
    assert 'browser-swatch' in html(base, '/workbench/recipes')[1]

    _, preview = html(base, version + '/preview', {})
    assert '未封存' in preview and 'East' in preview and 'Mica' in preview
    first, sealed = html(base, version + '/capture', {})
    assert '已封存' in sealed and '下载认证子库' not in sealed
    assert first in html(base, '/workbench/captures')[1]
    _, evidence = html(base, first + '/trace', {'table': 'substrate', 'key': '["East","Mica"]'})
    assert '直接入选原因' in evidence and 'S-01' in evidence
    assert '实存主键' in html(base, first + '/trace',
                            {'table': 'substrate', 'key': '["east","MICA"]'}, expected=404)[1]

    # 路线在标签页内自包含：保留有向边、路径位置与逐分支回环判定，不同标签页互不覆盖。
    _, start = html(base, first + '/trace', {'table': 'cassette', 'key': '[11]'})
    assert '当前节点是本分支首次到达' in start and '1 / 1' in start
    walk = next(item for item in forms_with(start, '沿此边继续')
                if item['edge_table'] == 'cassette' and item['direction'] == 'parent')
    assert walk['route'] == '[]'
    _, middle = html(base, first + '/trace', walk)
    assert '2 / 2' in middle and '本路线所选' in middle and '当前节点是本分支首次到达' in middle
    next_walk = next(item for item in forms_with(middle, '沿此边继续')
                     if item['edge_table'] == 'cassette' and item['direction'] == 'parent')
    _, closed = html(base, first + '/trace', next_walk)
    assert '3 / 3' in closed and '回到已见节点' in closed and '第 1 个位置首次到达' in closed
    back = next(item for item in forms_with(closed, '') if item.get('back') == '0')
    _, restored = html(base, first + '/trace', back)
    assert '1 / 1' in restored and '当前节点是本分支首次到达' in restored
    assert forms_with(restored, '沿此边继续')[0]['route'] == '[]'
    _, other_tab = html(base, first + '/trace', {'table': 'cassette', 'key': '[12]'})
    assert '1 / 1' in other_tab and '当前节点是本分支首次到达' in other_tab
    tampered = dict(walk, route='[{"edge":["cassette",9],"direction":"parent","table":"cassette","key":[99]}]')
    assert '路线数据无法识别' in html(base, first + '/trace', tampered, expected=400)[1]

    assert '需先认证' in html(base, first + '/database', expected=409)[1]

    with closing(sqlite3.connect(source)) as conn, conn:
        conn.execute("UPDATE assay SET length_um=7 WHERE mark='S-01'")
    second, _ = html(base, version + '/capture', {})
    args = {'left': first.rsplit('/', 1)[1], 'right': second.rsplit('/', 1)[1]}
    _, compare = html(base, '/workbench/compare', args)
    assert 'S-01' in compare and '修改' in compare
    assert '已停用' in html(base, version + '/retire', {})[1]
    assert '已停止投用' in html(base, version + '/capture', {}, expected=409)[1]
    assert '已构建' in html(base, first + '/build', {})[1]
    assert '已认证' in html(base, first + '/certify', {})[1]
    _, headers, database = open_page(base, first + '/database')
    assert headers['ETag'] == '"' + hashlib.sha256(database).hexdigest() + '"'
    handoff = root / 'handoff.sqlite'
    handoff.write_bytes(database)
    with closing(sqlite3.connect(handoff)) as conn:
        assert conn.execute("SELECT length_um FROM assay WHERE mark='S-01'").fetchone() == (3.25,)
        assert conn.execute('PRAGMA foreign_key_check').fetchall() == []
    print('浏览器端到端检查通过：目录、配方、试算、捕获、证据、对比、停用、构建、认证、下载')


def main():
    parser = argparse.ArgumentParser(description='在临时数据库演练关系样本工作台。')
    parser.parse_args()
    with TemporaryDirectory(prefix='keelsplice-browser-') as directory:
        root = Path(directory)
        source = root / 'source.sqlite'
        seed(source)
        with create_server(('127.0.0.1', 0), root / 'state.sqlite', {'microscope': source}) as server:
            thread = Thread(target=server.serve_forever)
            thread.start()
            try:
                run(f'http://127.0.0.1:{server.server_port}', source, root)
            finally:
                server.shutdown()
                thread.join()


if __name__ == '__main__':
    main()

"""Exercise the production workbench with the disposable microscope database."""

import argparse
import hashlib
import json
import re
import sqlite3
from contextlib import closing
from html import unescape
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

from keelsplice.api import create_server

from .walkthrough import seed

html_unquote = unescape


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


def run(base, source, root):
    path, directory = html(base, '/')
    assert path == '/workbench' and '外键关系' in directory
    _, table = html(base, '/workbench?source=microscope&table=substrate')
    assert 'region → code' in table and 'cassette' in table
    assert open_page(base, '/workbench/assets/workspace.css')[1].get_content_type() == 'text/css'
    assert '未注册' in html(base, '/workbench?source=missing', expected=404)[1]

    _, keys = html(base, '/workbench/keys?source=microscope&table=substrate')
    assert 'region (NOCASE ASC), code (NOCASE ASC)' in keys and '&quot;Mica&quot;' in keys
    assert '已核对到表尾' in keys
    session = html_unquote(re.search(r'name="session" value="([^"]+)"', keys)[1])
    steady_url = '/workbench/keys?' + urlencode({'source': 'microscope', 'table': 'substrate', 'session': session})
    _, steady = html(base, steady_url)
    assert '与上一页同一来源状态' in steady
    with closing(sqlite3.connect(source)) as conn, conn:
        conn.execute("INSERT INTO substrate VALUES('North','Quartz',x'00')")
    _, drifted = html(base, steady_url)
    assert '来源在翻页期间发生了变化' in drifted
    with closing(sqlite3.connect(source)) as conn, conn:
        conn.execute("DELETE FROM substrate WHERE region='North'")
    chosen_id, evidence = html(base, '/workbench/keys/select',
                               {'source': 'microscope', 'table': 'substrate',
                                'key': '["East","Mica"]', 'session': session})
    assert '选取证据' in evidence and 'East' in evidence and 'Mica' in evidence
    assert '带入配方登记' in evidence
    prefilled = html_unquote(re.search(r'name="roots" value="([^"]+)"', evidence)[1])
    assert json.loads(prefilled) == [{'table': 'substrate', 'key': ['East', 'Mica']}]
    _, listing = html(base, '/workbench/selections')
    assert chosen_id.rsplit('/', 1)[1][:12] in listing

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
    with closing(sqlite3.connect(source)) as conn, conn:
        conn.executemany("INSERT INTO frame(frame_id, mark, note) VALUES (?, NULL, 'extra')",
                        ((200 + index,) for index in range(8)))
    _, wide = html(base, '/workbench/keys?source=microscope&table=frame&limit=5')
    assert '下一页' in wide and '尚未覆盖全表' in wide
    frame_session = html_unquote(re.search(r'name="session" value="([^"]+)"', wide)[1])
    frame_after = html_unquote(re.search(r'name="after" value="([^"]+)"', wide)[1])
    next_url = '/workbench/keys?' + urlencode({'source': 'microscope', 'table': 'frame', 'limit': 5,
                                               'session': frame_session, 'after': frame_after})
    _, second = html(base, next_url)
    assert '与上一页同一来源状态' in second and '<code>204</code>' in second
    with closing(sqlite3.connect(source)) as conn, conn:
        conn.execute('DELETE FROM frame WHERE frame_id >= 200')

    _, headers, database = open_page(base, first + '/database')
    assert headers['ETag'] == '"' + hashlib.sha256(database).hexdigest() + '"'
    handoff = root / 'handoff.sqlite'
    handoff.write_bytes(database)
    with closing(sqlite3.connect(handoff)) as conn:
        assert conn.execute("SELECT length_um FROM assay WHERE mark='S-01'").fetchone() == (3.25,)
        assert conn.execute('PRAGMA foreign_key_check').fetchall() == []
    print('浏览器端到端检查通过：目录、实存主键浏览与选取、配方、试算、捕获、证据、对比、停用、构建、认证、下载')


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

"""Exercise the production workbench with the disposable microscope database."""

import argparse
import hashlib
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


@contextmanager
def serve(repo, source):
    with create_server(('127.0.0.1', 0), repo, {'microscope': source}) as server:
        thread = Thread(target=server.serve_forever)
        thread.start()
        try:
            yield f'http://127.0.0.1:{server.server_port}'
        finally:
            server.shutdown()
            thread.join()


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

    for _ in range(24):
        html(base, editor_url, form)
    listing = html(base, '/workbench/recipes')[1]
    assert '当前状态' in listing and '历史切面' in listing
    first_page, page_one = html(base, '/workbench/recipes/browser-swatch/history', {})
    session_id = first_page.rsplit('/', 1)[1]
    assert '当时投用结论' in page_one and '当前资格' in page_one and '交接地址' in page_one
    assert '版本 25</a>' in page_one and '版本 6</a>' in page_one and '版本 5</a>' not in page_one
    next_url = re.search(r'href="(/workbench/recipes/browser-swatch/history/[0-9a-f]+\?before=\d+)"',
                         page_one)[1]
    assert next_url.endswith('?before=6')
    html(base, editor_url, form)
    html(base, '/workbench/recipes/browser-swatch/25/retire', {})
    html(base, '/workbench/recipes/browser-swatch/3/retire', {})
    _, reopened = html(base, first_page)
    assert '版本 26</a>' not in reopened and '版本 25</a>' in reopened and '版本 6</a>' in reopened
    assert '切面截至版本</dt><dd>25' in reopened and '当前最新版本</dt><dd>26' in reopened
    assert '又有 1 个新版本' in reopened and reopened.count('资格已变化') == 1
    row = re.search(r'<tr><th scope="row"><a href="[^"]+/25">.*?</tr>', reopened, re.S)[0]
    assert '投用中' in row and '已停用' in row
    _, page_two = html(base, next_url)
    assert '版本 5</a>' in page_two and '版本 1</a>' in page_two
    assert '版本 6</a>' not in page_two and '版本 26</a>' not in page_two
    assert '?before=6' in page_two and page_two.count('资格已变化') == 1
    row = re.search(r'<tr><th scope="row"><a href="[^"]+/3">.*?</tr>', page_two, re.S)[0]
    assert '投用中' in row and '已停用' in row and '资格已变化' in row
    row = re.search(r'<tr><th scope="row"><a href="[^"]+/1">.*?</tr>', page_two, re.S)[0]
    assert '资格已变化' not in row and row.count('已停用') == 2
    html(base, '/workbench/recipes/browser-swatch/history/' + 'f' * 32, expected=404)
    html(base, first_page + '?before=abc', expected=400)
    html(base, editor_url, form | {'name': 'other-swatch'})
    html(base, f'/workbench/recipes/other-swatch/history/{session_id}', expected=404)

    assert '已构建' in html(base, first + '/build', {})[1]
    assert '已认证' in html(base, first + '/certify', {})[1]
    _, headers, database = open_page(base, first + '/database')
    assert headers['ETag'] == '"' + hashlib.sha256(database).hexdigest() + '"'
    handoff = root / 'handoff.sqlite'
    handoff.write_bytes(database)
    with closing(sqlite3.connect(handoff)) as conn:
        assert conn.execute("SELECT length_um FROM assay WHERE mark='S-01'").fetchone() == (3.25,)
        assert conn.execute('PRAGMA foreign_key_check').fetchall() == []
    print('浏览器端到端检查通过：目录、配方、试算、捕获、证据、对比、停用、历史切面、构建、认证、下载')


LEGACY_DDL = '''
CREATE TABLE recipes (
    name TEXT NOT NULL, version INTEGER NOT NULL, active INTEGER NOT NULL,
    schema_digest TEXT NOT NULL, recipe TEXT NOT NULL, PRIMARY KEY (name, version)
);
CREATE TABLE captures (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, version INTEGER NOT NULL,
    sealed TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN ('sealed','built','certified')),
    artifact BLOB, certificate TEXT,
    FOREIGN KEY(name, version) REFERENCES recipes(name, version)
);
'''


def legacy_check(source):
    with TemporaryDirectory(prefix='keelsplice-legacy-') as directory:
        repo = Path(directory) / 'legacy.sqlite'
        with closing(sqlite3.connect(repo)) as conn, conn:
            conn.executescript(LEGACY_DDL)
            conn.execute("INSERT INTO recipes VALUES('legacy',1,0,'legacy',?)",
                         ('{"source":"microscope","roots":[],"follow_children":[],"row_limit":1000}',))
        with serve(repo, source) as base:
            listing = html(base, '/workbench/recipes')[1]
            assert 'legacy' in listing and '当前状态' in listing
            handoff, page = html(base, '/workbench/recipes/legacy/history', {})
            assert '当时投用结论' in page and '当前资格' in page and '已停用' in page
        with serve(repo, source) as base:
            assert html(base, handoff)[1] == page
    print('旧状态库检查通过：仅存 active 标志的旧数据自动迁移，交接地址重开切面内容一致')


def main():
    parser = argparse.ArgumentParser(description='在临时数据库演练关系样本工作台。')
    parser.parse_args()
    with TemporaryDirectory(prefix='keelsplice-browser-') as directory:
        root = Path(directory)
        source = root / 'source.sqlite'
        seed(source)
        with serve(root / 'state.sqlite', source) as base:
            run(base, source, root)
        legacy_check(source)


if __name__ == '__main__':
    main()

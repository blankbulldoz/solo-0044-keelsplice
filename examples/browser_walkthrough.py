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
    assert '已构建' in html(base, first + '/build', {})[1]
    assert '已认证' in html(base, first + '/certify', {})[1]
    _, headers, database = open_page(base, first + '/database')
    assert headers['ETag'] == '"' + hashlib.sha256(database).hexdigest() + '"'
    handoff = root / 'handoff.sqlite'
    handoff.write_bytes(database)
    with closing(sqlite3.connect(handoff)) as conn:
        assert conn.execute("SELECT length_um FROM assay WHERE mark='S-01'").fetchone() == (3.25,)
        assert conn.execute('PRAGMA foreign_key_check').fetchall() == []

    _, watch_editor = html(base, '/workbench/watches/new?source=microscope')
    assert '外键关系' in watch_editor and '主键定义' in watch_editor
    detail_path, watch_page = html(base, '/workbench/watches/new', {
        'source': 'microscope', 'subject': 'edge:assay:0',
        'continuity_seconds': '1', 'note': '盯真实列映射'})
    assert '连续核对一致' in watch_page and '盯真实列映射' in watch_page
    watch_id = detail_path.rsplit('/', 1)[1]
    _, listing = html(base, '/workbench/watches')
    assert '源模式关注列表' in listing and watch_id in listing
    # Duplicate subject is rejected and the editor keeps its input.
    _, duplicate = html(base, '/workbench/watches/new', {
        'source': 'microscope', 'subject': 'edge:assay:0', 'continuity_seconds': '1'}, expected=409)
    assert '已经在关注列表中' in duplicate
    # An unrelated index does not raise a change.
    with closing(sqlite3.connect(source)) as conn, conn:
        conn.execute('CREATE INDEX browser_extra ON assay(cassette)')
    _, checked = html(base, '/workbench/sources/microscope/check', {})
    assert '目录核对完成' in checked and '本次产生通知' in checked
    _, still_verified = html(base, f'/workbench/watches/{watch_id}')
    assert '连续核对一致' in still_verified and '结构相同但存在未观测区间' not in still_verified
    # Breaking the watched foreign key creates a change notification with evidence.
    with closing(sqlite3.connect(source)) as conn, conn:
        conn.execute('CREATE TABLE assay_b (mark TEXT PRIMARY KEY NOT NULL, region TEXT, code TEXT, '
                     'cassette INTEGER NOT NULL, length_um REAL NOT NULL, '
                     'doubled REAL GENERATED ALWAYS AS (length_um*2) STORED)')
        conn.execute('INSERT INTO assay_b(mark,region,code,cassette,length_um) '
                     'SELECT mark,region,code,cassette,length_um FROM assay')
        conn.execute('DROP TABLE assay')
        conn.execute('ALTER TABLE assay_b RENAME TO assay')
    _, changed_page = html(base, '/workbench/sources/microscope/check', {})
    assert '目录核对完成' in changed_page
    _, notifications_page = html(base, '/workbench/notifications')
    assert '模式变化通知' in notifications_page and '外键 assay #0' in notifications_page
    notice_link = re.search(r'/workbench/notifications/[0-9a-f]{32}', notifications_page)[0]
    _, notice = html(base, notice_link)
    assert '当时各次目录结论' in notice and '当时使用这条结构的配方版本' in notice
    assert 'browser-swatch' in notice
    assert '确认已知悉' in notice
    _, acknowledged = html(base, notice_link + '/acknowledge', {})
    assert '已确认' in acknowledged and '确认已知悉' not in acknowledged
    # A primary-key watch is available from the catalog page.
    _, catalog_frame = html(base, '/workbench?source=microscope&table=frame')
    pk_link = re.search(r'/workbench/watches/new\?source=microscope&amp;subject=pk%3Aframe',
                        catalog_frame)
    assert pk_link is not None
    _, pk_created = html(base, '/workbench/watches/new', {
        'source': 'microscope', 'subject': 'pk:frame', 'continuity_seconds': '60'})
    assert '主键定义' in pk_created
    print('浏览器端到端检查通过：目录、配方、试算、捕获、证据、对比、停用、构建、认证、下载、源模式关注与通知')


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

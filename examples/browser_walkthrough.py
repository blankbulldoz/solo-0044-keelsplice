"""Exercise the production workbench with the disposable microscope database."""

import argparse
import base64
import hashlib
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


def parse_edge(link):
    token = re.search(r'edge=([^"&]+)', link)[1]
    return json.loads(base64.urlsafe_b64decode(token + '=' * (-len(token) % 4)))


def edge_links(page):
    """Return every relationship mapping address rendered in a page, with its stored reference."""
    result = []
    for link in re.findall(r'href="(/workbench/relations\?edge=[^"]+)"', page):
        result.append((link.replace('&amp;', '&'), parse_edge(link)))
    return result


def browse_relationships(base):
    _, cassette = html(base, '/workbench?source=microscope&table=cassette')
    assert '自引用' in cassette and '查看列映射' in cassette
    self_link = next(link for link, ref in edge_links(cassette) if ref['child'] == 'cassette')
    _, self_page = html(base, self_link)
    assert '正向补齐父行' in self_page and '反向纳入子行' in self_page
    assert '查看本表' in self_page and '自引用' in self_page
    assert '正在考察' in self_page

    _, assay = html(base, '/workbench?source=microscope&table=assay')
    assert '查看列映射' in assay and '同父多边' in assay
    links = edge_links(assay)
    composite = next((link, ref) for link, ref in links if ref['child_cols'] == ['region', 'code'])
    sibling = next((link, ref) for link, ref in links if ref['child_cols'] == ['fallback_region', 'fallback_code'])
    assert composite[1]['parent'] == sibling[1]['parent'] == 'substrate'
    assert composite[1]['id'] != sibling[1]['id']
    _, mapping = html(base, composite[0])
    assert '必需父关系' in mapping and '配方反向纳入' in mapping and '可空列' in mapping
    assert '另一条关系' in mapping and '正在考察' in mapping
    _, sparse = html(base, sibling[0])
    assert 'fallback_region' in sparse and '可空列' in sparse
    return assay, composite[0]


def focus_notice(base, path, expected):
    _, page = html(base, path)
    assert expected in page, page[:400]
    return page


def run(base, source, root):
    path, directory = html(base, '/')
    assert path == '/workbench' and '外键关系' in directory
    _, table = html(base, '/workbench?source=microscope&table=substrate')
    assert 'region → code' in table and 'cassette' in table
    assert open_page(base, '/workbench/assets/workspace.css')[1].get_content_type() == 'text/css'
    assert '未注册' in html(base, '/workbench?source=missing', expected=404)[1]
    _, focused = browse_relationships(base)

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

    token = re.search(r'edge=([^"&]+)', focused)[1]
    directory_focus = '/workbench?source=microscope&table=assay&edge=' + token
    assert '正在考察' in focus_notice(base, directory_focus, '正在考察')
    with closing(sqlite3.connect(source)) as conn, conn:
        conn.execute('CREATE INDEX frame_note_lookup ON frame(note)')
    still = focus_notice(base, directory_focus, '正在考察')
    assert '另一组列' not in still

    with closing(sqlite3.connect(source)) as conn, conn:
        conn.execute('ALTER TABLE assay RENAME COLUMN region TO sample_region')
    drifted = focus_notice(base, directory_focus, '现在指向另一组列')
    assert '复查旧关系并重新选择' in drifted
    _, mapping = html(base, focused)
    assert '原先考察的关系' in mapping and '已指向另一组列' in mapping
    assert 'region' in mapping and 'sample_region' in mapping and '重新选择' in mapping
    with closing(sqlite3.connect(source)) as conn, conn:
        conn.execute('ALTER TABLE assay RENAME COLUMN sample_region TO region')
    assert '正在考察' in focus_notice(base, directory_focus, '正在考察')
    assert '正在考察' in html(base, focused)[1]

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

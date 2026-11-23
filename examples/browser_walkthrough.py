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


def get_html(base, path, query, expected=200):
    return html(base, path + '?' + urlencode(query), expected=expected)


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

    # 封存一份包含大 BLOB、形近异类型值和虚拟生成列的捕获，再浏览入选行。
    specimen_editor = html(base, '/workbench/recipes/new?source=microscope')[1]
    specimen_digest = re.search(r'name="digest" value="([0-9a-f]{64})"', specimen_editor)[1]
    specimen_form = {'source': 'microscope', 'name': 'browser-specimen', 'digest': specimen_digest,
                     'row_limit': '50', 'edge': [],
                     'roots': '[{"table":"specimen","key":[1]},{"table":"specimen","key":[2]},'
                              '{"table":"specimen","key":[3]}]'}
    specimen_version, _ = html(base, editor_url, specimen_form)
    specimen_capture, specimen_page = html(base, specimen_version + '/capture', {})
    assert specimen_capture.startswith('/workbench/captures/')
    # 详情页逐表计数可进入行浏览，并始终显示原捕获编号与模式摘要。
    assert (specimen_capture + '/rows?table=specimen') in specimen_page
    _, rows_page = get_html(base, specimen_capture + '/rows', {'table': 'specimen'})
    assert '封存入选行' in rows_page and specimen_capture.rsplit('/', 1)[1] in rows_page
    assert '虚拟生成' in rows_page and '实存' in rows_page and 'alpha#1' in rows_page
    assert 'type-badge blob' in rows_page and '已截短' in rows_page and '展开原值' in rows_page
    # 文本 12 与整数 12 表面相同，但带不同存储类型徽标。
    assert 'type-badge text' in rows_page and 'type-badge integer' in rows_page
    # 只有 DDL、零入选行的表给出空态而非报错。
    _, empty_page = get_html(base, specimen_capture + '/rows', {'table': 'frame'})
    assert '没有入选行' in empty_page and '只有 DDL' in empty_page
    # 越界与非法页码有明确页面，且不离开原捕获。
    assert '页码超出' in get_html(base, specimen_capture + '/rows',
                                 {'table': 'specimen', 'page': '99'}, expected=404)[1]
    assert '页码' in get_html(base, specimen_capture + '/rows',
                              {'table': 'specimen', 'page': 'x'}, expected=400)[1]
    # 展开大 BLOB：完整十六进制、字节数和逐值摘要，确认截短不是原值。
    _, value_page = get_html(base, specimen_capture + '/value',
                             {'table': 'specimen', 'column': 'payload'})
    assert '封存值详情' in value_page and '300 字节' in value_page
    assert '030a11181f262d343b4249' in value_page and specimen_capture.rsplit('/', 1)[1] in value_page
    value_hashes = re.findall(r'<td><code>([0-9a-f]{64})</code></td>', value_page)
    assert len(value_hashes) == 3 and len(set(value_hashes)) == 3
    # 虚拟生成列同样可展开，值来自封存记录。
    _, generated_page = get_html(base, specimen_capture + '/value',
                                 {'table': 'specimen', 'column': 'alias_text'})
    assert '虚拟生成' in generated_page and 'beta#2' in generated_page
    # 形近的文本与整数逐值摘要不同，可据此确认没有把类型抹平。
    note_hash = re.findall(r'<td><code>([0-9a-f]{64})</code></td>',
                           get_html(base, specimen_capture + '/value',
                                    {'table': 'specimen', 'column': 'note'})[1])
    tag_hash = re.findall(r'<td><code>([0-9a-f]{64})</code></td>',
                          get_html(base, specimen_capture + '/value',
                                   {'table': 'specimen', 'column': 'tag'})[1])
    assert note_hash and tag_hash and note_hash != tag_hash
    # 从浏览到的任意一行继续追踪，仍使用同次捕获的精确键。
    _, continued = html(base, specimen_capture + '/trace',
                        {'table': 'specimen', 'key': '[2]'})
    assert '入选证据' in continued and '实存主键' in continued
    assert get_html(base, specimen_capture + '/value',
                    {'table': 'specimen', 'column': 'missing'}, expected=404)[0].endswith('/value')

    # 构建并认证这份捕获，独立连接核对生成列、完整 BLOB 与存储类型。
    assert '已构建' in html(base, specimen_capture + '/build', {})[1]
    assert '已认证' in html(base, specimen_capture + '/certify', {})[1]
    _, spec_headers, spec_database = open_page(base, specimen_capture + '/database')
    spec_handoff = root / 'specimen-handoff.sqlite'
    spec_handoff.write_bytes(spec_database)
    with closing(sqlite3.connect(spec_handoff)) as conn:
        assert conn.execute(
            'SELECT alias_text FROM specimen WHERE specimen_id=2').fetchone() == ('beta#2',)
        blob = conn.execute('SELECT payload FROM specimen WHERE specimen_id=1').fetchone()[0]
        assert len(blob) == 300
        assert conn.execute(
            "SELECT typeof(note), typeof(tag) FROM specimen WHERE specimen_id=2"
        ).fetchone() == ('text', 'integer')
        assert conn.execute('PRAGMA foreign_key_check').fetchall() == []
    assert spec_headers['ETag'] == '"' + hashlib.sha256(spec_database).hexdigest() + '"'

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

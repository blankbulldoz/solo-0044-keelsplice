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


def seed_precise(file):
    """Add a BLOB + long-integer key table used by the typed-editor checks."""
    with closing(sqlite3.connect(file)) as conn, conn:
        conn.execute('CREATE TABLE token (lot BLOB NOT NULL, serial INTEGER NOT NULL, label TEXT,'
                     ' PRIMARY KEY (lot, serial)) WITHOUT ROWID')
        conn.execute("INSERT INTO token VALUES (x'0000ff', 9007199254740993, 'big'), (x'', 1, 'empty')")


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
    assert '根位置（按输入顺序）' in editor and '添加根' in editor
    # Add a second card for `cassette`, then fill both roots cell by cell.
    cards = {'source': 'microscope', 'name': 'browser-swatch', 'digest': digest, 'row_limit': '20',
             'action': 'add:0', 'add_table': 'cassette',
             'root_table_0': ['substrate'], 'root_kind_0': ['text', 'text'],
             'root_value_0': ['east', 'MICA'], 'edge': ['0:assay']}
    _, added = html(base, editor_url, cards)
    assert added.count('class="root-card"') == 2 and '根 2' in added
    two = {key: value for key, value in cards.items() if key != 'action'}
    two |= {'root_table_1': ['cassette'], 'root_kind_1': ['integer'], 'root_value_1': ['11']}
    # Reordering keeps the typed cells and changes which root position they occupy.
    moved = two | {'action': 'down:0'}
    _, reordered = html(base, editor_url, moved)
    summary = reordered.split('root-summary">', 1)[1].split('</ol>', 1)[0]
    assert summary.index('cassette') < summary.index('substrate')
    assert 'value="east"' in reordered and 'value="11"' in reordered
    # Table switching reshapes the cells to the newly chosen table's key order.
    swapped = {key: value for key, value in cards.items()
               if not key.startswith(('root_table_1', 'root_kind_1', 'root_value_1'))}
    swapped |= {'root_table_0': ['cassette'], 'root_kind_0': ['text'],
                'root_value_0': ['11'], 'action': 'retable:0'}
    _, refreshed = html(base, editor_url, swapped)
    assert refreshed.count('name="root_kind_0"') == 1
    # Canonical JSON imports into the same cards, and the JSON box mirrors them.
    pasted = '[{"table":"substrate","key":["east","MICA"]}]'
    _, imported = html(base, editor_url, {'source': 'microscope', 'digest': digest, 'name': 'browser-swatch',
                                          'row_limit': '20', 'roots': pasted, 'action': 'import',
                                          'edge': ['0:assay']})
    assert 'value="east"' in imported and 'value="MICA"' in imported
    # A pasted box that was not imported is flagged rather than silently dropped.
    conflict = {'source': 'microscope', 'digest': digest, 'name': 'browser-swatch', 'row_limit': '20',
                'rendered': pasted, 'roots': '[{"table":"cassette","key":[13]}]',
                'root_table_0': ['cassette'], 'root_kind_0': ['integer'],
                'root_value_0': ['11']}
    _, clash = html(base, editor_url, conflict, expected=400)
    assert '不一致' in clash and 'value="11"' in clash
    # A navigation control instead adopts the newer JSON and keeps the paste.
    adopted = conflict | {'action': 'add', 'add_table': 'cassette'}
    _, adopt_page = html(base, editor_url, adopted)
    assert '已先按原始 JSON' in adopt_page and 'value="13"' in adopt_page
    form = {'source': 'microscope', 'name': 'browser-swatch', 'digest': digest,
            'row_limit': '20', 'edge': ['0:assay'],
            'root_table_0': ['substrate'], 'root_kind_0': ['text', 'text'],
            'root_value_0': ['east', 'MICA']}
    assert '目录已变化' in html(base, editor_url, form | {'digest': 'stale'}, expected=409)[1]
    assert '本机工作台' in html(base, editor_url, form, expected=403, origin='http://localhost:1')[1]
    assert '根 1' in html(base, editor_url, form | {'root_kind_0': ['integer']}, expected=400)[1]
    _, raw_error = html(base, editor_url, {'source': 'microscope', 'name': 'browser-swatch', 'digest': digest,
                                           'row_limit': '20', 'roots': 'bad', 'action': 'import'}, expected=400)
    assert '有效的 JSON' in raw_error and 'browser-swatch' in raw_error
    version, page = html(base, editor_url, form)
    assert version.endswith('/1') and '投用中' in page
    assert 'browser-swatch' in html(base, '/workbench/recipes')[1]

    # The same real row registered with two spellings keeps both typed root positions.
    _, preview = html(base, version + '/preview', {})
    assert '未封存' in preview and 'East' in preview and 'Mica' in preview
    cast = {'source': 'microscope', 'digest': digest, 'name': 'browser-types', 'row_limit': '20',
            'action': 'up:0',
            'root_table_0': ['substrate'], 'root_kind_0': ['text', 'text'],
            'root_value_0': ['EAST', 'mica'],
            'root_table_1': ['cassette'], 'root_kind_1': ['integer'], 'root_value_1': ['11'],
            'root_table_2': ['substrate'], 'root_kind_2': ['text', 'text'],
            'root_value_2': ['east', 'MICA']}
    _, typed = html(base, editor_url, cast)
    canonical = unescape(re.search(r'<textarea[^>]*>(.*?)</textarea>', typed, re.S)[1])
    assert json.loads(canonical) == [
        {'table': 'substrate', 'key': ['EAST', 'mica']},
        {'table': 'cassette', 'key': [11]},
        {'table': 'substrate', 'key': ['east', 'MICA']},
    ]
    # A JSON import fills the same cards, so both editors describe one recipe.
    _, imported = html(base, editor_url, {'source': 'microscope', 'digest': digest, 'name': 'browser-types',
                                          'row_limit': '20', 'roots': canonical, 'action': 'import'})
    assert imported.count('class="root-card"') == 3
    assert re.findall(r'name="root_value_0" value="([^"]*)"', imported) == ['EAST', 'mica']
    # Long integers, empty strings and BLOBs submit exactly as typed.
    _, editor = html(base, editor_url)
    digest = re.search(r'name="digest" value="([0-9a-f]{64})"', editor)[1]
    precise = {'source': 'microscope', 'digest': digest, 'name': 'browser-precise', 'row_limit': '20',
               'root_table_0': ['token'], 'root_kind_0': ['blob', 'integer'],
               'root_value_0': ['AAD/', '9007199254740993'],
               'root_table_1': ['token'], 'root_kind_1': ['blob', 'integer'],
               'root_value_1': ['', '1'],
               'root_table_2': ['frame'], 'root_kind_2': ['integer'], 'root_value_2': ['101']}
    precise_version, _ = html(base, editor_url, precise)
    assert precise_version.endswith('/1')
    precise_capture, precise_page = html(base, precise_version + '/capture', {})
    assert 'AAD/' in precise_page and '9007199254740993' in precise_page
    html(base, precise_capture + '/build', {})
    html(base, precise_capture + '/certify', {})
    _, _, precise_bytes = open_page(base, precise_capture + '/database')
    precise_file = root / 'precise.sqlite'
    precise_file.write_bytes(precise_bytes)
    with closing(sqlite3.connect(precise_file)) as conn:
        assert conn.execute('SELECT hex(lot), serial FROM token ORDER BY serial').fetchall() == [
            ('', 1), ('0000FF', 9007199254740993)]

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
    print('浏览器端到端检查通过：目录、配方、试算、捕获、证据、对比、停用、构建、认证、下载')


def main():
    parser = argparse.ArgumentParser(description='在临时数据库演练关系样本工作台。')
    parser.parse_args()
    with TemporaryDirectory(prefix='keelsplice-browser-') as directory:
        root = Path(directory)
        source = root / 'source.sqlite'
        seed(source)
        seed_precise(source)
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

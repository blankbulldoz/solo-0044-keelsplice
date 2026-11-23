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

REPLICA_SQL = '''
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
CREATE TABLE substrate (
    code TEXT COLLATE NOCASE NOT NULL,
    region TEXT COLLATE NOCASE NOT NULL,
    texture BLOB NOT NULL,
    PRIMARY KEY (code, region)
) WITHOUT ROWID;
CREATE TABLE cassette (
    serial INTEGER PRIMARY KEY,
    label TEXT NOT NULL
);
CREATE TABLE assay (
    mark TEXT PRIMARY KEY NOT NULL,
    cassette INTEGER NOT NULL REFERENCES cassette,
    length_um REAL NOT NULL
);
BEGIN;
INSERT INTO substrate VALUES ('Mica','East',x'0000ff'), ('Basalt','West',x'fe');
INSERT INTO cassette VALUES (11,'opal'), (12,'agate');
INSERT INTO assay VALUES ('S-01',11,3.25), ('S-02',12,5.5);
COMMIT;
'''

ROOTS = '[{"table":"substrate","key":["east","MICA"]},' \
        '{"table":"cassette","key":[11]},{"table":"assay","key":["S-01"]}]'


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


def hidden(page, name):
    return re.search(rf'name="{name}" value="([^"]*)"', page)[1]


def drafts_flow(base):
    _, editor = html(base, '/workbench/recipes/new?source=microscope')
    form = {'source': 'microscope', 'digest': hidden(editor, 'digest'), 'revision': '',
            'name': 'parallel-swatch', 'row_limit': '50', 'roots': ROOTS,
            'edge': ['0:assay'], 'intent': 'save', 'switch': 'replica'}
    _, saved = html(base, '/workbench/drafts', form)
    assert '草稿已保存' in saved and 'parallel-swatch' in saved and hidden(saved, 'revision') == '1'

    form['revision'] = hidden(saved, 'revision')
    _, switched = html(base, '/workbench/drafts', form | {'intent': 'switch'})
    assert '登记配方 · 源 replica' in switched and hidden(switched, 'revision') == ''

    _, copied = html(base, '/workbench/drafts/copy', form)
    assert '已创建' in copied and copied.count('需在该源编辑页确认') == 1
    assert 'cassette' in copied and 'assay' in copied and '两源主键列或其次序不同' in copied
    _, again = html(base, '/workbench/drafts/copy', form)
    assert '未重复复制' in again

    _, replica = html(base, '/workbench/recipes/new?source=replica')
    assert hidden(replica, 'revision') == '1' and 'parallel-swatch' in replica
    body = re.search(r'<textarea[^>]*name="roots"[^>]*>(.*?)</textarea>', replica, re.S)[1]
    assert 'cassette' in body and 'assay' in body and 'substrate' not in body
    assert body.index('cassette') < body.index('assay')
    assert replica.count('需确认') >= 2 and '确认 2 项复制根' in replica

    _, confirmed = html(base, '/workbench/drafts/confirm',
                        {'source': 'replica', 'revision': hidden(replica, 'revision')})
    assert '复制项已确认' in confirmed and confirmed.count('已复制') >= 2 and '确认 2 项复制根' not in confirmed

    rev = hidden(confirmed, 'revision')
    other = {'source': 'replica', 'revision': rev, 'name': 'parallel-swatch', 'row_limit': '50',
             'roots': '[{"table":"cassette","key":[11]},{"table":"assay","key":["S-01"]},'
                      '{"table":"cassette","key":[12]}]',
             'intent': 'save'}
    _, saved_other = html(base, '/workbench/drafts', other)
    assert '草稿已保存' in saved_other
    mine = other | {'revision': rev, 'name': 'parallel-renamed',
                    'roots': '[{"table":"cassette","key":[11]}]'}
    _, clash = html(base, '/workbench/drafts', mine, expected=409)
    assert '已复制（来自并行草稿）' in clash and '需确认（已保存版本有而本页未包含）' in clash
    assert '两侧各自编辑（本页提交有而已保存版本未包含）' in clash
    assert 'cassette' in clash and 'parallel-renamed' in clash and 'parallel-swatch' in clash
    _, forced = html(base, '/workbench/drafts', mine | {'revision': hidden(clash, 'revision')})
    assert '草稿已保存' in forced and 'parallel-renamed' in forced

    _, replica = html(base, '/workbench/recipes/new?source=replica')
    register = {'source': 'replica', 'digest': hidden(replica, 'digest'),
                'revision': hidden(replica, 'revision'), 'name': 'parallel-replica',
                'row_limit': '50', 'roots': '[{"table":"cassette","key":[11]}]'}
    _, stale = html(base, '/workbench/recipes/new', register | {'revision': '1'}, expected=409)
    assert '草稿在另一页已更新' in stale and '仍然登记到 replica' in stale
    version, page = html(base, '/workbench/recipes/new', register)
    assert version.endswith('/1') and 'replica' in page and '投用中' in page

    _, microscope = html(base, '/workbench/recipes/new?source=microscope')
    assert 'parallel-swatch' in microscope and 'substrate' in microscope and 'replica' in microscope
    register = {'source': 'microscope', 'digest': hidden(microscope, 'digest'),
                'revision': hidden(microscope, 'revision'), 'name': 'parallel-microscope',
                'row_limit': '50', 'roots': ROOTS, 'edge': ['0:assay']}
    version, page = html(base, '/workbench/recipes/new', register)
    assert version.endswith('/1') and 'microscope' in page and '投用中' in page
    print('并行草稿检查通过：保存切换、兼容复制、确认标记、并发对照、各自登记到明确的源')


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
    print('浏览器端到端检查通过：目录、配方、试算、捕获、证据、对比、停用、构建、认证、下载')
    drafts_flow(base)


def main():
    parser = argparse.ArgumentParser(description='在临时数据库演练关系样本工作台。')
    parser.parse_args()
    with TemporaryDirectory(prefix='keelsplice-browser-') as directory:
        root = Path(directory)
        source = root / 'source.sqlite'
        seed(source)
        replica = root / 'replica.sqlite'
        with closing(sqlite3.connect(replica)) as conn:
            conn.executescript(REPLICA_SQL)
        sources = {'microscope': source, 'replica': replica}
        with create_server(('127.0.0.1', 0), root / 'state.sqlite', sources) as server:
            thread = Thread(target=server.serve_forever)
            thread.start()
            try:
                run(f'http://127.0.0.1:{server.server_port}', source, root)
            finally:
                server.shutdown()
                thread.join()


if __name__ == '__main__':
    main()

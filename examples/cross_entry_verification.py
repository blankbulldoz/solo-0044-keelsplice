"""跨入口验证：同一配方经浏览器与 JSON 两个入口完成试算、封存与交接。

从工作台登记带 NOCASE 复合键、重复根和大整数键的配方；一次合法的源数据更新
（只改数据、不动模式，配方绑定保持有效）落在浏览器试算与 JSON 封存之间。
三条判据彼此独立：

1. 根出现项 —— 状态接口、追踪接口、状态库封存记录与浏览器页面分别核对：
   输入顺序保留、实存拼写取代输入拼写、重复根各占一项。
2. 实际 SQLite 类型 —— 用独立 SQLite 连接打开下载的子库，以 typeof() 核对
   存储类型，并逐值比对精确大整数、BLOB、REAL 与生成列。
3. 两次读取的先后关系 —— 试算计数停在源更新之前，封存计数与封存内容落在
   源更新之后；方向颠倒（封存复用试算快照）同样失败。

页面文字只是必要判据：状态库中的封存 JSON 与下载字节都经独立连接复核，
页面文字相同而封存或下载内容错误时，本脚本必须失败。
"""

import json
import re
import sqlite3
from contextlib import closing
from hashlib import sha256
from html import unescape
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

from keelsplice.api import create_server

from .walkthrough import seed

NAME = 'cross-entry-swatch'
# 超过 2**53 的 int64：任何经 float64 的往返都会改变末位数字。
BIG_POS = 2**62 + 12345
BIG_NEG = -(2**62) - 12345

ROOTS_INPUT = [
    {'table': 'substrate', 'key': ['east', 'MICA']},
    {'table': 'substrate', 'key': ['EAST', 'mica']},
    {'table': 'substrate', 'key': ['East', 'Mica']},
    {'table': 'cassette', 'key': [BIG_POS]},
    {'table': 'cassette', 'key': [BIG_NEG]},
]
# 三个 NOCASE 拼写解析为同一实存根，根出现项仍各占一位。
ROOTS_ACTUAL = [
    ['substrate', ['East', 'Mica']],
    ['substrate', ['East', 'Mica']],
    ['substrate', ['East', 'Mica']],
    ['cassette', [BIG_POS]],
    ['cassette', [BIG_NEG]],
]
COUNTS_BEFORE = {'assay': 2, 'cassette': 4, 'frame': 3, 'substrate': 1}
COUNTS_AFTER = {'assay': 3, 'cassette': 4, 'frame': 3, 'substrate': 1}


def call(base, route, body=None, status=200):
    """JSON 入口请求。"""
    headers = {'Content-Type': 'application/json'}
    if body is not None:
        body = json.dumps(body).encode('utf-8')
    try:
        response = urlopen(Request(base + route, body, headers), timeout=5)
    except HTTPError as exc:
        response = exc
    with response:
        data = response.read()
        assert response.status == status, (route, response.status, data[:500])
        if response.headers.get_content_type() == 'application/vnd.sqlite3':
            return data, response.headers['ETag'].strip('"')
        return json.loads(data)


def open_page(base, path, payload=None, expected=200):
    """浏览器入口请求，表单经 application/x-www-form-urlencoded 提交。"""
    data = None if payload is None else urlencode(payload, doseq=True).encode('ascii')
    headers = {} if data is None else {'Origin': base}
    try:
        response = urlopen(Request(base + path, data=data, headers=headers), timeout=5)
    except HTTPError as exc:
        response = exc
    with response:
        assert response.status == expected, (path, response.status, response.read()[:500])
        return urlsplit(response.url).path, response.headers, response.read()


def show(base, path, payload=None, expected=200):
    path, headers, body = open_page(base, path, payload, expected)
    assert headers.get_content_type() == 'text/html', path
    return path, body.decode('utf-8')


def page_counts(page):
    """从试算或捕获详情页解析逐表计数。"""
    return {name: int(count) for name, count in
            re.findall(r'<th scope="row"><code>([^<]+)</code></th><td>(\d+)</td>', page)}


def page_roots(page, heading):
    """从页面根列表解析 [表, 键] 序列，保持页面顺序。"""
    section = re.search(heading + r'</h2><ol class="data-list">(.*?)</ol>', page, re.S)
    assert section, f'页面缺少根列表：{heading}'
    return [[table, json.loads(unescape(key))] for table, key in
            re.findall(r'<code>([^<]+)</code> <code>(\[.*?\])</code>', section[1])]


def sealed_record(repo, capture_id):
    """绕过服务，用独立连接直接读取状态库中的封存 JSON。"""
    with closing(sqlite3.connect(repo)) as conn:
        row = conn.execute('SELECT sealed FROM captures WHERE id=?', (capture_id,)).fetchone()
    assert row, f'状态库中没有捕获 {capture_id}'
    return json.loads(row[0])


def verify_first_fixture(conn):
    """判据二：在独立 SQLite 连接上核对下载子库的实际类型与内容。"""
    assert conn.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    assert conn.execute('PRAGMA foreign_key_check').fetchall() == []
    for table, expected in COUNTS_AFTER.items():
        count = conn.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]
        assert count == expected, (table, count, expected)

    substrate = conn.execute(
        'SELECT region, typeof(region), code, typeof(code), texture, typeof(texture) FROM substrate').fetchone()
    assert substrate == ('East', 'text', 'Mica', 'text', b'\x00\x00\xff', 'blob'), substrate

    cassettes = conn.execute('SELECT serial, typeof(serial), prior, label FROM cassette ORDER BY serial').fetchall()
    assert cassettes == [(BIG_NEG, 'integer', None, 'huge-neg'), (11, 'integer', 12, 'opal'),
                         (12, 'integer', 11, 'agate'), (BIG_POS, 'integer', None, 'huge-pos')], cassettes
    assert all(type(serial) is int for serial, _, _, _ in cassettes)

    assays = conn.execute(
        'SELECT mark, region, code, cassette, length_um, typeof(length_um), doubled, typeof(doubled)'
        ' FROM assay ORDER BY mark').fetchall()
    assert assays == [('S-01', 'east', 'MICA', 11, 4.75, 'real', 9.5, 'real'),
                      ('S-02', 'EAST', 'mica', 12, 5.5, 'real', 11.0, 'real'),
                      ('S-05', 'EAST', 'mica', BIG_POS, 6.25, 'real', 12.5, 'real')], assays
    assert all(type(length) is float and type(doubled) is float
               for _, _, _, _, length, _, doubled, _ in assays)

    frames = conn.execute('SELECT frame_id, mark, note FROM frame ORDER BY frame_id').fetchall()
    assert frames == [(101, 'S-01', 'front'), (102, 'S-02', 'detail'), (103, 'S-02', 'side')], frames

    ddl = conn.execute("SELECT sql FROM sqlite_schema WHERE type='table' AND name='substrate'").fetchone()[0]
    assert ddl.count('COLLATE NOCASE') == 2 and 'WITHOUT ROWID' in ddl
    assay_ddl = conn.execute("SELECT sql FROM sqlite_schema WHERE type='table' AND name='assay'").fetchone()[0]
    assert 'GENERATED ALWAYS' in assay_ddl
    indexes = {row[0] for row in conn.execute("SELECT name FROM sqlite_schema WHERE type='index' AND sql IS NOT NULL")}
    assert {'assay_substrate_lookup', 'frame_mark_lookup'} <= indexes


def run(base, source, repo, root):
    # 0. JSON 目录：NOCASE 复合主键与外键编号是后续所有判据的基准。
    catalog = call(base, '/sources/microscope/catalog')
    digest = catalog['schema_digest']
    substrate = catalog['schema']['tables']['substrate']
    assert substrate['pk'] == ['region', 'code'] and substrate['ddl'].count('COLLATE NOCASE') == 2
    edges = {(edge['child'], edge['id']): edge for edge in catalog['schema']['edges']}
    assert edges[('assay', 0)]['parent'] == 'substrate' and edges[('assay', 0)]['parent_cols'] == ['region', 'code']
    assert edges[('frame', 0)]['parent'] == 'assay'

    # 1. 浏览器入口登记配方：NOCASE 复合键、三个重复根、两个大整数键。
    _, editor = show(base, '/workbench/recipes/new?source=microscope')
    form_digest = re.search(r'name="digest" value="([0-9a-f]{64})"', editor)[1]
    assert form_digest == digest, '浏览器表单与 JSON 目录应绑定同一模式摘要'
    assert 'value="0:assay"' in editor and 'value="0:frame"' in editor
    form = {'source': 'microscope', 'name': NAME, 'digest': form_digest, 'row_limit': '50',
            'edge': ['0:assay', '0:frame'], 'roots': json.dumps(ROOTS_INPUT)}
    recipe_url, recipe_page = show(base, '/workbench/recipes/new', form)
    assert recipe_url == f'/workbench/recipes/{NAME}/1' and '投用中' in recipe_page and digest in recipe_page
    assert page_roots(recipe_page, '登记的根记录') == [[r['table'], r['key']] for r in ROOTS_INPUT]
    saved = call(base, f'/recipes/{NAME}/versions/1')
    assert saved['recipe']['roots'] == ROOTS_INPUT, '配方应保留输入拼写'
    assert saved['recipe']['follow_children'] == [{'child': 'assay', 'id': 0}, {'child': 'frame', 'id': 0}]
    lookup = {'name': NAME, 'version': 1}

    # 2. 浏览器入口试算：第一次读取，发生在源更新之前。
    _, preview_page = show(base, recipe_url + '/preview', {})
    assert '未封存' in preview_page
    assert page_counts(preview_page) == COUNTS_BEFORE
    assert page_roots(preview_page, '源库实存根') == ROOTS_ACTUAL
    assert '入选行</dt><dd>10</dd>' in preview_page and '关系见证</dt><dd>14</dd>' in preview_page

    # 3. 合法的源数据更新（不改模式、不破坏外键）落在试算与封存之间。
    with closing(sqlite3.connect(source)) as conn, conn:
        conn.execute("INSERT INTO assay(mark,region,code,cassette,length_um)"
                     " VALUES ('S-05','EAST','mica',?,6.25)", (BIG_POS,))
        conn.execute("UPDATE assay SET length_um=4.75 WHERE mark='S-01'")
    followup = call(base, '/preview', lookup)
    assert followup['summary']['counts'] == COUNTS_AFTER and followup['summary']['rows'] == 11
    assert followup['roots'] == ROOTS_ACTUAL, '试算总是重读源，前一次试算才确实在更新之前'

    # 4. JSON 入口封存：第二次读取，必须落在源更新之后（判据三的方向锚点）。
    cap1 = call(base, '/captures', lookup, 201)
    cap1_id = cap1['id']
    assert cap1['state'] == 'sealed'
    assert cap1['summary'] == {'counts': COUNTS_AFTER, 'rows': 11, 'witnesses': 17, 'root_occurrences': 5}

    # 判据一：根出现项 —— 顺序、实存拼写、重复根、大整数类型。
    assert cap1['roots'] == ROOTS_ACTUAL
    big_roots = [keys[0] for table, keys in cap1['roots'] if table == 'cassette']
    assert big_roots == [BIG_POS, BIG_NEG] and all(type(value) is int for value in big_roots)
    traced = call(base, f'/captures/{cap1_id}/trace', {'table': 'substrate', 'key': ['East', 'Mica']})
    assert traced['roots'] == [0, 1, 2] and len(traced['causes']) == 3 and traced['dependencies'] == []
    pos = call(base, f'/captures/{cap1_id}/trace', {'table': 'cassette', 'key': [BIG_POS]})
    assert pos['roots'] == [3] and len(pos['causes']) == 1
    neg = call(base, f'/captures/{cap1_id}/trace', {'table': 'cassette', 'key': [BIG_NEG]})
    assert neg['roots'] == [4] and neg['causes'] == []
    miss = call(base, f'/captures/{cap1_id}/trace', {'table': 'substrate', 'key': ['east', 'MICA']}, 404)
    assert miss['error'] == 'row_not_captured', '别名拼写不能伪造实存主键'

    # 浏览器跨入口读取 JSON 封存的捕获，页面文字与接口一致。
    _, cap1_page = show(base, f'/workbench/captures/{cap1_id}')
    assert '已封存' in cap1_page and page_counts(cap1_page) == COUNTS_AFTER
    assert page_roots(cap1_page, '源库实存根') == ROOTS_ACTUAL
    _, evidence = show(base, f'/workbench/captures/{cap1_id}/trace',
                       {'table': 'substrate', 'key': '["East","Mica"]'})
    assert '根序号</dt><dd>1, 2, 3</dd>' in evidence

    # 封存内容独立复核：直接读状态库，页面文字相同而封存错误在此失败。
    sealed1 = sealed_record(repo, cap1_id)
    assert sealed1['schema_digest'] == digest
    assert sealed1['result']['roots'] == ROOTS_ACTUAL
    rows1 = sealed1['result']['rows']
    assert rows1['assay']['["S-01"]'][4:] == [{'real': (4.75).hex()}, {'real': (9.5).hex()}]
    assert rows1['assay']['["S-02"]'][4:] == [{'real': (5.5).hex()}, {'real': (11.0).hex()}]
    assert rows1['assay']['["S-05"]'][3] == BIG_POS and type(rows1['assay']['["S-05"]'][3]) is int
    assert set(rows1['cassette']) == {'[11]', '[12]', f'[{BIG_POS}]', f'[{BIG_NEG}]'}
    assert rows1['cassette'][f'[{BIG_POS}]'] == [BIG_POS, None, 'huge-pos']
    assert rows1['substrate']['["East","Mica"]'][2] == {'blob': 'AAD/'}

    # 5. 第二次合法更新后走浏览器入口封存，两个入口的捕获互相可对比。
    with closing(sqlite3.connect(source)) as conn, conn:
        conn.execute("UPDATE assay SET length_um=8.0 WHERE mark='S-02'")
    cap2_url, cap2_page = show(base, recipe_url + '/capture', {})
    cap2_id = cap2_url.rsplit('/', 1)[1]
    assert '已封存' in cap2_page and page_counts(cap2_page) == COUNTS_AFTER
    assert page_roots(cap2_page, '源库实存根') == ROOTS_ACTUAL
    sealed2 = sealed_record(repo, cap2_id)
    assert sealed2['result']['rows']['assay']['["S-02"]'][4:] == [{'real': (8.0).hex()}, {'real': (16.0).hex()}]
    assert sealed2['result']['rows']['assay']['["S-01"]'][4] == {'real': (4.75).hex()}
    diff = call(base, '/compare', {'left': cap1_id, 'right': cap2_id})
    assert diff['changes']['assay'] == {'added': [], 'removed': [], 'modified': [['S-02']]}
    for table, changes in diff['changes'].items():
        if table != 'assay':
            assert changes == {'added': [], 'removed': [], 'modified': []}, (table, changes)
    _, compare_page = show(base, '/workbench/compare', {'left': cap1_id, 'right': cap2_id})
    assert '修改' in compare_page and '&quot;S-02&quot;' in compare_page

    # 6. JSON 入口构建并认证第一次捕获，浏览器入口下载（判据二的主战场）。
    built = call(base, f'/captures/{cap1_id}/build', {})
    assert built['state'] == 'built'
    certified = call(base, f'/captures/{cap1_id}/certify', {})
    certificate = certified['certificate']
    assert certified['state'] == 'certified' and certificate['counts'] == COUNTS_AFTER
    _, cap1_done = show(base, f'/workbench/captures/{cap1_id}')
    assert '已认证' in cap1_done and certificate['sha256'] in cap1_done
    _, headers, data1 = open_page(base, f'/workbench/captures/{cap1_id}/database')
    assert headers.get_content_type() == 'application/vnd.sqlite3'
    assert headers['ETag'] == '"' + sha256(data1).hexdigest() + '"' == '"' + certificate['sha256'] + '"'
    assert len(data1) == certificate['bytes']
    fixture1 = root / 'cross-entry-c1.sqlite'
    fixture1.write_bytes(data1)
    with closing(sqlite3.connect(fixture1.as_uri() + '?mode=ro', uri=True)) as conn:
        verify_first_fixture(conn)

    # 7. 浏览器入口构建并认证第二次捕获，JSON 入口下载并独立复核。
    _, built_page = show(base, f'/workbench/captures/{cap2_id}/build', {})
    assert '已构建' in built_page
    _, certified_page = show(base, f'/workbench/captures/{cap2_id}/certify', {})
    assert '已认证' in certified_page
    certificate2 = call(base, f'/captures/{cap2_id}')['certificate']
    assert certificate2['sha256'] in certified_page
    data2, etag2 = call(base, f'/captures/{cap2_id}/database')
    assert etag2 == sha256(data2).hexdigest() == certificate2['sha256']
    fixture2 = root / 'cross-entry-c2.sqlite'
    fixture2.write_bytes(data2)
    with closing(sqlite3.connect(fixture2.as_uri() + '?mode=ro', uri=True)) as conn:
        assert conn.execute('PRAGMA foreign_key_check').fetchall() == []
        row = conn.execute("SELECT length_um, typeof(length_um), doubled FROM assay WHERE mark='S-02'").fetchone()
        assert row == (8.0, 'real', 16.0), row
        assert conn.execute("SELECT length_um FROM assay WHERE mark='S-01'").fetchone() == (4.75,)
        serials = conn.execute('SELECT serial, typeof(serial) FROM cassette ORDER BY serial').fetchall()
        assert serials == [(BIG_NEG, 'integer'), (11, 'integer'), (12, 'integer'), (BIG_POS, 'integer')]

    print('跨入口验证通过：NOCASE 复合键、重复根、大整数键，试算先于源更新而封存在其后，'
          '根出现项、实际 SQLite 类型、封存记录与下载内容均独立相符')


def main():
    with TemporaryDirectory(prefix='keelsplice-cross-entry-') as directory:
        root = Path(directory)
        source = root / 'source.sqlite'
        seed(source)
        with closing(sqlite3.connect(source)) as conn, conn:
            conn.execute('INSERT INTO cassette VALUES (?, NULL, ?)', (BIG_POS, 'huge-pos'))
            conn.execute('INSERT INTO cassette VALUES (?, NULL, ?)', (BIG_NEG, 'huge-neg'))
        with create_server(('127.0.0.1', 0), root / 'state.sqlite', {'microscope': source}) as server:
            thread = Thread(target=server.serve_forever, kwargs={'poll_interval': 0.05})
            thread.start()
            try:
                run(f'http://127.0.0.1:{server.server_port}', source, root / 'state.sqlite', root)
            finally:
                server.shutdown()
                thread.join()


if __name__ == '__main__':
    main()

"""Exercise handover table links against replacement, rename, and FK-change updates."""

import base64
import json
import os
import re
import sqlite3
from contextlib import closing
from html import escape, unescape
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from urllib.parse import urlencode

from keelsplice.api import create_server

from .browser_walkthrough import html

SCHEMA = '''
CREATE TABLE site (name TEXT PRIMARY KEY, note TEXT);
CREATE TABLE ledger (
    id INTEGER PRIMARY KEY,
    site TEXT NOT NULL REFERENCES site(name),
    amount REAL NOT NULL
);
CREATE TABLE "weird ""quoted"" 表" (k TEXT PRIMARY KEY, ref INTEGER REFERENCES ledger(id));
CREATE TABLE "odd 100% & +=?#" (n INTEGER PRIMARY KEY, tag TEXT);
'''
ROWS_V1 = '''
INSERT INTO site VALUES ('north','a'),('south','b');
INSERT INTO ledger VALUES (1,'north',10.5),(2,'south',-3);
INSERT INTO "weird ""quoted"" 表" VALUES ('k1',1);
INSERT INTO "odd 100% & +=?#" VALUES (7,'x');
'''
ROWS_V2 = '''
INSERT INTO site VALUES ('east','c');
INSERT INTO ledger VALUES (5,'east',0.25);
INSERT INTO "weird ""quoted"" 表" VALUES ('k9',5);
INSERT INTO "odd 100% & +=?#" VALUES (8,'y');
'''
FK_CHANGED = '''
CREATE TABLE site (name TEXT PRIMARY KEY, note TEXT);
CREATE TABLE zone (name TEXT PRIMARY KEY);
CREATE TABLE ledger (
    id INTEGER PRIMARY KEY,
    site TEXT NOT NULL REFERENCES zone(name),
    amount REAL NOT NULL
);
CREATE TABLE "weird ""quoted"" 表" (k TEXT PRIMARY KEY, ref INTEGER REFERENCES ledger(id));
CREATE TABLE "odd 100% & +=?#" (n INTEGER PRIMARY KEY, tag TEXT);
INSERT INTO site VALUES ('north','a');
INSERT INTO zone VALUES ('north');
INSERT INTO ledger VALUES (1,'north',10.5);
'''
BETA = 'CREATE TABLE ledger (uid TEXT PRIMARY KEY, note TEXT);'
WEIRD = 'weird "quoted" 表'
ODD = 'odd 100% & +=?#'


def seed(file, *scripts):
    with closing(sqlite3.connect(file)) as conn:
        conn.executescript(''.join(scripts))


def handoff_link(page):
    return unescape(re.search(r'id="handoff-link"[^>]*value="([^"]+)"', page)[1])


def digest_of(page):
    return re.search(r'<dt>模式摘要</dt><dd><code>([0-9a-f]{64})</code></dd>', page)[1]


def retoken(link, **patch):
    head, _, token = link.rpartition('handoff=')
    envelope = json.loads(base64.urlsafe_b64decode(token + '=' * (-len(token) % 4)))
    envelope.update(patch)
    packed = json.dumps(envelope, sort_keys=True, separators=(',', ':')).encode('ascii')
    return head + 'handoff=' + base64.urlsafe_b64encode(packed).decode('ascii').rstrip('=')


def run(base, alpha, beta, staging):
    def restore_alpha():
        fresh = staging / 'restore.sqlite'
        seed(fresh, SCHEMA, ROWS_V1)
        os.replace(fresh, alpha)

    _, page = html(base, '/workbench?' + urlencode({'source': 'alpha', 'table': 'ledger'}))
    assert '交接核对' not in page and '交接链接' in page
    ledger_link = handoff_link(page)
    assert ledger_link.startswith('/workbench?source=alpha&table=ledger&handoff=')

    _, page = html(base, ledger_link)
    assert 'class="handoff match"' in page and '可沿用原确认' in page
    first_digest = digest_of(page)

    replacement = staging / 'same-structure.sqlite'
    seed(replacement, SCHEMA, ROWS_V2)
    os.replace(replacement, alpha)
    with closing(sqlite3.connect(alpha)) as conn:
        assert conn.execute('SELECT count(*) FROM ledger').fetchone() == (1,)
    _, page = html(base, ledger_link)
    assert '可沿用原确认' in page and digest_of(page) == first_digest

    seed(staging / 'fk-changed.sqlite', FK_CHANGED)
    os.replace(staging / 'fk-changed.sqlite', alpha)
    _, page = html(base, ledger_link)
    assert 'class="handoff review" open' in page and '需要接手人确认' in page
    assert '外键缺失' in page and '外键新增' in page and 'zone' in page
    assert '可沿用原确认' not in page

    restore_alpha()
    with closing(sqlite3.connect(alpha)) as conn, conn:
        conn.execute('ALTER TABLE ledger RENAME TO ledger_main')
    _, page = html(base, ledger_link, expected=404)
    assert '需要接手人确认' in page and 'ledger_main' in page and '重命名' in page
    restore_alpha()
    _, page = html(base, ledger_link)
    assert '可沿用原确认' in page

    weird_links = {}
    for name in (WEIRD, ODD):
        _, page = html(base, '/workbench?' + urlencode({'source': 'alpha', 'table': name}))
        assert f'<h1>{escape(name)}</h1>' in page
        weird_links[name] = handoff_link(page)
        _, page = html(base, weird_links[name])
        assert '可沿用原确认' in page and f'<h1>{escape(name)}</h1>' in page
    with closing(sqlite3.connect(alpha)) as conn, conn:
        conn.execute('ALTER TABLE "weird ""quoted"" 表" RENAME TO "weird ""quoted"" 表 v2"')
    _, page = html(base, weird_links[WEIRD], expected=404)
    assert '需要接手人确认' in page and escape('weird "quoted" 表 v2') in page
    _, page = html(base, weird_links[ODD])
    assert '可沿用原确认' in page and f'<h1>{escape(ODD)}</h1>' in page
    restore_alpha()
    _, page = html(base, weird_links[WEIRD])
    assert '可沿用原确认' in page

    _, beta_page = html(base, '/workbench?' + urlencode({'source': 'beta', 'table': 'ledger'}))
    beta_link = handoff_link(beta_page)
    assert beta_link != ledger_link
    _, page = html(base, beta_link)
    assert '可沿用原确认' in page
    _, page = html(base, ledger_link.replace('source=alpha', 'source=beta'))
    assert 'class="handoff unknown" open' in page and '不一致' in page
    _, page = html(base, ledger_link.replace('table=ledger', 'table=site'))
    assert 'class="handoff unknown" open' in page and '不一致' in page

    alpha.write_bytes(b'not a sqlite database at all')
    _, page = html(base, ledger_link, expected=422)
    assert '无法确定' in page and '原结构依据' in page and 'CREATE TABLE ledger' in page
    restore_alpha()
    _, page = html(base, ledger_link)
    assert '可沿用原确认' in page

    ghost = retoken(ledger_link, source='ghost').replace('source=alpha', 'source=ghost')
    _, page = html(base, ghost, expected=404)
    assert '未注册' in page and '无法确定' in page
    _, page = html(base, ledger_link[:-24])
    assert '无法确定' in page and '无法解析' in page
    print('交接链接检查通过：同结构替换、外键变更、表重命名、双别名同名表、特殊字符表名、源不可读、链接损坏')


def main():
    with TemporaryDirectory(prefix='keelsplice-handoff-') as directory:
        root = Path(directory)
        staging = root / 'staging'
        staging.mkdir()
        alpha, beta = root / 'alpha.sqlite', root / 'beta.sqlite'
        seed(alpha, SCHEMA, ROWS_V1)
        seed(beta, BETA)
        with create_server(('127.0.0.1', 0), root / 'state.sqlite',
                           {'alpha': alpha, 'beta': beta}) as server:
            thread = Thread(target=server.serve_forever, kwargs={'poll_interval': 0.05})
            thread.start()
            try:
                run(f'http://127.0.0.1:{server.server_port}', alpha, beta, staging)
            finally:
                server.shutdown()
                thread.join()


if __name__ == '__main__':
    main()

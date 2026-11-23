"""Exercise the production workbench with the disposable microscope database."""

import argparse
import hashlib
import html as html_lib
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


def rebuild_foreign_keys(source):
    """A colleague recreates the assay foreign keys while the draft stays open.

    A protocol table is added and its reference is declared as a table-level
    foreign key ahead of the composite substrate reference.  SQLite numbers
    table-level foreign keys before column-level ones, so the new protocol
    reference takes id 0 and the substrate reference the draft knew as id 0 is
    renumbered to id 1 — while id 0 now means something entirely different.
    Same id number, unknowable old meaning: the review must not guess.
    """
    with closing(sqlite3.connect(source)) as conn, conn:
        rows = conn.execute('SELECT mark, region, code, cassette, length_um FROM assay').fetchall()
        conn.execute('CREATE TABLE IF NOT EXISTS protocol (proto_id INTEGER PRIMARY KEY, label TEXT NOT NULL)')
        conn.execute("INSERT OR IGNORE INTO protocol VALUES (1, 'default')")
        conn.execute('DROP TABLE assay')
        conn.execute('''CREATE TABLE assay (
            mark TEXT PRIMARY KEY NOT NULL,
            region TEXT,
            code TEXT,
            cassette INTEGER NOT NULL REFERENCES cassette,
            proto INTEGER NOT NULL DEFAULT 1,
            length_um REAL NOT NULL,
            doubled REAL GENERATED ALWAYS AS (length_um * 2) STORED,
            FOREIGN KEY (region, code) REFERENCES substrate,
            FOREIGN KEY (proto) REFERENCES protocol(proto_id)
        )''')
        conn.executemany(
            'INSERT INTO assay(mark,region,code,cassette,length_um,proto) VALUES (?,?,?,?,?,1)', rows)
        conn.execute('CREATE INDEX assay_substrate_lookup ON assay(region,code) WHERE code IS NOT NULL')
    with closing(sqlite3.connect(source)) as conn:
        conn.row_factory = sqlite3.Row
        ids = {row['id']: row['table'] for row in conn.execute('PRAGMA foreign_key_list(assay)')}
    assert ids == {0: 'protocol', 1: 'substrate', 2: 'cassette'}, ids


def drift_review(base, source, editor_url, form, digest, draft_evidence):
    rebuild_foreign_keys(source)
    # Evidence is mandatory for drift recovery; mismatched or altered evidence is refused.
    assert '目录证据' in html(base, editor_url,
                             {k: v for k, v in form.items() if k != 'evidence'}, expected=400)[1]
    assert '不一致' in html(base, editor_url, form | {'digest': 'stale'}, expected=400)[1]
    tampered = dict(form, evidence=draft_evidence.replace('"child_cols":', '"child_mapping":', 1))
    assert '不正确' in html(base, editor_url, tampered, expected=400)[1]
    # The stale draft is rejected with the item-by-item review page, not a dead end.
    _, review = html(base, editor_url, form, expected=409)
    new_digest = re.search(r'name="new_digest" value="([0-9a-f]{64})"', review)[1]
    assert new_digest != digest
    review_form = {k: form[k] for k in ('source', 'name', 'row_limit', 'roots', 'evidence')}
    review_form['digest'] = digest
    review_form['new_digest'] = new_digest
    review_form['edge'] = form['edge']
    # The surviving substrate root is offered for manual confirmation, while the
    # old edge id 0 is undecidable: the new catalog reuses 0 for another meaning.
    assert '可人工确认' in review and 'name="keep_root" value="0"' in review
    assert '含义无法判定' in review and '不能代为选择' in review
    assert 'name="keep_edge"' not in review
    assert 'checked' not in review.split('逐项核对根记录')[1]
    # A draft edge whose parent and column mapping survive the rebuild is offered
    # for manual confirmation, but it is never preselected on the user's behalf.
    edge_form = dict(form, edge=['0:assay', '0:frame'])
    _, edge_review = html(base, editor_url, edge_form, expected=409)
    assert 'name="keep_edge" value="0:frame"' in edge_review
    assert 'checked' not in edge_review.split('逐项核对反向关系')[1]
    # The undecidable assay edge still has no confirmable control next to frame.
    assert edge_review.count('含义无法判定') >= 1
    # The operator can explicitly keep only the still-meaningful frame edge.
    keep_edge = review_form | {'edge': ['0:assay', '0:frame'],
                               'keep_root': ['0'], 'keep_edge': ['0:frame'], 'confirmed': '1'}
    edge_target, edge_saved = html(base, '/workbench/recipes/reconcile', keep_edge)
    assert '投用中' in edge_saved
    _, edge_detail = html(base, edge_target)
    assert 'frame' in edge_detail and '外键 0' in edge_detail
    # Submitting without the explicit confirmation is refused.
    _, refused = html(base, '/workbench/recipes/reconcile',
                      review_form | {'keep_root': ['0']}, expected=400)
    assert '最终提交确认' in refused
    # Confirming the undecidable old number is refused rather than remapped to
    # the new edge that happens to carry the same number 0.
    sneaky = review_form | {'keep_root': ['0'], 'keep_edge': ['0:assay'], 'confirmed': '1'}
    assert '无法判定的旧编号' in html(base, '/workbench/recipes/reconcile', sneaky, expected=400)[1]
    # A digest that changes again during review forces another review pass.
    stale_review = review_form | {'new_digest': '0' * 64, 'keep_root': ['0'], 'confirmed': '1'}
    assert '再次变化' in html(base, '/workbench/recipes/reconcile', stale_review, expected=409)[1]
    # Explicit commit binds the new version to the current schema version only,
    # and the undecidable edge is dropped rather than silently re-interpreted.
    commit = review_form | {'keep_root': ['0'], 'confirmed': '1'}
    target, saved = html(base, '/workbench/recipes/reconcile', commit)
    assert target.startswith('/workbench/recipes/browser-swatch/') and '投用中' in saved
    _, detail = html(base, target)
    assert new_digest in detail and 'substrate' in detail and '未选择反向关系' in detail
    _, preview = html(base, target + '/preview', {})
    assert 'East' in preview and 'Mica' in preview
    # Reopen the editor against the current catalog so the remaining baseline
    # flow binds to the schema version that now exists in the source database.
    _, refreshed = html(base, editor_url)
    current_digest = re.search(r'name="digest" value="([0-9a-f]{64})"', refreshed)[1]
    current_evidence = html_lib.unescape(re.search(r'name="evidence" value="([^"]+)"', refreshed)[1])
    return current_digest, current_evidence


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
    draft_evidence = html_lib.unescape(re.search(r'name="evidence" value="([^"]+)"', editor)[1])
    form = {'source': 'microscope', 'name': 'browser-swatch', 'digest': digest, 'evidence': draft_evidence,
            'row_limit': '20', 'edge': ['0:assay'],
            'roots': '[{"table":"substrate","key":["east","MICA"]}]'}
    digest, draft_evidence = drift_review(base, source, editor_url, form, digest, draft_evidence)
    # The substrate composite edge is now numbered 1 in the rebuilt catalog.
    form.update(digest=digest, evidence=draft_evidence, edge=['1:assay'])
    assert '本机工作台' in html(base, editor_url, form, expected=403, origin='http://localhost:1')[1]
    _, error = html(base, editor_url, form | {'roots': 'bad'}, expected=400)
    assert '有效的 JSON' in error and 'browser-swatch' in error
    version, page = html(base, editor_url, form)
    assert version.endswith('/3') and '投用中' in page
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

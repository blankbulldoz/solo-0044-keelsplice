"""Exercise identity rendering with quoted names, BLOB keys and twin edges.

The sample deliberately contains table and index names with embedded double
quotes, a composite primary key of NOCASE text plus exact BLOB, two foreign keys
on one child table that point at the same parent columns with different numbers,
and values long enough to need a shortened gloss.

    python3 -m examples.quoted_walkthrough

The walkthrough checks that the original routes, JSON evidence, form
round-trips and root positions agree, and that each page distinguishes human
text from an exact key from a reversible directed edge reference.
"""

import base64
import hashlib
import html as htmllib
import json
import re
import sqlite3
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

from keelsplice.api import create_server

SAMPLE = Path(__file__).with_name('quoted.sql')
TABLE = 'slab "core"'
PAIR = 'link "pair"'
MIRROR = 'mirror "copy"'
READ = 'read "out"'
LONG_REGION = 'north-east corridor with a very long readable label'
LONG_TOKEN = 'AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8='


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


OPENER = build_opener(NoRedirect)


def seed(path):
    with closing(sqlite3.connect(path)) as conn:
        conn.executescript(SAMPLE.read_text(encoding='utf-8'))


def call(base, route, body=None, form=None, status=200):
    headers = {}
    if form is not None:
        data = urlencode(form, doseq=True).encode('ascii')
        headers['Origin'] = base
    elif body is not None:
        data = json.dumps(body).encode('utf-8')
        headers['Content-Type'] = 'application/json'
    else:
        data = None
    try:
        response = OPENER.open(Request(base + route, data, headers), timeout=10)
    except HTTPError as exc:
        response = exc
    with response:
        payload = response.read()
        assert response.status == status, (route, response.status, payload[:600])
        kind = response.headers.get_content_type()
        location = response.headers.get('Location')
        etag = response.headers.get('ETag')
    if status == 303:
        return location
    if kind == 'application/json':
        return json.loads(payload)
    if kind == 'text/html':
        return payload.decode('utf-8')
    return payload, etag


def visible(markup):
    return re.sub(r'\s+', ' ', htmllib.unescape(re.sub(r'<[^>]+>', ' ', markup))).strip()


def identity_fragments(markup, kind):
    """Visible text of every identity fragment of one kind, in document order."""
    found = []
    for attributes, body in re.findall(
            r'<[a-z]+[^>]*\bclass="[^"]*\b(?:identity\s+)?(?:exact|gloss|edge|identifier)\b[^"]*"'
            r'[^>]*\bdata-kind="([a-z]+)"[^>]*>(.*?)</[a-z]+>', markup, re.S):
        if attributes == kind:
            found.append(visible(body))
    return found


def attributes(markup, pattern):
    return [{key: htmllib.unescape(value)
             for key, value in re.findall(r'data-([a-z-]+)="([^"]*)"', match)}
            for match in re.findall(pattern, markup, re.S)]


def trace_forms(markup):
    """Every trace form's ordered hidden (table, key) pair, decoded once."""
    forms = []
    for body in re.findall(r'<form[^>]*action="[^"]*/trace"[^>]*>(.*?)</form>', markup, re.S):
        fields = [(htmllib.unescape(name), htmllib.unescape(value))
                  for name, value in re.findall(
                      r'<input type="hidden" name="([^"]*)" value="([^"]*)"', body)]
        if [name for name, _ in fields] == ['table', 'key']:
            forms.append(fields)
    return forms


def roots_page(markup):
    return [int(item) for item in re.search(r'data-root-positions="([^"]*)"', markup)[1].split(',')
            if item]


def walkthrough(base, source, root):
    catalog = call(base, '/sources/quoted/catalog')
    tables = catalog['schema']['tables']
    assert set(tables) == {TABLE, PAIR, MIRROR, READ}
    assert tables[TABLE]['pk'] == ['region', 'token']
    assert [col['name'] for col in tables[MIRROR]['columns']][:2] == ['region', 'token']

    # Two foreign keys on one child table share parent and parent columns; only
    # the SQLite number distinguishes them, and both are reversible references.
    pair_edges = sorted((edge['id'], tuple(edge['child_cols']))
                        for edge in catalog['schema']['edges'] if edge['child'] == PAIR)
    assert pair_edges == [(0, ('right_region', 'right_token')),
                          (1, ('left_region', 'left_token'))]
    for edge in catalog['schema']['edges']:
        assert edge['parent'] == TABLE and edge['parent_cols'] == ['region', 'token']

    recipe_body = {
        'source': 'quoted',
        'roots': [
            {'table': 'SLAB "CORE"', 'key': ['east', {'blob': 'AP8='}]},
            {'table': TABLE, 'key': ['WEST', {'blob': '/gA='}]},
            {'table': TABLE, 'key': [LONG_REGION, {'blob': LONG_TOKEN}]},
        ],
        'follow_children': [{'child': PAIR, 'id': 0}, {'child': PAIR, 'id': 1},
                            {'child': MIRROR, 'id': 0}, {'child': READ, 'id': 0}],
        'row_limit': 100,
    }
    version = call(base, '/recipes/quote/versions', recipe_body, status=201)
    assert [item['table'] for item in version['recipe']['roots']] == [TABLE] * 3
    assert version['recipe']['roots'][0]['key'] == ['east', {'blob': 'AP8='}]

    preview = call(base, '/preview', {'name': 'quote', 'version': 1})
    # NOCASE lookup substitutes the stored spelling while keeping input order.
    assert preview['roots'] == [
        [TABLE, ['East', {'blob': 'AP8='}]],
        [TABLE, ['West', {'blob': '/gA='}]],
        [TABLE, [LONG_REGION, {'blob': LONG_TOKEN}]],
    ]
    assert preview['summary']['counts'] == {PAIR: 3, MIRROR: 2, READ: 2, TABLE: 4}

    capture = call(base, '/captures', {'name': 'quote', 'version': 1}, status=201)
    endpoint = '/captures/' + capture['id']
    assert capture['roots'] == preview['roots']

    long_trace = call(base, endpoint + '/trace', {'table': TABLE, 'key': preview['roots'][2][1]})
    assert long_trace['roots'] == [2] and long_trace['row'] == preview['roots'][2]
    assert call(base, endpoint + '/trace',
                {'table': TABLE, 'key': ['east', {'blob': 'AP8='}]}, status=404)
    twin_zero = call(base, endpoint + '/trace', {'table': PAIR, 'key': [1]})
    twin_one = call(base, endpoint + '/trace', {'table': PAIR, 'key': [3]})
    assert {cause['edge'][1] for cause in twin_zero['causes']} == {0, 1}
    assert all(cause['edge'] == [PAIR, 0] or cause['edge'] == [PAIR, 1]
               for cause in twin_zero['causes'] + twin_one['causes'])

    # ---- the browser form round-trip over the same exact keys -------------
    editor = call(base, '/workbench/recipes/new?source=quoted')
    digest = re.search(r'name="digest" value="([0-9a-f]{64})"', editor)[1]
    assert digest == catalog['schema_digest']
    # The directory renders each quoted table name as an identifier fragment.
    assert visible(re.search(r'<h1>.*?</h1>', editor, re.S).group(0)) == '登记配方'
    assert set(identity_fragments(editor, 'identifier')) >= {TABLE, PAIR, MIRROR, READ}
    assert not identity_fragments(editor, 'key') and not identity_fragments(editor, 'text')

    form = {
        'source': 'quoted', 'name': 'quote-browser', 'digest': digest, 'row_limit': '100',
        'roots': json.dumps(recipe_body['roots'], ensure_ascii=False),
        'edge': [f'1:{PAIR}', f'0:{PAIR}', f'0:{MIRROR}', f'0:{READ}'],
    }
    recipe_route = call(base, '/workbench/recipes/new', form=form, status=303)
    assert recipe_route == '/workbench/recipes/quote-browser/1'
    capture_route = call(base, recipe_route + '/capture', form={}, status=303)

    detail = call(base, capture_route)
    roots = trace_forms(detail)
    # Hidden fields carry the exact typed key, not the visible gloss.
    assert roots == [[('table', table), ('key', json.dumps(key, separators=(',', ':')))]
                     for table, key in capture['roots']]
    exact_keys = identity_fragments(detail, 'key')
    assert exact_keys == [json.dumps(key, separators=(',', ':')) for _, key in capture['roots']]
    glosses = identity_fragments(detail, 'text')
    assert len(glosses) == len(capture['roots'])
    assert LONG_REGION not in glosses[2] and '…' in glosses[2]
    assert '共 51 字符' in visible(detail) and '二进制 32 字节' in visible(detail)

    for index, fields in enumerate(roots):
        evidence = call(base, capture_route + '/trace', form=dict(fields))
        # Root positions are reported zero-based in markup, one-based in words.
        expected_positions = [2] if index == 2 else [index]
        assert roots_page(evidence) == expected_positions
        assert visible(re.search(r'<div><dt>表</dt>.*?</div>', evidence, re.S).group(0)) \
            == f'表 {TABLE}'
        # The traced row's own key is the first identity key on the page; any
        # further keys belong to the related rows offered for navigation.
        assert identity_fragments(evidence, 'key')[0] == fields[1][1]
        if index == 2:
            assert identity_fragments(evidence, 'text')[0] == glosses[2]
            assert 'data-kind="text"' in evidence and 'data-truncated="yes"' in evidence

    # Every edge shown on an evidence page is a directed, reversible reference.
    for fields in roots:
        evidence = call(base, capture_route + '/trace', form=dict(fields))
        edges = attributes(evidence, r'<code class="edge"([^>]*)>')
        for edge in edges:
            assert edge['child'] in {PAIR, MIRROR, READ}
            assert edge['number'].isdigit()
            assert edge['direction'] in {'parent', 'child', 'self'}
            assert edge['reverse-direction'] in {'parent', 'child', 'self'}
    assert any(edge['child'] == PAIR and edge['number'] == '0'
               for fields in roots
               for edge in attributes(call(base, capture_route + '/trace', form=dict(fields)),
                                      r'<code class="edge"([^>]*)>'))
    assert any(edge['child'] == PAIR and edge['number'] == '1'
               for fields in roots
               for edge in attributes(call(base, capture_route + '/trace', form=dict(fields)),
                                      r'<code class="edge"([^>]*)>'))

    # A human gloss is not an address: submitting it is rejected.
    call(base, capture_route + '/trace',
         form={'table': TABLE, 'key': identity_fragments(detail, 'text')[2]}, status=400)

    # A quoted or dotted alias spelling is not silently repaired either.
    call(base, capture_route + '/trace', form={'table': 'slab', 'key': roots[0][1][1]}, status=422)

    # ---- build, certify, hand off the same rows --------------------------
    call(base, '/captures/' + capture['id'] + '/build', {}, status=200)
    certificate = call(base, '/captures/' + capture['id'] + '/certify', {}, status=200)['certificate']
    body, etag = call(base, endpoint + '/database')
    assert etag == '"' + hashlib.sha256(body).hexdigest() + '"'
    assert etag.strip('"') == certificate['sha256']
    fixture = root / 'quoted-handoff.sqlite'
    fixture.write_bytes(body)
    with closing(sqlite3.connect(fixture.as_uri() + '?mode=ro', uri=True)) as conn:
        assert conn.execute('PRAGMA foreign_key_check').fetchall() == []
        assert conn.execute(f'SELECT count(*) FROM {quote(TABLE)}').fetchone()[0] == 4
        row = conn.execute(f'SELECT token FROM {quote(TABLE)} WHERE region=?', (LONG_REGION,)).fetchone()
        assert bytes(row[0]) == base64.b64decode(LONG_TOKEN)

    # ---- the same facts survive a source update --------------------------
    with closing(sqlite3.connect(source)) as conn, conn:
        conn.execute(f'UPDATE {quote(TABLE)} SET caption=? WHERE region=?', ('changed', 'East'))
    newer = call(base, '/captures', {'name': 'quote', 'version': 1}, status=201)
    difference = call(base, '/compare', {'left': capture['id'], 'right': newer['id']})
    # The caption changed but the typed key did not: one exact BLOB-bearing key
    # is reported as modified, and every other table is untouched.
    assert difference['changes'][TABLE] == {
        'added': [], 'removed': [], 'modified': [['East', {'blob': 'AP8='}]]}
    assert difference['changes'][PAIR] == {'added': [], 'removed': [], 'modified': []}
    page = call(base, '/workbench/compare', form={'left': capture['id'], 'right': newer['id']})
    assert identity_fragments(page, 'key')[0] == '["East",{"blob":"AP8="}]'
    assert identity_fragments(page, 'text')[0] == "['East', 「二进制 2 字节」]"
    print('带引号表名、复合 BLOB 键与同名双外键检查通过：'
          '原路由、表单回传、根位置与 JSON 证据一致，人读值、精确键与边引用可区分')


def quote(identifier):
    return '"' + identifier.replace('"', '""') + '"'


def main():
    with TemporaryDirectory(prefix='keelsplice-quoted-') as directory:
        root = Path(directory)
        source = root / 'quoted.sqlite'
        seed(source)
        with create_server(('127.0.0.1', 0), root / 'state.sqlite', {'quoted': source}) as server:
            thread = Thread(target=server.serve_forever)
            thread.start()
            try:
                walkthrough(f'http://127.0.0.1:{server.server_port}', source, root)
            finally:
                server.shutdown()
                thread.join()


if __name__ == '__main__':
    main()

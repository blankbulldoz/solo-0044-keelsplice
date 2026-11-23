"""Browser end-to-end checks for the handoff revisit workbench pages."""

import json
import sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from keelsplice.api import create_server
from keelsplice.revisit import materialize, short

from .walkthrough import seed


def open_page(base, path, payload=None, expected=200):
    data = None if payload is None else urlencode(payload, doseq=True).encode('ascii')
    headers = {} if data is None else {'Origin': base}
    request = Request(base + path, data=data, headers=headers)
    try:
        response = urlopen(request, timeout=10)
    except HTTPError as exc:
        response = exc
    with response:
        assert response.status == expected, (path, response.status, response.read()[:300])
        return response.read().decode('utf-8')


def certified_capture(base):
    catalog = json.loads(urlopen(base + '/sources/microscope/catalog', timeout=10).read())
    edge = next(e for e in catalog['schema']['edges'] if e['child'] == 'assay' and e['parent'] == 'substrate')
    body = {'source': 'microscope', 'roots': [{'table': 'substrate', 'key': ['east', 'MICA']}],
            'follow_children': [{'child': 'assay', 'id': edge['id']}], 'row_limit': 20}
    req = Request(base + '/recipes/revisit-demo/versions', json.dumps(body).encode(),
                  {'Content-Type': 'application/json'})
    json.loads(urlopen(req, timeout=10).read())
    req = Request(base + '/captures', json.dumps({'name': 'revisit-demo', 'version': 1}).encode(),
                  {'Content-Type': 'application/json'})
    capture = json.loads(urlopen(req, timeout=10).read())
    cid = capture['id']
    for route in ('build', 'certify'):
        req = Request(base + f'/captures/{cid}/{route}', b'{}', {'Content-Type': 'application/json'})
        json.loads(urlopen(req, timeout=10).read())
    certified = json.loads(urlopen(base + f'/captures/{cid}', timeout=10).read())
    delivered = urlopen(base + f'/captures/{cid}/database', timeout=10).read()
    return cid, certified['certificate'], delivered


def submit_revisit(base, cid, package, note, expected=200):
    bundle = json.dumps({'files': package, 'note': note}, ensure_ascii=False)
    return open_page(base, '/workbench/revisits',
                     {'capture': cid, 'bundle': bundle, 'note': note}, expected)


def run(base, root):
    cid, certificate, delivered = certified_capture(base)
    fixture = root / 'receiver.sqlite'
    fixture.write_bytes(delivered)

    # Navigation and the empty index render.
    index = open_page(base, '/workbench/revisits')
    assert '交接回访' in index and '尚无回访记录' in index and cid[:12] in index

    # Pristine receiver copy -> fully equivalent report.
    pristine = materialize(fixture, {'sha256': certificate['sha256'],
                                     'capture_digest': certificate['capture_digest']}, note='原样保留')
    page = submit_revisit(base, cid, pristine, '原样保留')
    assert '可证实的逻辑等价' in page and '可定位的结构或值变化（0）' in page
    assert 'manifest.json' in page and 'tables.json' in page
    rid = page.split('<code>')[1].split('</code>')[0]

    # Material file page and detail page share the same revisit id.
    detail = open_page(base, f'/workbench/revisits/{rid}')
    assert rid in detail and '管理员独立复查' in detail
    material = open_page(base, f'/workbench/revisits/{rid}/materials/schema.json')
    assert 'schema_digest' in material and '不含行原文' in material

    # A changed BLOB/value on the receiver side locates the exact row.
    changed = root / 'changed.sqlite'
    changed.write_bytes(delivered)
    with sqlite3.connect(changed) as conn:
        conn.execute("UPDATE substrate SET texture=x'112233' WHERE region='East' AND code='Mica'")
        conn.commit()
    package = materialize(changed, {'sha256': certificate['sha256'],
                                    'capture_digest': certificate['capture_digest']}, note='BLOB 变更')
    page = submit_revisit(base, cid, package, 'BLOB 变更')
    assert '可定位的结构或值变化（' in page and 'East' in page and 'substrate' in page
    rid2 = page.split('<code>')[1].split('</code>')[0]
    row_subject = next(line for line in page.split('value="') if line.startswith('table:substrate:row:')).split('"')[0]
    recheck = open_page(base, f'/workbench/revisits/{rid2}/recheck', {'subject': row_subject})
    assert '复查不成立' in recheck and '封存行重算' in recheck

    # A tampered package is rejected with a visible Chinese notice.
    tampered = json.loads(json.dumps(pristine))
    tampered['tables.json']['tables']['assay']['count'] = 4242
    rejected = submit_revisit(base, cid, tampered, '篡改', expected=409)
    assert 'manifest' in rejected and '材料不可信' in rejected

    # A foreign anchor is rejected even with a self-consistent manifest.
    foreign = json.loads(json.dumps(pristine))
    foreign['manifest.json']['anchor']['sha256'] = 'a' * 64
    foreign['manifest.json']['files'] = {
        name: short(foreign[name]) for name in ('file.json', 'schema.json', 'tables.json')}
    rejected = submit_revisit(base, cid, foreign, '外来材料', expected=409)
    assert '锚点' in rejected

    # The index lists both revisits.
    listing = open_page(base, '/workbench/revisits')
    assert '原样保留' in listing and 'BLOB 变更' in listing
    print('浏览器回访检查通过：登记、三类结论、材料追溯、独立复查、篡改与锚点拒绝')


def main():
    with TemporaryDirectory(prefix='keelsplice-revisit-browser-') as directory:
        root = Path(directory)
        source = root / 'source.sqlite'
        seed(source)
        with create_server(('127.0.0.1', 0), root / 'state.sqlite', {'microscope': source}) as server:
            thread = Thread(target=server.serve_forever)
            thread.start()
            try:
                run(f'http://127.0.0.1:{server.server_port}', root)
            finally:
                server.shutdown()
                thread.join()


if __name__ == '__main__':
    main()

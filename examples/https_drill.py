"""Rehearse the proxied deployment end to end against disposable files.

Covers the four drills from the deployment brief:
1. HTTPS page form identity (browser origin) drives the workbench.
2. The relayed position is attributed to the local TLS proxy, not the loopback.
3. Stopping and restarting the proxy leaves the state database's captures intact.
4. The downloaded bytes are checked against the certificate digest.
"""

import argparse
import hashlib
import http.client
import os
import re
import secrets
import socket
import sqlite3
import ssl
import subprocess
import sys
import time
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlencode

from keelsplice.web import access

HOST = 'localhost'
PUBLIC = 'https://localhost'
TOKEN_ENV = 'KEELSPLICE_DRILL_TOKEN'


def free_port():
    with closing(socket.socket()) as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def wait_ready(port, context, path='/workbench'):
    for _ in range(200):
        try:
            status, _, body = fetch(port, path, context=context)
            if status < 500:
                return
        except (ConnectionError, OSError):
            pass
        time.sleep(0.03)
    raise SystemExit('TLS proxy did not become ready')


def fetch(port, path, payload=None, context=None, origin=None, headers=None):
    connection = http.client.HTTPSConnection(HOST, port, context=context, timeout=5) if context \
        else http.client.HTTPConnection('127.0.0.1', port, timeout=5)
    data = None if payload is None else urlencode(payload, doseq=True).encode('ascii')
    request = {'Content-Type': 'application/x-www-form-urlencoded'} if data is not None else {}
    if origin is not None:
        request['Origin'] = origin
    request.update(headers or {})
    try:
        connection.request('GET' if data is None else 'POST', path, body=data, headers=request)
        answer = connection.getresponse()
        return answer.status, dict(answer.getheaders()), answer.read()
    finally:
        connection.close()


def start(*command, env=None):
    return subprocess.Popen(command, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)


def stop(process):
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def page(body):
    return body.decode('utf-8')


def run(repository, source, port, context, backend_port, proxy_state):
    # 1. HTTPS page renders, and the identity shown is the proxy position.
    status, headers, body = fetch(port, '/workbench', context=context)
    assert status == 200, (status, body[:200])
    html = page(body)
    assert 'HTTPS · 前置代理' in html and 'transport-https-proxy' in html
    status, _, editor = fetch(port, '/workbench/recipes/new', context=context)
    assert status == 200
    digest = re.search(r'name="digest" value="([0-9a-f]{64})"', page(editor))
    assert digest, 'catalog digest missing from HTTPS editor page'
    form = {'source': 'microscope', 'name': 'https-swatch', 'digest': digest[1],
            'row_limit': '20', 'edge': ['0:assay'],
            'roots': '[{"table":"substrate","key":["east","MICA"]}]'}
    status, headers, body = fetch(port, '/workbench/recipes/new', form,
                                  context=context, origin=PUBLIC)
    assert status == 303 and headers.get('Location', '').endswith('/workbench/recipes/https-swatch/1'), \
        (status, headers, body[:300])
    status, _, body = fetch(port, headers['Location'], context=context)
    assert status == 200 and '版本 1' in page(body) and '投用中' in page(body)

    # The loopback JSON client still uses the original entry point unchanged.
    status, _, catalog = fetch(backend_port, '/sources/microscope/catalog')
    assert status == 200 and b'substrate' in catalog
    status, _, _ = fetch(port, '/sources/microscope/catalog', context=context)
    assert status == 404, 'public edge must publish only the workbench'

    # 2. Proxy position: direct loopback is labelled differently, and a forged
    #    proxy claim does not fool the backend.
    status, _, direct = fetch(backend_port, '/workbench')
    assert status == 200 and '本机回环' in page(direct) and 'HTTPS · 前置代理' not in page(direct)
    forged = {access.PROXY_HEADER: 'x' * 24, 'X-Forwarded-Proto': 'https',
              'X-Forwarded-Host': 'localhost'}
    status, _, body = fetch(backend_port, '/workbench', headers=forged)
    assert status == 403, status
    assert '有效凭据' in page(body), page(body)[-600:]

    # Form posts claiming the wrong origin are rejected before they reach state.
    status, _, body = fetch(port, '/workbench/recipes/new',
                            form | {'name': 'wrong-origin'}, context=context,
                            origin='https://localhost:1')
    assert status == 403 and '来源' in page(body)

    # 3. Seal and then take the proxy down and back up: the persisted capture
    #    survives, and the retired recipe still exposes its history.
    status, headers, body = fetch(port, '/workbench/recipes/https-swatch/1/capture', {},
                                  context=context, origin=PUBLIC)
    assert status == 303 and '/workbench/captures/' in headers.get('Location', ''), (status, headers)
    capture_id = headers['Location'].rsplit('/', 1)[1]
    stop(proxy_state['process'])
    with closing(sqlite3.connect(repository)) as conn:
        stored = conn.execute('SELECT id,state FROM captures WHERE id=?', (capture_id,)).fetchone()
    assert stored == (capture_id, 'sealed')
    proxy_state['process'] = start(*proxy_state['args'], env=proxy_env)
    wait_ready(port, context)
    status, _, body = fetch(port, f'/workbench/captures/{capture_id}', context=context)
    assert status == 200 and '已封存' in page(body)

    # Retire the recipe over HTTPS; existing captures stay readable and new
    # captures are refused without losing the stored one.
    status, _, _ = fetch(port, '/workbench/recipes/https-swatch/1/retire', {},
                         context=context, origin=PUBLIC)
    assert status == 303
    status, _, body = fetch(port, '/workbench/captures/' + capture_id, context=context)
    assert status == 200 and '已封存' in page(body)
    status, _, body = fetch(port, '/workbench/recipes/https-swatch/1/capture', {},
                            context=context, origin=PUBLIC)
    assert status == 409 and '已停止投用' in page(body)
    status, _, body = fetch(port, f'/workbench/captures/{capture_id}/database', context=context)
    assert status == 409 and '需先认证' in page(body)

    # 4. Build, certify, download, and check the certificate digest of the bytes.
    for action in ('build', 'certify'):
        status, _, _ = fetch(port, f'/workbench/captures/{capture_id}/{action}', {},
                             context=context, origin=PUBLIC)
        assert status == 303
    status, headers, body = fetch(port, f'/workbench/captures/{capture_id}', context=context)
    certificate = re.search(r'文件 SHA-256</dt><dd><code>([0-9a-f]{64})</code>', page(body))
    assert certificate, 'certificate digest not rendered'
    status, headers, database = fetch(port, f'/workbench/captures/{capture_id}/database',
                                      context=context)
    assert status == 200 and headers['ETag'] == '"' + hashlib.sha256(database).hexdigest() + '"'
    assert headers['ETag'].strip('"') == certificate[1]
    assert headers['Content-Disposition'].startswith('attachment;')
    fixture = repository.parent / 'drill-handoff.sqlite'
    fixture.write_bytes(database)
    with closing(sqlite3.connect(fixture)) as conn:
        assert conn.execute('PRAGMA foreign_key_check').fetchall() == []
        assert conn.execute("SELECT length_um FROM assay WHERE mark='S-01'").fetchone() == (3.25,)
    fixture.unlink()
    print('HTTPS 演练通过：页面表单身份、代理位置、停启后既有捕获、证书摘要核对')


proxy_env = {}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    token = secrets.token_hex(24)
    proxy_env.clear()
    proxy_env.update(os.environ | {TOKEN_ENV: token})
    with TemporaryDirectory(prefix='keelsplice-https-') as directory:
        root = Path(directory)
        source = root / 'microscope.sqlite'
        repository = root / 'state.sqlite'
        from examples.walkthrough import seed
        seed(source)
        subprocess.run((sys.executable, '-m', 'examples.make_certificate', '--directory', str(root / 'tls')), check=True)
        context = ssl.create_default_context(cafile=str(root / 'tls' / 'keelsplice-ca.pem'))
        backend_port, port = free_port(), free_port()
        backend = start(sys.executable, '-m', 'keelsplice', '--repository', str(repository),
                        '--source', f'microscope={source}', '--port', str(backend_port),
                        '--public-origin', PUBLIC, '--proxy-token-env', TOKEN_ENV, env=proxy_env)
        proxy_args = (sys.executable, '-m', 'examples.tls_proxy', '--port', str(port),
                      '--backend', f'http://127.0.0.1:{backend_port}', '--public-origin', PUBLIC,
                      '--certificate', str(root / 'tls' / 'keelsplice-server.pem'),
                      '--key', str(root / 'tls' / 'keelsplice-server.key'),
                      '--proxy-token-env', TOKEN_ENV)
        proxy = start(*proxy_args, env=proxy_env)
        state = {'process': proxy, 'args': proxy_args}
        try:
            wait_ready(port, context)
            run(repository, source, port, context, backend_port, state)
        finally:
            stop(state['process'])
            stop(backend)


if __name__ == '__main__':
    main()

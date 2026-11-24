"""Run the two deployment entries as separate processes against one repository.

The example launches ``python -m keelsplice`` twice — once on the built-in
``http.server`` entry (the legacy one) and once on ``--wsgi`` (the hosted one) —
sharing the same repository file and source registration.  It is the process
level companion to ``deployment_walkthrough.py``: the CLI, environment handling
and the switch back to the original entry are exercised, not just the callables.
"""

import hashlib
import json
import os
import signal
import socket
import sqlite3
import subprocess
import sys
import time
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from .walkthrough import seed

ROOT = Path(__file__).resolve().parent.parent
STARTUP_TIMEOUT = 20


def free_ports(count):
    sockets = [socket.socket() for _ in range(count)]
    try:
        for sock in sockets:
            sock.bind(('127.0.0.1', 0))
        return [sock.getsockname()[1] for sock in sockets]
    finally:
        for sock in sockets:
            sock.close()


def start(arguments, log):
    process = subprocess.Popen([sys.executable, '-m', 'keelsplice', *arguments], cwd=str(ROOT),
                               env={**os.environ, 'PYTHONPATH': str(ROOT)},
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    deadline = time.monotonic() + STARTUP_TIMEOUT
    while time.monotonic() < deadline:
        line = process.stdout.readline()
        if line:
            log.append(line.rstrip())
            if line.startswith('Keelsplice listening on '):
                return process
        elif process.poll() is not None:
            raise SystemExit('server exited before listening: ' + process.stdout.read())
        else:
            time.sleep(0.05)
    process.kill()
    raise SystemExit('server did not report a listening address')


def fetch(base, route, payload=None):
    data = None if payload is None else json.dumps(payload).encode()
    headers = {'Content-Type': 'application/json'} if data is not None else {}
    try:
        response = urlopen(Request(base + route, data=data, headers=headers), timeout=10)
    except HTTPError as exc:
        response = exc
    with response:
        return response.status, response.headers, response.read()


def stop(process):
    process.send_signal(signal.SIGINT)
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
    return process.stdout.read()


def main():
    with TemporaryDirectory(prefix='keelsplice-process-') as directory:
        root = Path(directory)
        source = root / 'microscope.sqlite'
        repository = root / 'state.sqlite'
        seed(source)
        legacy_port, hosted_port = free_ports(2)
        common = ['--repository', str(repository), '--source', f'microscope={source}']
        legacy_log, hosted_log = [], []
        legacy = start(common + ['--port', str(legacy_port)], legacy_log)
        hosted = start(common + ['--wsgi', '--port', str(hosted_port)], hosted_log)
        legacy_base = f'http://127.0.0.1:{legacy_port}'
        hosted_base = f'http://127.0.0.1:{hosted_port}'
        sealed = None
        try:
            assert 'http.server' in legacy_log[-1] and 'wsgi' in hosted_log[-1]
            old = fetch(legacy_base, '/sources/microscope/catalog')
            new = fetch(hosted_base, '/sources/microscope/catalog')
            assert old[0] == 200 and old[2] == new[2]
            assert fetch(hosted_base, '/workbench/assets/workspace.css')[1].get_content_type() == 'text/css'
            assert '外键关系' in fetch(legacy_base, '/workbench')[2].decode('utf-8')
            print('两个进程并行：原入口与 WSGI 宿主读取同一目录，错误结构一致')

            recipe = {'source': 'microscope', 'roots': [{'table': 'substrate', 'key': ['east', 'MICA']}],
                      'follow_children': [{'child': 'assay', 'id': 0}], 'row_limit': 20}
            assert fetch(hosted_base, '/recipes/process/versions', recipe)[0] == 201
            status, _, body = fetch(hosted_base, '/captures', {'name': 'process', 'version': 1})
            assert status == 201
            sealed = json.loads(body)['id']
            assert fetch(legacy_base, f'/captures/{sealed}')[0] == 200
            print('WSGI 进程封存的捕获可被原入口读取')
        finally:
            stop(hosted)
            stop(legacy)

        restarted_log = []
        restarted = start(common + ['--port', str(legacy_port)], restarted_log)
        try:
            base = f'http://127.0.0.1:{legacy_port}'
            status, _, body = fetch(base, f'/captures/{sealed}')
            assert status == 200 and json.loads(body)['id'] == sealed
            assert fetch(base, f'/captures/{sealed}/build', {})[0] == 200
            status, _, body = fetch(base, f'/captures/{sealed}/certify', {})
            assert status == 200 and json.loads(body)['state'] == 'certified'
            certificate = json.loads(body)['certificate']
            status, headers, data = fetch(base, f'/captures/{sealed}/database')
            assert status == 200 and headers['ETag'] == f'"{certificate["sha256"]}"'
            assert hashlib.sha256(data).hexdigest() == certificate['sha256']
            with closing(sqlite3.connect(repository)) as conn:
                assert conn.execute('SELECT count(*) FROM captures').fetchone()[0] == 1
                assert conn.execute('SELECT state FROM captures').fetchone()[0] == 'certified'
            print('停掉 WSGI 进程后原入口继续构建、认证并下载同一封存记录')
        finally:
            stop(restarted)


if __name__ == '__main__':
    main()

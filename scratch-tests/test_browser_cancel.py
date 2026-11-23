"""Browser-level checks for the cancellable preview job lifecycle over real HTML pages."""

import sqlite3
import sys
import threading
import time
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from keelsplice.api import create_server  # noqa: E402
from test_preview_cancel import seed_fanout  # noqa: E402


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
        return urlsplit(response.url).path, response.headers, response.read()


def html(base, path, payload=None, expected=200):
    path, headers, body = open_page(base, path, payload, expected)
    assert headers.get_content_type() == 'text/html', path
    return path, body.decode('utf-8')


class Gate:
    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()

    def __call__(self, job_id, conn, control):
        self.entered.set()
        self.release.wait(timeout=15)


def main():
    with TemporaryDirectory(prefix='keelsplice-browser-cancel-') as directory:
        root = Path(directory)
        source = root / 'fan.sqlite'
        seed_fanout(source)
        server = create_server(('127.0.0.1', 0), root / 'state.sqlite', {'fanout': source})
        bench = server.workbench
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.02})
        thread.start()
        base = f'http://127.0.0.1:{server.server_port}'
        try:
            # register a recipe through the JSON API (schema digest handled server-side)
            edge = bench.catalog('fanout')['schema']['edges'][0]
            bench.register('fan', {'source': 'fanout', 'roots': [{'table': 'root', 'key': [1]}],
                                   'follow_children': [{'child': 'leaf', 'id': edge['id']}],
                                   'row_limit': 5000})

            # Start a preview and hold it inside its read transaction.
            gate = Gate()
            bench.preview_gate = gate
            job_path, _ = html(base, '/workbench/recipes/fan/1/preview', {})
            assert job_path.startswith('/workbench/previews/'), job_path
            assert gate.entered.wait(10)
            _, running = html(base, job_path)
            assert '试算进行中' in running and '取消本次试算' in running
            assert 'http-equiv="refresh"' in running, 'running page should auto-refresh'
            assert f'action="{job_path}/cancel"' in running

            # Cancel via the browser form, then let the worker unwind.
            cancel_path, cancelled_page = html(base, job_path + '/cancel', {})
            assert cancel_path == job_path
            gate.release.set()
            deadline = time.time() + 10
            while '已取消' not in cancelled_page and time.time() < deadline:
                _, cancelled_page = html(base, job_path)
                time.sleep(0.05)
            assert '已取消' in cancelled_page
            assert '重新发起试算' in cancelled_page
            # Terminal page must keep refreshing no longer.
            assert 'http-equiv="refresh"' not in cancelled_page
            print('browser ok: running page auto-refreshes and cancel renders a durable page')

            # Reopening the same "tab" later still shows the cancelled conclusion.
            _, reopened = html(base, job_path)
            assert '已取消' in reopened and job_path in html(base, '/workbench/previews')[1]

            # A fresh trial completes and shows the capture action, not the old cancel.
            bench.preview_gate = None
            new_path, _ = html(base, '/workbench/recipes/fan/1/preview', {})
            assert new_path != job_path, 'each trial must get its own job id'
            deadline = time.time() + 10
            page = ''
            while time.time() < deadline:
                _, page = html(base, new_path)
                if '试算进行中' not in page:
                    break
                time.sleep(0.05)
            assert '试算完成' in page and '封存当前样本' in page and 'leaf' in page
            _, old = html(base, job_path)
            assert '已取消' in old, 'the previous cancellation must not be overwritten'
            print('browser ok: new trial completes independently and keeps its own id')
        finally:
            server.shutdown()
            thread.join()
            server.server_close()

        # Restart: a stranded running job is reported as inconclusive in the browser.
        orphan = 'b' * 32
        with closing(sqlite3.connect(root / 'state.sqlite')) as conn:
            conn.execute("INSERT INTO preview_jobs (id,name,version,state) VALUES (?,?,?,'running')",
                         (orphan, 'fan', 1))
            conn.commit()
        server2 = create_server(('127.0.0.1', 0), root / 'state.sqlite', {'fanout': source})
        t2 = threading.Thread(target=server2.serve_forever, kwargs={'poll_interval': 0.02})
        t2.start()
        try:
            base2 = f'http://127.0.0.1:{server2.server_port}'
            _, page = html(base2, f'/workbench/previews/{orphan}')
            assert '结论丢失' in page and '没有留下结论' in page
        finally:
            server2.shutdown()
            t2.join()
            server2.server_close()
        print('browser ok: restart shows the stranded trial as inconclusive')
        print('ALL BROWSER CANCELLATION CHECKS PASSED')


if __name__ == '__main__':
    main()

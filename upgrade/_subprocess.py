"""Subprocess and HTTP helpers shared by the upgrade phase scripts."""

import json
import socket
import subprocess
import time
from contextlib import closing
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def run_phase(python, module, arguments, cwd=None):
    """Run another upgrade/app module under an explicit interpreter.

    Modules are mapped to their absolute script path because isolated mode
    (``-I``) deliberately ignores PYTHONPATH and the script directory; the
    upgrade scripts add the project root to ``sys.path`` themselves.
    """
    script = PROJECT_ROOT / Path(module.replace('.', '/') + '.py')
    command = [python, '-I', str(script), *map(str, arguments)]
    completed = subprocess.run(command, cwd=cwd or PROJECT_ROOT, capture_output=True,
                               text=True, timeout=600, check=False)
    if completed.returncode != 0:
        raise RuntimeError(
            f'phase failed ({completed.returncode}): {command}\n'
            f'--- stdout ---\n{completed.stdout}\n--- stderr ---\n{completed.stderr}')
    return completed


def free_port():
    with closing(socket.socket()) as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


class LoopbackServer:
    """Start ``python -m keelsplice`` under a chosen interpreter as a subprocess."""

    def __init__(self, python, repository, sources, port=None):
        self.python = python
        self.repository = Path(repository)
        self.sources = dict(sources)
        self.port = port or free_port()
        self.process = None

    def __enter__(self):
        # -I keeps the environment out of the interpreter; the -c launcher adds
        # the project root explicitly so keelsplice's package-relative imports
        # resolve without relying on PYTHONPATH or the current directory.
        launcher = f"import sys; sys.path.insert(0, {str(PROJECT_ROOT)!r}); from keelsplice.__main__ import main; main()"
        command = [self.python, '-I', '-c', launcher, '--repository', str(self.repository),
                   '--port', str(self.port)]
        for alias, source in self.sources.items():
            command.extend(['--source', f'{alias}={Path(source)}'])
        self.process = subprocess.Popen(
            command, cwd=PROJECT_ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                stderr = self.process.stderr.read()
                raise RuntimeError(f'server exited early\n{stderr}')
            try:
                with closing(socket.create_connection(('127.0.0.1', self.port), timeout=0.5)):
                    break
            except OSError:
                time.sleep(0.1)
        else:
            self.stop()
            raise RuntimeError('server did not start listening within 20s')
        return self

    @property
    def base(self):
        return f'http://127.0.0.1:{self.port}'

    def request(self, route, body=None, status=200):
        headers = {'Content-Type': 'application/json'}
        data = None if body is None else json.dumps(body).encode('utf-8')
        try:
            response = urlopen(Request(self.base + route, data, headers), timeout=10)
        except HTTPError as exc:
            response = exc
        with response:
            payload = response.read()
            if response.status != status:
                raise AssertionError((route, response.status, payload[:500]))
            if response.headers.get_content_type() == 'application/vnd.sqlite3':
                return payload, response.headers['ETag'].strip('"')
            return json.loads(payload)

    def stop(self):
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()

    def __exit__(self, exc_type, exc, traceback):
        self.stop()
        return False

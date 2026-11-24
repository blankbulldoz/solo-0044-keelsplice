"""Start the loopback backend and the HTTPS front proxy together for local use."""

import argparse
import os
import secrets
import signal
import subprocess
import sys
from pathlib import Path

from keelsplice.web import access

TOKEN_ENV = 'KEELSPLICE_PROXY_TOKEN'
ROOT_FILES = ('keelsplice-ca.pem', 'keelsplice-server.pem', 'keelsplice-server.key')


def prepare(directory, host):
    """Reuse or create the root and a certificate covering the browser host."""
    missing = [name for name in ROOT_FILES if not (directory / name).is_file()]
    if missing:
        command = [sys.executable, '-m', 'examples.make_certificate', '--directory', str(directory)]
        if host not in ('localhost', '127.0.0.1'):
            command += ['--host', host]
        subprocess.run(command, check=True)
    return directory / 'keelsplice-server.pem', directory / 'keelsplice-server.key'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repository', required=True)
    parser.add_argument('--source', action='append', required=True, metavar='ALIAS=FILE')
    parser.add_argument('--backend-port', default=8794, type=int)
    parser.add_argument('--port', default=8443, type=int)
    parser.add_argument('--public-origin', default='https://localhost:8443')
    parser.add_argument('--tls-directory', default='keelsplice-local/tls')
    args = parser.parse_args()
    try:
        host, _ = access.split_origin(args.public_origin)
    except ValueError as exc:
        parser.error(str(exc))
    directory = Path(args.tls_directory)
    directory.mkdir(parents=True, exist_ok=True)
    certificate, key = prepare(directory, host)
    token = os.environ.get(TOKEN_ENV) or secrets.token_hex(24)
    environment = os.environ | {TOKEN_ENV: token}
    backend = [sys.executable, '-m', 'keelsplice', '--repository', args.repository,
               '--source', *args.source, '--port', str(args.backend_port),
               '--public-origin', args.public_origin, '--proxy-token-env', TOKEN_ENV]
    proxy = [sys.executable, '-m', 'examples.tls_proxy', '--port', str(args.port),
             '--backend', f'http://127.0.0.1:{args.backend_port}',
             '--public-origin', args.public_origin, '--certificate', str(certificate),
             '--key', str(key), '--proxy-token-env', TOKEN_ENV]
    children = [subprocess.Popen(command, env=environment) for command in (backend, proxy)]
    print(f'工作台 {args.public_origin}/workbench（浏览器须信任 {directory / "keelsplice-ca.pem"}）', flush=True)
    print(f'JSON 回环 http://127.0.0.1:{args.backend_port}', flush=True)

    def supervise(*_):
        for child in children:
            child.terminate()

    signal.signal(signal.SIGINT, supervise)
    signal.signal(signal.SIGTERM, supervise)
    try:
        while all(child.poll() is None for child in children):
            try:
                children[0].wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                pass
    finally:
        supervise()
        for child in children:
            child.wait(timeout=5)


if __name__ == '__main__':
    main()

"""Terminate local TLS and relay only the workbench to the loopback backend.

This is the only public edge of a proxied deployment. It forwards a fixed
header set that identifies the proxy and normalizes the browser-facing
origin, so the backend can tell an HTTPS form post from a direct loopback
one without trusting arbitrary Forwarded headers.
"""

import argparse
import os
import re
import ssl
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.request import HTTPRedirectHandler, Request, build_opener

from keelsplice.web import access
from keelsplice.web.forms import MAX_BYTES

LOOPBACK = ('127.0.0.1', '::1', 'localhost')
RESPONSE_HEADERS = {'Content-Type', 'ETag', 'Content-Disposition',
                    'Cache-Control', 'X-Content-Type-Options', 'Content-Security-Policy', 'Location'}


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


OPENER = build_opener(_NoRedirect)


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    server_version = 'KeelspliceTLS'
    sys_version = ''

    def log_message(self, format, *args):
        if self.server.verbose:
            super().log_message(format, *args)

    def body(self):
        length = self.headers.get('Content-Length', '0')
        if not re.fullmatch('[0-9]{1,8}', length):
            return None
        size = int(length)
        if size > MAX_BYTES:
            return None
        return self.rfile.read(size) if size else b''

    def respond(self, status, body, headers):
        self.send_response(status)
        for key, value in headers.items():
            self.send_header(key, value)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(body)

    def refuse(self, status, message):
        body = message.encode('utf-8')
        self.respond(status, body, {'Content-Type': 'text/plain; charset=utf-8', 'Cache-Control': 'no-store'})

    def is_workbench(self):
        path = self.path
        return path == '/' or path.startswith('/workbench') or path.startswith('/?')

    def relay(self, data):
        target = self.server.backend.rstrip('/') + self.path
        forwarded = {
            'Host': self.server.backend_host,
            'X-Forwarded-Proto': 'https',
            'X-Forwarded-Host': self.server.public_host,
            'X-Forwarded-Port': str(self.server.public_port),
            access.PROXY_HEADER: self.server.token,
            'Origin': self.headers.get('Origin', ''),
        }
        if data is not None:
            forwarded['Content-Type'] = self.headers.get('Content-Type', '')
        try:
            with OPENER.open(Request(target, data=data, headers=forwarded, method=self.command), timeout=20) as answer:
                return answer.status, answer.read(), answer.headers
        except Exception as exc:  # urllib raises HTTPError for backend 4xx/5xx
            answer = getattr(exc, 'code', None)
            if answer is None:
                self.refuse(502, '后端工作台不可用。')
                return None
            return answer, exc.read(), exc.headers

    def dispatch(self):
        try:
            if not self.is_workbench():
                self.refuse(404, '前置代理只发布工作台路径。')
                return
            if self.command == 'POST' and self.headers.get('Origin', '').strip().lower() != self.server.origin:
                self.refuse(403, '表单来源与已登记的工作台地址不一致。')
                return
            data = self.body()
            if data is None:
                self.refuse(413 if len(self.headers.get('Content-Length', '')) > 8 else 400, '请求长度无效。')
                return
            relayed = self.relay(data if self.command == 'POST' else None)
            if relayed is None:
                return
            status, body, headers = relayed
            self.respond(status, body, {k: v for k, v in headers.items() if k in RESPONSE_HEADERS})
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            return

    do_GET = dispatch
    do_POST = dispatch


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, backend, public_origin, token, verbose=False):
        self.backend = backend
        self.token = token
        self.verbose = verbose
        self.origin = access.public_origin(public_origin)
        host, port = access.split_origin(self.origin)
        self.public_host = host if port == '443' else f'{host}:{port}'
        self.public_port = port
        self.backend_host = backend.split('://', 1)[-1].split('/', 1)[0]
        super().__init__(address, Handler)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--listen', default='127.0.0.1', help='loopback interface to listen on')
    parser.add_argument('--port', default=8443, type=int)
    parser.add_argument('--backend', default='http://127.0.0.1:8794', help='loopback backend base URL')
    parser.add_argument('--public-origin', required=True, metavar='https://HOST:PORT')
    parser.add_argument('--certificate', required=True)
    parser.add_argument('--key', required=True)
    parser.add_argument('--proxy-token-env', default='KEELSPLICE_PROXY_TOKEN')
    parser.add_argument('--verbose', action='store_true')
    args = parser.parse_args()
    if args.listen not in LOOPBACK:
        parser.error('--listen must stay on a loopback interface')
    token = os.environ.get(args.proxy_token_env, '')
    if len(token) < access.MIN_TOKEN or not token.isascii():
        parser.error(f'Set {args.proxy_token_env} to at least {access.MIN_TOKEN} ASCII characters')
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(args.certificate, args.key)
    with Server((args.listen, args.port), args.backend, args.public_origin, token, args.verbose) as server:
        server.socket = context.wrap_socket(server.socket, server_side=True)
        print(f'TLS proxy listening on {args.public_origin} -> {args.backend}', flush=True)
        server.serve_forever()


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)

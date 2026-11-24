"""Loopback HTTP entry points and wire-level error mapping."""

import json
import re
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from .codec import Fault, dumps
from .recipe import fields
from .service import Workbench
from .storage import Repository
from .web import http as web

MAX_REQUEST_BYTES = 256 * 1024
VERSION = re.compile('[1-9][0-9]{0,18}')


class FixtureHTTPServer(ThreadingHTTPServer):
    def __init__(self, address, workbench):
        self.workbench = workbench
        super().__init__(address, RequestHandler)


class RequestHandler(BaseHTTPRequestHandler):
    def setup(self):
        self.request.settimeout(10)
        super().setup()

    def read_body(self):
        length = self.headers.get('Content-Length', '0')
        if not re.fullmatch('[0-9]{1,8}', length):
            raise Fault('invalid_length', 'Invalid Content-Length', 400)
        size = int(length)
        if size > MAX_REQUEST_BYTES:
            raise Fault('payload_too_large', 'JSON request exceeds 256 KiB', 413)
        if not size:
            return {}
        if self.headers.get_content_type() != 'application/json':
            raise Fault('invalid_content_type', 'Expected application/json', 415)
        try:
            return json.loads(self.rfile.read(size).decode('utf-8'))
        except (UnicodeError, ValueError, RecursionError) as exc:
            raise Fault('invalid_json', 'Expected a JSON body', 400) from exc

    def route_request(self):
        path = urlsplit(self.path)
        parts = path.path.removeprefix('/').split('/')
        if path.query or path.fragment:
            raise Fault('invalid_query', 'Use the JSON body for parameters', 400)
        bench = self.server.workbench
        body = self.read_body() if self.command == 'POST' else None
        match self.command, parts:
            case 'GET', ['sources', alias, 'catalog']:
                return 200, bench.catalog(alias)
            case 'POST', ['proposals', 'evaluate']:
                return 200, bench.evaluate_proposal(body)
            case 'POST', ['recipes', name, 'versions']:
                return 201, bench.register(name, body)
            case 'GET', ['recipes', name, 'versions', number] if VERSION.fullmatch(number) and int(number) < 2**63:
                return 200, bench.version(name, int(number))
            case 'POST', ['recipes', name, 'versions', number, 'retire'] if VERSION.fullmatch(number) and int(number) < 2**63:
                fields(body)
                return 200, bench.retire(name, int(number))
            case 'POST', ['preview']:
                return 200, bench.preview(body)
            case 'POST', ['captures']:
                return 201, bench.capture(body)
            case 'GET', ['captures', capture_id]:
                return 200, bench.status(capture_id)
            case 'POST', ['captures', capture_id, 'trace']:
                return 200, bench.trace(capture_id, body)
            case 'POST', ['captures', capture_id, 'build']:
                fields(body)
                return 200, bench.build(capture_id)
            case 'POST', ['captures', capture_id, 'certify']:
                fields(body)
                return 200, bench.certify(capture_id)
            case 'POST', ['compare']:
                return 200, bench.compare(body)
            case 'GET', ['captures', capture_id, 'database']:
                return 200, bench.download(capture_id)
        raise Fault('unknown_route', 'Unknown endpoint', 404)

    def send_result(self, status, payload):
        self.send_response(status)
        if isinstance(payload, tuple):
            data, sha = payload
            self.send_header('Content-Type', 'application/vnd.sqlite3')
            self.send_header('Content-Disposition', 'attachment; filename="keelsplice-fixture.sqlite"')
            self.send_header('ETag', f'"{sha}"')
        else:
            data = dumps(payload).encode('ascii')
            self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def handle_request(self):
        if self.path in ('/', '/workbench') or self.path.startswith(('/workbench/', '/workbench?')):
            return web.handle(self)
        try:
            status, payload = self.route_request()
        except Fault as exc:
            status, payload = exc.status, {'error': exc.code, 'message': str(exc)}
        except sqlite3.Error as exc:
            busy = getattr(exc, 'sqlite_errorcode', 0) & 0xFF in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)
            status, payload = ((503, {'error': 'database_busy', 'message': 'A database is busy'}) if busy
                               else (422, {'error': 'database_input', 'message': 'SQLite could not process the database input'}))
        except ValueError:
            status, payload = 400, {'error': 'invalid_request', 'message': 'Malformed HTTP request'}
        try:
            self.send_result(status, payload)
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            return

    do_GET = handle_request
    do_POST = handle_request


def create_server(address, repo, sources):
    return FixtureHTTPServer(address, Workbench(Repository(repo, sources)))

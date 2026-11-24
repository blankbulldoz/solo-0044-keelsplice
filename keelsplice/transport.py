"""Transport-neutral request handling shared by the stdlib and WSGI entries.

Both the original ``http.server`` entry and the WSGI application build a
:class:`Request` and hand it to :func:`respond`.  All status codes, JSON error
shapes, HTML pages, form validation and download headers therefore come from
this single implementation, which keeps the two entries interchangeable.
"""

import json
import re
import sqlite3
from dataclasses import dataclass, field
from http.client import responses
from urllib.parse import urlsplit

from .codec import Fault, dumps
from .recipe import fields
from .web import http as web
from .web.forms import MAX_BYTES, decode_form

MAX_REQUEST_BYTES = 256 * 1024
VERSION = re.compile('[1-9][0-9]{0,18}')
BUSY_CODES = (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)

CSP = "default-src 'none'; style-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'"


@dataclass(frozen=True)
class Response:
    status: int
    headers: tuple = field(default=())
    body: bytes = b''

    def reason(self):
        return responses.get(self.status, 'Unknown')


class Request:
    """A method, raw request target, headers and a bounded body reader.

    ``headers`` must provide ``get(name, default)`` and ``get_content_type()``
    with the semantics of :class:`email.message.Message`, which is what the
    stdlib handler already exposes.  ``read`` mirrors ``rfile.read``.
    """

    def __init__(self, method, target, headers, read, workbench):
        self.method = method
        self.target = target
        self.headers = headers
        self.workbench = workbench
        self._read = read

    def read_bytes(self, size):
        chunks = []
        remaining = size
        while remaining:
            chunk = self._read(remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        return b''.join(chunks)

    def json_body(self):
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
            return json.loads(self.read_bytes(size).decode('utf-8'))
        except (UnicodeError, ValueError, RecursionError) as exc:
            raise Fault('invalid_json', 'Expected a JSON body', 400) from exc

    def form_values(self):
        host = self.headers.get('Host', '')
        origin = self.headers.get('Origin')
        if not web.LOOPBACK_HOST.fullmatch(host) or origin != 'http://' + host:
            raise Fault('browser_origin', '请从本机工作台提交此表单。', 403)
        length = self.headers.get('Content-Length', '')
        if not re.fullmatch('[0-9]{1,8}', length):
            raise Fault('browser_form', '表单长度无效。', 400)
        size = int(length)
        if size > MAX_BYTES:
            raise Fault('browser_form', '表单超过 256 KiB。', 413)
        if size and self.headers.get_content_type() != 'application/x-www-form-urlencoded':
            raise Fault('browser_form', '请使用工作台表单格式。', 415)
        return decode_form(self.read_bytes(size))


def is_workbench(target):
    return target in ('/', '/workbench') or target.startswith(('/workbench/', '/workbench?'))


def route_json(request):
    path = urlsplit(request.target)
    parts = path.path.removeprefix('/').split('/')
    if path.query or path.fragment:
        raise Fault('invalid_query', 'Use the JSON body for parameters', 400)
    bench = request.workbench
    body = request.json_body() if request.method == 'POST' else None
    match request.method, parts:
        case 'GET', ['sources', alias, 'catalog']:
            return 200, bench.catalog(alias)
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


def json_response(request):
    try:
        status, payload = route_json(request)
    except Fault as exc:
        status, payload = exc.status, {'error': exc.code, 'message': str(exc)}
    except sqlite3.Error as exc:
        busy = getattr(exc, 'sqlite_errorcode', 0) & 0xFF in BUSY_CODES
        status, payload = ((503, {'error': 'database_busy', 'message': 'A database is busy'}) if busy
                           else (422, {'error': 'database_input', 'message': 'SQLite could not process the database input'}))
    except ValueError:
        status, payload = 400, {'error': 'invalid_request', 'message': 'Malformed HTTP request'}
    if isinstance(payload, tuple):
        data, sha = payload
        headers = (('Content-Type', 'application/vnd.sqlite3'),
                   ('Content-Disposition', 'attachment; filename="keelsplice-fixture.sqlite"'),
                   ('ETag', f'"{sha}"'))
    else:
        data = dumps(payload).encode('ascii')
        headers = (('Content-Type', 'application/json'),)
    return Response(status, headers + (('Content-Length', str(len(data))),), data)


def web_response(request):
    try:
        status, body, kind, extra = web.route(request)
    except (Fault, sqlite3.Error, ValueError, UnicodeError) as exc:
        status, body, kind, extra = web.failure(exc)
    headers = (('Content-Type', kind), ('Content-Length', str(len(body))),
               ('Cache-Control', 'no-store'), ('X-Content-Type-Options', 'nosniff'),
               ('Content-Security-Policy', CSP)) + tuple(extra.items())
    return Response(status, headers, body)


def unsupported(request):
    data = dumps({'error': 'invalid_method', 'message': 'Only GET and POST are supported'}).encode('ascii')
    body = b'' if request.method == 'HEAD' else data
    return Response(501, (('Content-Type', 'application/json'), ('Allow', 'GET, POST'),
                          ('Content-Length', str(len(data)))), body)


def respond(request):
    """Map one request to a transport-independent response."""
    if request.method not in ('GET', 'POST'):
        return unsupported(request)
    if is_workbench(request.target):
        return web_response(request)
    return json_response(request)

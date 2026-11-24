"""PEP 3333 application for hosting the workbench under a supported WSGI host.

The WSGI entry and the original ``http.server`` entry both call
:func:`keelsplice.transport.respond`, so paths, status codes, JSON error
envelopes, form-origin checks and download headers are identical.  The two
processes may run at the same time against one repository file; SQLite
serializes their writes and readers always see committed records.

The application must be mounted at the server root (empty ``SCRIPT_NAME``)
because rendered forms and redirects use root-relative ``/workbench`` URLs.
"""

import json
import os
import sys
import threading
from email.message import Message
from socketserver import ThreadingMixIn
from wsgiref.simple_server import WSGIRequestHandler, WSGIServer, make_server

from .service import Workbench
from .storage import ALIAS, Repository
from .transport import Request, respond

REPOSITORY_ENV = 'KEELSPLICE_REPOSITORY'
SOURCES_ENV = 'KEELSPLICE_SOURCES'


class EnvironHeaders:
    """Expose WSGI environ headers with ``HTTPMessage`` lookup semantics."""

    def __init__(self, environ):
        self.environ = environ

    def get(self, name, default=None):
        key = name.upper().replace('-', '_')
        if key == 'CONTENT_TYPE':
            value = self.environ.get('CONTENT_TYPE')
        elif key == 'CONTENT_LENGTH':
            # PEP 3333 allows an empty CONTENT_LENGTH when there is no body.
            value = self.environ.get('CONTENT_LENGTH') or None
        elif key == 'HOST':
            value = self.environ.get('HTTP_HOST')
            if value is None and self.environ.get('SERVER_NAME'):
                value = '{}:{}'.format(self.environ['SERVER_NAME'], self.environ.get('SERVER_PORT', '80'))
        else:
            value = self.environ.get('HTTP_' + key)
        return default if value is None else value

    def get_content_type(self):
        message = Message()
        if self.environ.get('CONTENT_TYPE'):
            message['Content-Type'] = self.environ['CONTENT_TYPE']
        return message.get_content_type()


def target(environ):
    raw = environ.get('RAW_URI') or environ.get('REQUEST_URI')
    if raw:
        return raw
    path = environ.get('PATH_INFO') or '/'
    query = environ.get('QUERY_STRING', '')
    return path + ('?' + query if query else '')


class Application:
    def __init__(self, workbench):
        self.workbench = workbench

    def __call__(self, environ, start_response):
        method = environ.get('REQUEST_METHOD', 'GET').upper()
        if environ.get('SCRIPT_NAME'):
            body = 'Mount Keelsplice at the server root; SCRIPT_NAME must be empty.\n'.encode('utf-8')
            start_response('500 Internal Server Error', [('Content-Type', 'text/plain; charset=utf-8'),
                                                          ('Content-Length', str(len(body)))])
            return [body]
        request = Request(method, target(environ), EnvironHeaders(environ),
                          environ['wsgi.input'].read, self.workbench)
        result = respond(request)
        start_response(f'{result.status} {result.reason()}', list(result.headers))
        return [result.body]


def parse_sources(value):
    try:
        pairs = json.loads(value)
    except ValueError as exc:
        raise RuntimeError(f'{SOURCES_ENV} must be a JSON object of alias to file') from exc
    if not isinstance(pairs, dict) or not pairs:
        raise RuntimeError(f'{SOURCES_ENV} must map at least one alias to a file')
    for alias, file in pairs.items():
        if not ALIAS.fullmatch(alias) or not isinstance(file, str) or not file:
            raise RuntimeError(f'Invalid source registration in {SOURCES_ENV}: {alias!r}')
    return pairs


def create_application(environ=None):
    env = os.environ if environ is None else environ
    repository = env.get(REPOSITORY_ENV)
    if not repository:
        raise RuntimeError(f'{REPOSITORY_ENV} must name the repository file')
    return Application(Workbench(Repository(repository, parse_sources(env.get(SOURCES_ENV, '')))))


_application = None
_lock = threading.Lock()


def application(environ, start_response):
    """Lazily built callable for hosts configured through the environment."""
    global _application
    if _application is None:
        with _lock:
            if _application is None:
                _application = create_application()
    return _application(environ, start_response)


class ThreadingWSGIServer(ThreadingMixIn, WSGIServer):
    """Loopback WSGI host with one thread per request."""

    daemon_threads = True
    allow_reuse_address = True


class QuietHandler(WSGIRequestHandler):
    """Request log with the same access-log shape as the stdlib entry."""

    def log_message(self, format, *args):
        print(f'{self.address_string()} - - [{self.log_date_time_string()}] {format % args}', file=sys.stderr)


def serve(address, repository, sources):
    return make_server(address[0], address[1], Application(Workbench(Repository(repository, sources))),
                       server_class=ThreadingWSGIServer, handler_class=QuietHandler)

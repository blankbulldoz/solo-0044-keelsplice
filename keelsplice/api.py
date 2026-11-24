"""Loopback HTTP entry built on the shared transport core."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .service import Workbench
from .storage import Repository
from .transport import Request, respond


class FixtureHTTPServer(ThreadingHTTPServer):
    def __init__(self, address, workbench):
        self.workbench = workbench
        super().__init__(address, RequestHandler)


class RequestHandler(BaseHTTPRequestHandler):
    def setup(self):
        self.request.settimeout(10)
        super().setup()

    def send_result(self, result):
        self.send_response(result.status)
        for name, value in result.headers:
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(result.body)

    def handle_request(self):
        request = Request(self.command, self.path, self.headers, self.rfile.read, self.server.workbench)
        try:
            self.send_result(respond(request))
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            return

    do_GET = handle_request
    do_POST = handle_request
    do_HEAD = handle_request
    do_PUT = handle_request
    do_PATCH = handle_request
    do_DELETE = handle_request
    do_OPTIONS = handle_request


def create_server(address, repo, sources):
    return FixtureHTTPServer(address, Workbench(Repository(repo, sources)))

"""Route workbench requests while sharing the existing domain operations."""

import re
import sqlite3
from urllib.parse import urlsplit

from . import assets, captures, comparison, directory, evidence, preview, selection, versions
from .errors import invalid_route, message
from .forms import decode_form, field, version
from .render import page, segment, text
from ..codec import Fault

LOOPBACK_HOST = re.compile(r'(?:127\.0\.0\.1|localhost)(?::[0-9]{1,5})?\Z')


def redirect(path):
    return 303, b'', 'text/plain; charset=utf-8', {'Location': path}


def route(request):
    bench = request.workbench
    method = request.method
    path = urlsplit(request.target)
    if method == 'GET' and path.path == '/':
        return redirect('/workbench')
    parts = path.path.removeprefix('/').split('/')
    if method == 'GET' and parts[:2] == ['workbench', 'assets'] and len(parts) == 3:
        return 200, assets.serve(parts[2]), 'text/css; charset=utf-8', {}
    query = decode_form(path.query.encode('ascii'))
    values = request.form_values() if method == 'POST' else {}
    match method, parts:
        case 'GET', ['workbench']:
            status, body = directory.show(bench, query)
        case 'GET', ['workbench', 'recipes']:
            status, body = versions.index(bench)
        case 'GET', ['workbench', 'recipes', 'new']:
            source = field(query, 'source', next(iter(sorted(bench.repository.sources)), ''))
            status, body = selection.editor(bench, source)
        case 'POST', ['workbench', 'recipes', 'new']:
            result = selection.register(bench, values)
            if isinstance(result, str):
                return redirect(result)
            status, body = result
        case 'GET', ['workbench', 'recipes', name, number]:
            status, body = versions.detail(bench, name, version(number))
        case 'POST', ['workbench', 'recipes', name, number, action]:
            number = version(number)
            base = f'/workbench/recipes/{segment(name)}/{number}'
            if action == 'preview':
                status, body = preview.show(bench, name, number)
            elif action == 'capture':
                capture = bench.capture(dict(name=name, version=number))
                return redirect(f'/workbench/captures/{segment(capture["id"])}')
            elif action == 'retire':
                bench.retire(name, number)
                return redirect(base)
            else:
                raise invalid_route()
        case 'GET', ['workbench', 'captures']:
            status, body = captures.index(bench)
        case 'GET', ['workbench', 'captures', capture_id]:
            status, body = captures.detail(bench, capture_id)
        case 'GET', ['workbench', 'captures', capture_id, 'database']:
            data, sha = bench.download(capture_id)
            return 200, data, 'application/vnd.sqlite3', {
                'Content-Disposition': f'attachment; filename="keelsplice-{capture_id}.sqlite"',
                'ETag': f'"{sha}"',
            }
        case 'POST', ['workbench', 'captures', capture_id, action]:
            if action == 'trace':
                status, body = evidence.show(bench, capture_id, values)
            elif action in ('build', 'certify'):
                (bench.build if action == 'build' else bench.certify)(capture_id)
                return redirect(f'/workbench/captures/{segment(capture_id)}')
            else:
                raise invalid_route()
        case 'GET', ['workbench', 'compare']:
            status, body = comparison.show(bench)
        case 'POST', ['workbench', 'compare']:
            status, body = comparison.show(bench, values)
        case _:
            raise invalid_route()
    return status, body, 'text/html; charset=utf-8', {}


def failure(exc):
    if isinstance(exc, sqlite3.Error):
        busy = getattr(exc, 'sqlite_errorcode', 0) & 255 in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)
        exc = Fault('database_busy', '', 503) if busy else Fault('database_input', '', 422)
    elif not isinstance(exc, Fault):
        exc = Fault('browser_form', '请求格式无效。', 400)
    body = page('操作未完成', '', 'error', error=text(message(exc)), status=exc.status)
    return exc.status, body, 'text/html; charset=utf-8', {}

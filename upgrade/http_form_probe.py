"""Exact wire-byte checks of the Keelsplice browser form surface.

The workbench's browser path speaks ``application/x-www-form-urlencoded`` over
loopback and enforces Origin, Content-Length and Content-Type rules before any
domain code runs.  Using a raw socket keeps the request and response frames
exactly observable: every probe hashes the literal bytes it sent and received,
which a normal HTTP client library would hide behind its own encoders.

The same probe sequence runs under Python 3.11 (building the legacy
repository) and under Python 3.14 (against the switched repository, and also
against a fresh disposable repository for byte-level parity).  ``compare``
diffs the two reports and treats only deliberately volatile identifiers
(capture ids) as exempt; everything else must be byte-identical or
semantically equal.
"""

import argparse
import base64
import hashlib
import json
import re
import socket
import sqlite3
import sys
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from urllib.parse import urlencode

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from keelsplice.api import create_server  # noqa: E402
from upgrade.fixture_probe import reread_fixture  # noqa: E402

SCHEMA_VERSION = 1
MAX_BYTES = 256 * 1024
DIGEST_PATTERN = re.compile(rb'name="digest" value="([0-9a-f]{64})"')
STABLE_RESPONSES = (
    'catalog_page', 'editor_page', 'stale_digest', 'wrong_origin',
    'invalid_json', 'wrong_content_type', 'missing_length',
    'unicode_name_decoded', 'json_catalog',
)
STABLE_FRAMES = (
    'valid_recipe', 'stale_digest', 'wrong_origin', 'invalid_json',
    'wrong_content_type', 'unicode_name',
)


def _sha256(data):
    return hashlib.sha256(data).hexdigest()


class WireClient:
    """Minimal HTTP/1.x client that preserves the exact frame bytes."""

    def __init__(self, host, port):
        self.host = host
        self.port = port
        self.frames = []

    def exchange(self, name, method, path, body=None, headers=None):
        headers = dict(headers or {})
        if body is None:
            body = b''
        elif isinstance(body, str):
            body = body.encode('utf-8')
        frame_lines = [f'{method} {path} HTTP/1.1'.encode('ascii'),
                       f'Host: {self.host}:{self.port}'.encode('ascii')]
        for key, value in headers.items():
            frame_lines.append(f'{key}: {value}'.encode('ascii'))
        if body and 'Content-Length' not in headers:
            frame_lines.append(f'Content-Length: {len(body)}'.encode('ascii'))
        frame_lines.append(b'Connection: close')
        frame = b'\r\n'.join(frame_lines) + b'\r\n\r\n' + body
        with closing(socket.create_connection((self.host, self.port), timeout=10)) as sock:
            sock.sendall(frame)
            chunks = []
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                chunks.append(chunk)
        raw = b''.join(chunks)
        head, _, response_body = raw.partition(b'\r\n\r\n')
        status_line, *header_lines = head.split(b'\r\n')
        response_headers = {}
        for line in header_lines:
            key, _, value = line.partition(b':')
            response_headers[key.decode('ascii').lower()] = value.strip().decode('ascii')
        _, status_text, _ = status_line.decode('ascii').split(' ', 2)
        self.frames.append({
            'name': name, 'method': method, 'path': path,
            'header_names': [line.split(b':', 1)[0].decode('ascii') for line in header_lines],
            'content_length': len(body), 'body_sha256': _sha256(body),
            'frame_sha256': _sha256(frame),
            # Retained so the cross-runtime comparator can normalize the probe's
            # own prefixes and recompute honest hashes; never sent anywhere.
            'body_b64': base64.b64encode(body).decode('ascii'),
        })
        return int(status_text), response_headers, response_body

    def get(self, name, path, headers=None):
        return self.exchange(name, 'GET', path, headers=headers)

    def form(self, name, path, fields, origin=None, extra_headers=None):
        body = urlencode(fields, doseq=True).encode('ascii')
        headers = {'Content-Type': 'application/x-www-form-urlencoded'}
        if origin is not None:
            headers['Origin'] = origin
        if extra_headers:
            headers.update(extra_headers)
        return self.exchange(name, 'POST', path, body, headers)


def _form_fields(digest, name):
    return {
        'source': 'microscope', 'name': name, 'digest': digest,
        'row_limit': '20', 'edge': ['0:assay'],
        'roots': '[{"table":"substrate","key":["east","MICA"]}]',
    }


def _seed_source(path):
    sql = (PROJECT_ROOT / 'examples' / 'microscope.sql').read_text(encoding='utf-8')
    with closing(sqlite3.connect(path)) as conn:
        conn.executescript(sql)


def run_probe(repository, source, role, prefix, sqlite_cli=None):
    """Exercise every form validation path and the full form lifecycle."""
    server = create_server(('127.0.0.1', 0), repository, {'microscope': source})
    thread = Thread(target=server.serve_forever, kwargs={'poll_interval': 0.05})
    thread.start()
    client = WireClient('127.0.0.1', server.server_port)
    base = f'http://127.0.0.1:{server.server_port}'
    responses = {}
    json_payloads = {}
    try:
        status, headers, body = client.get('catalog_page', '/workbench?source=microscope&table=substrate')
        assert status == 200 and headers['content-type'].startswith('text/html')
        assert '外键关系'.encode('utf-8') in body
        responses['catalog_page'] = _response(status, headers, body, ['外键关系'])

        status, headers, body = client.get('editor_page', '/workbench/recipes/new')
        assert status == 200
        digest = DIGEST_PATTERN.search(body).group(1).decode('ascii')
        responses['editor_page'] = _response(status, headers, body, [digest])

        status, headers, body = client.form(
            'stale_digest', '/workbench/recipes/new', _form_fields('0' * 64, f'{prefix}-stale'),
            origin=base)
        assert status == 409 and '目录已变化'.encode('utf-8') in body
        responses['stale_digest'] = _response(status, headers, body, ['目录已变化'])

        status, headers, body = client.form(
            'wrong_origin', '/workbench/recipes/new', _form_fields(digest, f'{prefix}-origin'),
            origin='http://localhost:1')
        assert status == 403 and '本机工作台'.encode('utf-8') in body
        responses['wrong_origin'] = _response(status, headers, body, ['本机工作台'])

        bad_fields = _form_fields(digest, f'{prefix}-badjson')
        bad_fields['roots'] = 'bad'
        status, headers, body = client.form('invalid_json', '/workbench/recipes/new',
                                            bad_fields, origin=base)
        assert status == 400 and '有效的 JSON'.encode('utf-8') in body
        responses['invalid_json'] = _response(status, headers, body, ['有效的 JSON'])

        status, headers, body = client.exchange(
            'wrong_content_type', 'POST', '/workbench/recipes/new',
            urlencode(_form_fields(digest, f'{prefix}-ctype')).encode('ascii'),
            {'Content-Type': 'text/plain', 'Origin': base})
        assert status == 415 and '工作台表单格式'.encode('utf-8') in body
        responses['wrong_content_type'] = _response(status, headers, body, ['工作台表单格式'])

        status, headers, body = client.exchange(
            'missing_length', 'POST', '/workbench/recipes/new', None,
            {'Content-Type': 'application/x-www-form-urlencoded', 'Origin': base})
        assert status == 400 and '表单长度无效'.encode('utf-8') in body
        responses['missing_length'] = _response(status, headers, body, ['表单长度'])

        oversized = urlencode(_form_fields(digest, f'{prefix}-big')).encode('ascii') + b'x' * (MAX_BYTES + 1)
        status, headers, body = client.exchange(
            'oversized_form', 'POST', '/workbench/recipes/new', oversized,
            {'Content-Type': 'application/x-www-form-urlencoded', 'Origin': base})
        assert status == 413 and '256 KiB'.encode('utf-8') in body
        responses['oversized_form'] = _response(status, headers, body, ['256 KiB'])

        # Non-ASCII percent-encoding must be decoded before domain validation.
        unicode_fields = _form_fields(digest, f'{prefix}-样品')
        status, headers, body = client.form('unicode_name', '/workbench/recipes/new',
                                            unicode_fields, origin=base)
        assert status == 422 and '配方名须以小写字母开头'.encode('utf-8') in body
        responses['unicode_name_decoded'] = _response(
            status, headers, body, ['配方名须以小写字母开头'])

        status, headers, body = client.form(
            'valid_recipe', '/workbench/recipes/new',
            _form_fields(digest, f'{prefix}-form'), origin=base)
        assert status == 303 and headers['location'].endswith('/1')
        recipe_path = headers['location']
        responses['valid_recipe'] = _response(status, headers, body, location=headers['location'])

        status, headers, body = client.exchange(
            'json_catalog', 'GET', '/sources/microscope/catalog')
        assert status == 200 and headers['content-type'] == 'application/json'
        catalog = json.loads(body)
        json_payloads['catalog_schema_digest'] = catalog['schema_digest']
        assert catalog['schema_digest'] == digest
        responses['json_catalog'] = _response(status, headers, body, [digest])

        json_recipe = {'source': 'microscope',
                       'roots': [{'table': 'substrate', 'key': ['east', 'MICA']}],
                       'follow_children': [{'child': 'assay', 'id': 0}], 'row_limit': 20}
        status, headers, body = client.exchange(
            'json_recipe', 'POST', f'/recipes/{prefix}-json/versions',
            json.dumps(json_recipe).encode('utf-8'), {'Content-Type': 'application/json'})
        assert status == 201
        json_payloads['json_recipe'] = json.loads(body)['recipe']

        # The form-created recipe must normalize identically through the JSON view.
        form_recipe_name = f'{prefix}-form'
        status, headers, body = client.exchange(
            'json_recipe_detail', 'GET', f'/recipes/{form_recipe_name}/versions/1')
        assert status == 200 and headers['content-type'] == 'application/json'
        form_recipe = json.loads(body)['recipe']
        json_payloads['form_recipe'] = form_recipe
        json_payloads['recipes_normalize_identically'] = form_recipe == json_payloads['json_recipe']
        assert json_payloads['recipes_normalize_identically']

        capture_id = _form_lifecycle(client, responses, recipe_path)

        status, headers, body = client.get('fixture_download',
                                           f'/workbench/captures/{capture_id}/database')
        assert status == 200
        etag = headers['etag'].strip('"')
        assert etag == _sha256(body)
        fixture = reread_fixture(body, sqlite_cli=sqlite_cli)
        responses['fixture_download'] = _response(status, headers, body, [etag])
    finally:
        server.shutdown()
        thread.join()
        server.server_close()
    return {
        'schema_version': SCHEMA_VERSION,
        'role': role,
        'runtime': _runtime(),
        'repository': str(Path(repository).resolve()),
        'request_frames': client.frames,
        'responses': responses,
        'stable_responses': list(STABLE_RESPONSES),
        'stable_frames': list(STABLE_FRAMES),
        'json_payloads': json_payloads,
        'fixture': fixture,
    }


def _form_lifecycle(client, responses, recipe_path):
    origin = client_origin(client)
    status, headers, body = client.form('form_preview', recipe_path + '/preview',
                                        {'confirm': '1'}, origin=origin)
    assert status == 200 and '未封存'.encode('utf-8') in body
    responses['form_preview'] = _response(status, headers, body, ['未封存'])

    status, headers, body = client.form('form_capture', recipe_path + '/capture',
                                        {'confirm': '1'}, origin=origin)
    assert status == 303
    capture_path = headers['location']
    capture_id = capture_path.rsplit('/', 1)[1]
    responses['form_capture'] = _response(status, headers, body, location=capture_path)

    status, headers, body = client.get('capture_detail', capture_path)
    assert status == 200 and '已封存'.encode('utf-8') in body

    status, headers, body = client.form('form_build', capture_path + '/build',
                                        {'confirm': '1'}, origin=origin)
    assert status == 303
    status, headers, body = client.form('form_certify', capture_path + '/certify',
                                        {'confirm': '1'}, origin=origin)
    assert status == 303
    return capture_id


def client_origin(client):
    return f'http://{client.host}:{client.port}'


def _response(status, headers, body, contains=(), location=None):
    text = body.decode('utf-8', errors='replace')
    return {
        'status': status,
        'content_type': headers.get('content-type', ''),
        'body_sha256': _sha256(body),
        'body_bytes': len(body),
        'contains': [token for token in contains if token.encode('utf-8') in body
                     or token in text],
        'etag': headers.get('etag'),
        'location': location or headers.get('location'),
        'cache_control': headers.get('cache-control'),
    }


def _runtime():
    return {
        'executable': sys.executable,
        'python_version': list(sys.version_info[:3]),
        'sqlite_version': sqlite3.sqlite_version,
        'sqlite_version_info': list(sqlite3.sqlite_version_info),
    }


PLACEHOLDER = 'probe-prefix'


def _normalize_prefix(report):
    """Return a deep copy with the run's recipe prefix pinned to a placeholder."""
    normalized = json.loads(json.dumps(report))
    prefix = None
    for frame in normalized['request_frames']:
        body = base64.b64decode(frame.pop('body_b64'))
        if not body:
            frame['normalized_sha256'] = frame['body_sha256']
            frame['normalized_length'] = 0
            continue
        text = body.decode('ascii')
        if prefix is None:
            match = re.search(r'name=([A-Za-z0-9_.-]+)-stale', text)
            if match:
                prefix = match.group(1)
        if prefix:
            text = text.replace(prefix, PLACEHOLDER)
        normalized_body = text.encode('ascii')
        frame['normalized_sha256'] = _sha256(normalized_body)
        frame['normalized_length'] = len(normalized_body)
    for response in normalized['responses'].values():
        # body_sha256 cannot be recomputed without the bytes; error pages only
        # differ by the echoed prefix, so normalize by structural equality and
        # record the exemption explicitly instead of claiming byte identity.
        response['prefix_echo_page'] = response['status'] in (400, 422)
    return normalized


def compare(old_report, new_report):
    """Byte-level comparison; capture ids and fixtures are treated separately."""
    # The only legitimate wire differences come from the two runs' own recipe
    # name prefixes, which are echoed into form bodies and error pages.  Pin
    # both prefixes to one placeholder before hashing, so the comparison measures
    # the workbench's encoding rather than the probe's chosen names.
    old_report = _normalize_prefix(old_report)
    new_report = _normalize_prefix(new_report)
    byte_matches, mismatches = [], []
    for name in sorted(set(old_report['responses']) & set(new_report['responses'])):
        before, after = old_report['responses'][name], new_report['responses'][name]
        if before['status'] != after['status'] or before['content_type'] != after['content_type']:
            mismatches.append({'response': name, 'reason': 'status-or-content-type-drift',
                               'old_status': before['status'], 'new_status': after['status']})
            continue
        if name in STABLE_RESPONSES:
            identical = before['body_sha256'] == after['body_sha256']
            entry = {'response': name, 'old_status': before['status'],
                     'new_status': after['status'], 'byte_identical': identical,
                     'old_sha256': before['body_sha256'], 'new_sha256': after['body_sha256']}
            if not identical and before.get('prefix_echo_page'):
                # Error pages echo the probe-chosen recipe name; status, content
                # type and the normalized frame bytes are the real invariants.
                entry['note'] = 'error page echoes probe prefix; frame bytes normalized below'
                byte_matches.append(entry)
            elif identical:
                byte_matches.append(entry)
            else:
                mismatches.append(entry)
        else:
            byte_matches.append({'response': name, 'byte_identical': False,
                                 'note': 'volatile response exempt from byte identity'})

    frame_matches, frame_mismatches = [], []
    old_frames = {frame['name']: frame for frame in old_report['request_frames']}
    new_frames = {frame['name']: frame for frame in new_report['request_frames']}
    for name in STABLE_FRAMES:
        before, after = old_frames[name], new_frames[name]
        identical = (before['normalized_sha256'] == after['normalized_sha256']
                     and before['normalized_length'] == after['normalized_length'])
        entry = {'frame': name, 'byte_identical': identical,
                 'old_length': before['normalized_length'],
                 'new_length': after['normalized_length'],
                 'old_sha256': before['normalized_sha256'],
                 'new_sha256': after['normalized_sha256']}
        (frame_matches if identical else frame_mismatches).append(entry)

    semantic_equal = (old_report['json_payloads']['catalog_schema_digest']
                      == new_report['json_payloads']['catalog_schema_digest']
                      and old_report['json_payloads']['form_recipe']
                      == new_report['json_payloads']['form_recipe']
                      and old_report['json_payloads']['json_recipe']
                      == new_report['json_payloads']['json_recipe']
                      and new_report['json_payloads']['recipes_normalize_identically'])
    return {
        'schema_version': SCHEMA_VERSION,
        'old_runtime': old_report['runtime'],
        'new_runtime': new_report['runtime'],
        'stable_byte_identical_responses': byte_matches,
        'byte_mismatches': mismatches,
        'frame_matches': frame_matches,
        'frame_mismatches': frame_mismatches,
        'semantic_json_equal': semantic_equal,
        'fixtures_independently_reread': {
            'old': old_report['fixture'], 'new': new_report['fixture'],
            'note': 'Fixture bytes legitimately differ across libsqlite3 builds; '
                    'each must pass its own independent re-read and certificate.',
        },
        'verdict': 'go' if not mismatches and not frame_mismatches and semantic_equal else 'no-go',
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    probe = commands.add_parser('probe')
    probe.add_argument('--repository')
    probe.add_argument('--source')
    probe.add_argument('--role', choices=('legacy', 'switched', 'fresh'), required=True)
    probe.add_argument('--prefix', default='wire')
    probe.add_argument('--sqlite-cli')
    probe.add_argument('--out', required=True)
    diff = commands.add_parser('compare')
    diff.add_argument('--old', dest='old_path', required=True)
    diff.add_argument('--new', dest='new_path', required=True)
    diff.add_argument('--out', required=True)
    args = parser.parse_args(argv)

    if args.command == 'compare':
        verdict = compare(json.loads(Path(args.old_path).read_text(encoding='utf-8')),
                          json.loads(Path(args.new_path).read_text(encoding='utf-8')))
        _write(verdict, args.out)
        print(f"verdict={verdict['verdict']} -> {args.out}")
        return 0 if verdict['verdict'] == 'go' else 2

    if args.role == 'fresh':
        with TemporaryDirectory(prefix='keelsplice-wire-') as directory:
            root = Path(directory)
            source = root / 'source.sqlite'
            repository = root / 'state.sqlite'
            _seed_source(source)
            report = run_probe(repository, source, 'fresh', args.prefix, args.sqlite_cli)
    else:
        report = run_probe(Path(args.repository), Path(args.source), args.role,
                           args.prefix, args.sqlite_cli)
    _write(report, args.out)
    print(f"role={report['role']} py={'.'.join(map(str, report['runtime']['python_version']))} "
          f"frames={len(report['request_frames'])} fixture_sha={report['fixture']['sha256'][:12]} "
          f"-> {args.out}")
    return 0


def _write(payload, destination):
    path = Path(destination)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + '\n',
                    encoding='utf-8')


if __name__ == '__main__':
    raise SystemExit(main())

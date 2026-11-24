"""Build and replay the night-shift handover drill.

The builder drives the real loopback JSON and workbench service against a copy
of the public ``examples/microscope.sql`` sample. It leaves a product
repository with recipes and sealed captures, labelled source snapshots, a shift
journal, raw observations and an integrity manifest under
``examples/handover-night/material``.

The replay path is offline and never mutates the delivered material. It
verifies file checksums, recomputes every sealed capture from its labelled
snapshot with the production closure code, separates captures that look
identical, completes every sealed capture on a temporary repository copy and
compares the derived handover verdicts with ``expected-verdicts.json``.

Usage:
  python3 -m examples.handover_night build [--material DIR] [--force]
  python3 -m examples.handover_night replay [--material DIR] [--report FILE] [--probe]
"""

import argparse
import json
import re
import shutil
import socket
import sqlite3
import struct
import sys
import tempfile
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from threading import Thread

from keelsplice import capture as selection
from keelsplice.api import RequestHandler, create_server
from keelsplice.codec import fingerprint, loads

from .handover_common import (
    AFTER_RETARGET, AFTER_VALUE, ATTIC, EXPECTED, LABELS, MATERIAL, PUBLIC,
    RELAY, SNAPSHOTS, SAMPLE, DrillError, appearance_counts, jdumps, mutate,
    readonly, require, sealed_from_snapshot, seed_source, sha_bytes, sha_file,
    snapshot, summarize, write_json,
)

DROPPED = 'dropped-response.json'
PREVIEWS = 'previews.json'
CAPTURE_PAGE = 'captures-page.html'
LOST_LABEL = 'relay-lost'
RETRY_LABEL = 'relay-retry'

CAPTURE_KEYS = ('id', 'name', 'version', 'state', 'schema_digest', 'summary', 'roots')


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


class Journal:
    def __init__(self):
        self.events = []

    def add(self, actor, action, request=None, response=None, evidence=None, note=''):
        event = {'seq': len(self.events) + 1, 'at': utc_now(), 'actor': actor, 'action': action}
        if request is not None:
            event['request'] = request
        if response is not None:
            event['response'] = response
        if evidence is not None:
            event['evidence'] = evidence
        if note:
            event['note'] = note
        self.events.append(event)
        return event

    def write(self, path):
        Path(path).write_text(
            ''.join(jdumps(event) + '\n' for event in self.events), encoding='utf-8')


class Client:
    def __init__(self, base, journal):
        self.base = base
        self.journal = journal

    def call(self, actor, action, method, path, body=None, status=200,
             keep=None, note=''):
        from urllib.error import HTTPError
        from urllib.request import Request, urlopen
        data = None if body is None else json.dumps(body).encode('utf-8')
        headers = {'Content-Type': 'application/json'} if data is not None else {}
        request = Request(self.base + path, data=data, headers=headers, method=method)
        try:
            response = urlopen(request, timeout=5)
        except HTTPError as exc:
            response = exc
        with response:
            raw = response.read()
            actual = response.status
        payload = json.loads(raw)
        require(actual == status, f'{path} returned {actual}, expected {status}: {raw[:300]!r}')
        observed = {key: payload[key] for key in keep} if keep else payload
        self.journal.add(
            actor=actor, action=action,
            request={'method': method, 'path': path,
                     **({'body': body} if body is not None else {})},
            response={'status': actual, 'observed': True, 'body': observed},
            evidence={'kind': 'http', 'observed': 'response'}, note=note)
        return payload

    def page(self, actor, action, path, note=''):
        from urllib.request import urlopen
        with urlopen(self.base + path, timeout=5) as response:
            raw = response.read()
            require(response.status == 200, f'{path} returned {response.status}')
        self.journal.add(
            actor=actor, action=action, request={'method': 'GET', 'path': path},
            response={'status': 200, 'observed': True,
                      'body': {'content_type': 'text/html', 'sha256': sha_bytes(raw)}},
            evidence={'kind': 'http', 'observed': 'response'}, note=note)
        return raw.decode('utf-8')


def parse_capture_ids(html):
    return sorted(set(re.findall(r'href="/workbench/captures/([0-9a-f]{32})"', html)))


def capture_ids(repo):
    with readonly(repo) as conn:
        return {row[0] for row in conn.execute('SELECT id FROM captures')}


def post_without_reading_response(port, path):
    """Send a real workbench form and reset the socket before reading its reply."""
    request = (f'POST {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n'
               f'Origin: http://127.0.0.1:{port}\r\n'
               'Content-Type: application/x-www-form-urlencoded\r\n'
               'Content-Length: 0\r\nConnection: close\r\n\r\n').encode('ascii')
    sock = socket.create_connection(('127.0.0.1', port), timeout=5)
    sock.sendall(request)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack('ii', 1, 0))
    sock.close()
    return request


def wait_for_new_capture(repo, before, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        new = capture_ids(repo) - before
        if len(new) == 1:
            return new.pop()
        if len(new) > 1:
            raise DrillError('the dropped response created more than one capture')
        time.sleep(0.02)
    raise DrillError('the dropped response did not leave a capture record')


def repository_rows(repo, statement, params=()):
    with readonly(repo) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(row) for row in conn.execute(statement, params)]


def checkpoint_repository(repo):
    with closing(sqlite3.connect(repo)) as conn:
        conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        conn.execute('PRAGMA journal_mode=DELETE')
    for suffix in ('-wal', '-shm'):
        extra = Path(str(repo) + suffix)
        if extra.exists():
            extra.unlink()


def build(material, force):
    material = Path(material).resolve()
    if material.exists():
        if not force:
            raise DrillError(f'{material} already exists; pass --force to rebuild')
        shutil.rmtree(material)
    base_material = material.parent
    sources = material / 'sources'
    observations = material / 'observations'
    state = material / 'state'
    for directory in (sources, observations, state):
        directory.mkdir(parents=True)
    journal = Journal()
    RequestHandler.log_message = lambda *args, **kwargs: None

    with tempfile.TemporaryDirectory(prefix='keelsplice-drill-') as temporary:
        work = Path(temporary) / 'source.sqlite'
        seed_source(work)
        snapshot(work, sources / SNAPSHOTS[PUBLIC])
        repo = state / 'repository.sqlite'

        with create_server(('127.0.0.1', 0), repo, {'microscope': work}) as server:
            thread = Thread(target=server.serve_forever, kwargs={'poll_interval': 0.02})
            thread.start()
            port = server.server_port
            base = f'http://127.0.0.1:{port}'
            client = Client(base, journal)
            try:
                disclosed = client.call('night-a', 'catalog', 'GET',
                                        '/sources/microscope/catalog',
                                        keep=('schema_digest', 'schema'))
                edge = next(item for item in disclosed['schema']['edges']
                            if item['child'] == 'assay' and item['parent'] == 'substrate')
                require(edge['parent_cols'] == ['region', 'code'],
                        'the public sample foreign key shape changed')
                relay = {'source': 'microscope',
                         'roots': [{'table': 'substrate', 'key': ['east', 'MICA']}],
                         'follow_children': [{'child': 'assay', 'id': edge['id']}],
                         'row_limit': 20}
                attic = {'source': 'microscope',
                         'roots': [{'table': 'substrate', 'key': ['West', 'Basalt']}],
                         'follow_children': [{'child': 'assay', 'id': edge['id']}],
                         'row_limit': 20}
                client.call('night-a', 'register-relay', 'POST', '/recipes/relay/versions',
                            relay, 201,
                            keep=('name', 'version', 'active', 'schema_digest', 'recipe'))
                client.call('night-a', 'register-attic', 'POST', '/recipes/attic/versions',
                            attic, 201,
                            keep=('name', 'version', 'active', 'schema_digest', 'recipe'))

                previews = []

                def preview(actor, action, note):
                    payload = client.call(actor, action, 'POST', '/preview',
                                          {'name': RELAY, 'version': 1},
                                          keep=('name', 'version', 'schema_digest',
                                                'summary', 'roots'), note=note)
                    previews.append({'action': action, 'summary': payload['summary'],
                                     'roots': payload['roots'],
                                     'schema_digest': payload['schema_digest'], 'note': note})
                    return payload

                first = preview('night-a', 'preview-before-value',
                                'First rehearsal. No sealed record exists yet.')
                early = client.call('night-a', 'capture-early', 'POST', '/captures',
                                    {'name': RELAY, 'version': 1}, 201, keep=CAPTURE_KEYS)
                legacy = client.call('night-a', 'capture-attic-legacy', 'POST', '/captures',
                                     {'name': ATTIC, 'version': 1}, 201, keep=CAPTURE_KEYS)
                client.call('night-a', 'retire-attic', 'POST',
                            '/recipes/attic/versions/1/retire', {},
                            keep=('name', 'version', 'active'))
                client.call('night-a', 'capture-attic-retired', 'POST', '/captures',
                            {'name': ATTIC, 'version': 1}, 409,
                            note='Retirement is rechecked inside the sealing write.')

                mutate(work, "UPDATE assay SET length_um=4.75 WHERE mark='S-01'")
                snapshot(work, sources / SNAPSHOTS[AFTER_VALUE])
                second = preview('night-a', 'preview-after-value',
                                 'Same row counts as the first rehearsal; a stored value moved.')
                require(appearance_counts(first) == appearance_counts(second),
                        'the two rehearsals were expected to report equal row counts')

                before = capture_ids(repo)
                path = f'/workbench/recipes/{RELAY}/1/capture'
                request_bytes = post_without_reading_response(port, path)
                lost_id = wait_for_new_capture(repo, before)
                lost_row = client.call('successor-b', 'read-lost-status', 'GET',
                                       f'/captures/{lost_id}', 200, keep=CAPTURE_KEYS,
                                       note='Found in the repository although no page reply was read.')
                lost = {
                    'label': LOST_LABEL,
                    'capture_id': lost_id,
                    'request': {'method': 'POST', 'path': path,
                                'headers': {'Origin': base,
                                            'Content-Type': 'application/x-www-form-urlencoded',
                                            'Content-Length': '0'},
                                'raw_sha256': sha_bytes(request_bytes)},
                    'client_observation': {
                        'response_status': None, 'response_body': None,
                        'detail': 'Socket reset after the request. The operator never saw a '
                                  'confirmation page and cannot quote a capture id from it.'},
                    'server_side_evidence': {
                        'repository_row_present': True,
                        'discovered_by': 'capture id set difference in the same repository',
                        'state': lost_row['state'],
                        'summary': lost_row['summary'],
                        'roots': lost_row['roots']},
                }
                # Record the lost attempt in the shift journal itself, from the
                # operator's side: the request left the client, no response came back.
                journal.add(
                    actor='night-a', action='capture-lost-page',
                    request={'method': 'POST', 'path': path,
                             'raw_sha256': lost['request']['raw_sha256']},
                    response=None,
                    evidence={'kind': 'http', 'observed': 'no-response'},
                    note='Seal request sent from the workbench; connection reset before any '
                         'page response could be read.')

                duplicate = client.call('successor-b', 'capture-retry', 'POST', '/captures',
                                        {'name': RELAY, 'version': 1}, 201, keep=CAPTURE_KEYS,
                                        note='Blind retry after the lost page response; '
                                             'the retry does not know the first record exists.')
                page_html = client.page('successor-b', 'open-capture-list', '/workbench/captures',
                                        note='Reconcile the browser list with the repository.')
                listed = parse_capture_ids(page_html)
                for expected_id in (lost_id, duplicate['id']):
                    require(expected_id in listed, 'the capture list is missing a sealed record')
                lost['reconciled'] = {
                    'listed_ids': listed, 'duplicate_id': duplicate['id'],
                    'note': 'The lost record and the blind retry both appear in one list; '
                            'only the stable repository identifier and this journal tell them apart.'}
                write_json(observations / DROPPED, lost)

                mutate(work, "UPDATE assay SET region='West', code='Basalt' WHERE mark='S-02'")
                snapshot(work, sources / SNAPSHOTS[AFTER_RETARGET])
                third = preview('successor-b', 'preview-after-retarget',
                                'The relationship target moved. Only this live rehearsal exists; '
                                'nothing was sealed for it.')
                require(appearance_counts(first) != appearance_counts(third),
                        'the retarget rehearsal was expected to change row counts')
                client.call('successor-b', 'capture-attic-after-retarget', 'POST', '/captures',
                            {'name': ATTIC, 'version': 1}, 409,
                            note='The retired version still refuses new work after the move.')
            finally:
                server.shutdown()
                thread.join()

        checkpoint_repository(repo)
        write_json(observations / PREVIEWS,
                   {'previews': previews,
                    'equal_counts': appearance_counts(first) == appearance_counts(second),
                    'counts': {'first': appearance_counts(first),
                               'second': appearance_counts(second),
                               'third': appearance_counts(third)}})
        (observations / CAPTURE_PAGE).write_text(page_html, encoding='utf-8')

        rows = repository_rows(repo, 'SELECT id,name,version,state,sealed FROM captures ORDER BY rowid')
        ids = {event['action']: (event.get('response') or {}).get('body', {}).get('id')
               for event in journal.events if event['action'].startswith('capture-')}
        expect_ids = {early['id'], legacy['id'], lost_id, duplicate['id']}
        require({row['id'] for row in rows} == expect_ids,
                f'repository captures differ from the drill: {sorted(expect_ids)}')
        label_of = {early['id']: 'relay-early', legacy['id']: 'attic-legacy',
                    lost_id: LOST_LABEL, duplicate['id']: RETRY_LABEL}
        snapshot_of = {'relay-early': PUBLIC, LOST_LABEL: AFTER_VALUE,
                       RETRY_LABEL: AFTER_VALUE, 'attic-legacy': PUBLIC}
        captures = []
        for row in rows:
            label = label_of[row['id']]
            sealed = loads(row['sealed'])
            traces = {}
            values = {}
            for table, tokens in sealed['result']['rows'].items():
                for token in sorted(tokens):
                    key = json.loads(token)
                    traces[f'{table}:{token}'] = selection.trace(
                        sealed, {'table': table, 'key': key})
                    values[f'{table}:{token}'] = fingerprint(tokens[token])
            captures.append({
                'label': label, 'id': row['id'], 'name': row['name'],
                'version': row['version'], 'state': row['state'],
                'snapshot': snapshot_of[label], 'schema_digest': sealed['schema_digest'],
                'summary': summarize(sealed), 'roots': sealed['result']['roots'],
                'capture_digest': fingerprint(sealed), 'recipe': sealed['recipe'],
                'trace': traces, 'row_values': values,
            })
        by_label = {item['label']: item for item in captures}
        require(by_label[LOST_LABEL]['trace'] == by_label[RETRY_LABEL]['trace'] and
                by_label[LOST_LABEL]['row_values'] == by_label[RETRY_LABEL]['row_values'],
                'the lost record and the retry must agree on both relation and value evidence')
        require(by_label['relay-early']['trace'] == by_label[LOST_LABEL]['trace'],
                'the relation evidence is expected to be stable across a value-only change')
        require(by_label['relay-early']['row_values'] != by_label[LOST_LABEL]['row_values'],
                'the early record and the later record must differ by stored row values')
        recipes = []
        for item in repository_rows(repo, 'SELECT name,version,active,schema_digest,recipe '
                                          'FROM recipes ORDER BY rowid'):
            item['recipe'] = loads(item['recipe'])
            item['active'] = bool(item['active'])
            recipes.append(item)

        relays = next(item['recipe'] for item in captures if item['label'] == 'relay-early')
        snapshots = []
        for key in SNAPSHOTS:
            recomputed = sealed_from_snapshot(sources / SNAPSHOTS[key], relays)
            snapshots.append({'key': key, 'file': SNAPSHOTS[key],
                              'sha256': sha_file(sources / SNAPSHOTS[key]),
                              'relay_v1_summary': summarize(recomputed)})

        manifest = {
            'drill': 'keelsplice-night-handover-v1',
            'built_at': utc_now(),
            'sample': {'path': 'examples/microscope.sql', 'sha256': sha_file(SAMPLE)},
            'source_alias': 'microscope',
            'snapshots': snapshots,
            'previews': previews,
            'recipes': recipes,
            'captures': captures,
            'lost_response': {'label': LOST_LABEL, 'capture_id': lost_id,
                              'observation_file': DROPPED, 'page_response_observed': False},
            'unsealed_work': {
                'relay-retargeted-closure': {
                    'preview_action': 'preview-after-retarget',
                    'summary': third['summary'],
                    'note': 'This rehearsal was never sealed; the source has already moved on.'},
            },
            'retirement': {
                'recipe': ATTIC, 'version': 1,
                'refusals': [event['response']['body']['error'] for event in journal.events
                             if event['action'].startswith('capture-attic') and
                             event['response']['status'] == 409]},
            'labels': list(LABELS),
        }
        journal.write(material / 'journal.jsonl')
        manifest['material_files'] = {
            str(path.relative_to(material)): sha_file(path)
            for path in sorted(material.rglob('*'))
            if path.is_file() and path.name != 'manifest.json'}
        write_json(material / 'manifest.json', manifest)

        expectations = {
            'drill': manifest['drill'],
            'captures': {
                'relay-early': {'verdict': 'recover',
                                'why': 'unique sealed record; recomputes byte-for-byte from '
                                       'snapshot 00-public-sample'},
                'relay-lost': {'verdict': 'recover',
                               'why': 'sealed record exists although its page response was '
                                      'lost; the journal shows the night-a request and the '
                                      'repository row, and it recomputes from snapshot 01'},
                'attic-legacy': {'verdict': 'recover',
                                 'why': 'sealed before retire; still readable and completable '
                                        'after retirement'},
                'relay-retry': {'verdict': 'candidate',
                                'why': 'identical content to relay-lost but no evidence ties '
                                       'it to the original operator; it proves a candidate '
                                       'sample only'},
            },
            'requested_work': {
                'relay-retargeted-closure': {'verdict': 'reread',
                                             'why': 'only a live preview exists after the '
                                                    'relationship target moved; a preview is not '
                                                    'a record'},
                'attic-fresh-sample': {'verdict': 'reread',
                                       'why': 'attic v1 is retired and cannot seal; a new '
                                              'sample needs a fresh source read and a new version'},
            },
        }
        write_json(base_material / EXPECTED.name, expectations)
        print(f'built {material}')
        for item in captures:
            print(f"  {item['label']:14s} {item['id']}  rows={item['summary']['rows']}")
        print(f"  manifest {material / 'manifest.json'}")
        print(f"  expected {base_material / EXPECTED.name}")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    builder = sub.add_parser('build')
    builder.add_argument('--material', default=str(MATERIAL))
    builder.add_argument('--force', action='store_true')
    replayer = sub.add_parser('replay')
    replayer.add_argument('--material', default=str(MATERIAL))
    replayer.add_argument('--report')
    replayer.add_argument('--probe', action='store_true',
                          help='also serve the delivered repository and probe it over HTTP')
    args = parser.parse_args(argv)
    if args.command == 'build':
        build(args.material, args.force)
        return 0
    from .handover_replay import VERDICT_FILE, replay
    from .handover_common import write_json
    report_path = Path(args.report) if args.report else Path(args.material).parent / VERDICT_FILE
    report = replay(Path(args.material), report_path)
    if report['ok'] and args.probe:
        from .handover_probe import print_probe, probe
        try:
            report['live_probe'] = probe(Path(args.material))
            write_json(report_path, report)
            print_probe(report['live_probe'])
        except DrillError as exc:
            print(f'probe failed: {exc}')
            return 1
    return 0 if report['ok'] else 1


if __name__ == '__main__':
    sys.exit(main())

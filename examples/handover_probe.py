"""Serve the delivered drill repository and probe it over real HTTP.

The probe copies the delivered repository into a temporary directory, registers
the labelled source snapshots read-only and drives the public JSON and workbench
endpoints. It asserts the same handover facts the offline replay proves, but
through the wire format a successor would actually use.
"""

import json
import re
import shutil
import tempfile
from pathlib import Path
from threading import Thread
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from keelsplice.api import RequestHandler, create_server

from .handover_common import AFTER_RETARGET, DrillError, MATERIAL, require, sha_bytes

LOST = 'relay-lost'
RETRY = 'relay-retry'


def call(base, method, path, body=None, status=200):
    data = None if body is None else json.dumps(body).encode('utf-8')
    headers = {'Content-Type': 'application/json'} if data is not None else {}
    request = Request(base + path, data=data, headers=headers, method=method)
    try:
        response = urlopen(request, timeout=5)
    except HTTPError as exc:
        response = exc
    with response:
        raw = response.read()
        kind = response.headers.get_content_type()
        actual = response.status
        etag = response.headers.get('ETag')
    require(actual == status, f'{path} returned {actual}, expected {status}: {raw[:300]!r}')
    if kind == 'application/vnd.sqlite3':
        return raw, etag
    return json.loads(raw)


def page(base, path):
    with urlopen(base + path, timeout=5) as response:
        require(response.status == 200, f'{path} returned {response.status}')
        return response.read().decode('utf-8')


def probe(material=MATERIAL):
    material = Path(material).resolve()
    manifest = json.loads((material / 'manifest.json').read_text(encoding='utf-8'))
    ids = {entry['label']: entry['id'] for entry in manifest['captures']}
    RequestHandler.log_message = lambda *args, **kwargs: None
    results = {}
    with tempfile.TemporaryDirectory(prefix='keelsplice-probe-') as temporary:
        repo = Path(temporary) / 'repository.sqlite'
        shutil.copy2(material / 'state' / 'repository.sqlite', repo)
        sources = {'microscope': material / 'sources' / f'{AFTER_RETARGET}.sqlite'}
        with create_server(('127.0.0.1', 0), repo, sources) as server:
            thread = Thread(target=server.serve_forever, kwargs={'poll_interval': 0.02})
            thread.start()
            base = f'http://127.0.0.1:{server.server_port}'
            try:
                for label, capture_id in ids.items():
                    status = call(base, 'GET', f'/captures/{capture_id}')
                    require(status['state'] == 'sealed', f'{label} is not sealed on delivery')
                results['every_capture_is_sealed'] = sorted(ids)

                same = call(base, 'POST', '/compare',
                            {'left': ids[LOST], 'right': ids[RETRY]})
                require(all(not any(part.values()) for part in same['changes'].values()),
                        'the lost record and the blind retry are not identical over HTTP')
                results['lost_and_retry_compare_identical'] = True

                early_vs_lost = call(base, 'POST', '/compare',
                                     {'left': ids['relay-early'], 'right': ids[LOST]})
                modified = early_vs_lost['changes']['assay']['modified']
                require(modified == [['S-01']],
                        f'expected exactly S-01 to differ by value, saw {modified}')
                results['early_and_lost_differ_by_value'] = modified

                trace = call(base, 'POST', f'/captures/{ids[LOST]}/trace',
                             {'table': 'assay', 'key': ['S-01']})
                retry_trace = call(base, 'POST', f'/captures/{ids[RETRY]}/trace',
                                   {'table': 'assay', 'key': ['S-01']})
                require(trace == retry_trace,
                        'the lost record and the retry resolve different evidence')
                require(call(base, 'POST', f'/captures/{ids[LOST]}/trace',
                             {'table': 'assay', 'key': ['S-04']}, 404)['error'] == 'row_not_captured',
                        'the lost record unexpectedly holds a row from another closure')
                results['trace_evidence'] = trace

                html = page(base, '/workbench/captures')
                require(ids[LOST] in html and ids[RETRY] in html,
                        'the browser list does not show both competing records')
                results['browser_lists_both'] = True

                refused = call(base, 'POST', '/captures', {'name': 'attic', 'version': 1}, 409)
                require(refused['error'] == 'retired_recipe',
                        f'retired recipe did not refuse: {refused}')
                results['retired_recipe_refuses_new_capture'] = refused['error']

                early = call(base, 'GET', f'/captures/{ids["relay-early"]}/database', None, 409)
                require(early['error'] == 'fixture_not_certified',
                        'download was allowed before certification')
                results['download_requires_certification'] = early['error']

                call(base, 'POST', f'/captures/{ids[LOST]}/build', {})
                certified = call(base, 'POST', f'/captures/{ids[LOST]}/certify', {})
                data, etag = call(base, 'GET', f'/captures/{ids[LOST]}/database')
                require(etag == f'"{sha_bytes(data)}"',
                        'ETag does not match the delivered bytes')
                require(etag.strip('"') == certified['certificate']['sha256'],
                        'certificate does not bind the delivered bytes')
                results['lost_record_completes'] = {
                    'state': certified['state'], 'counts': certified['certificate']['counts'],
                    'sha256': etag.strip('"')}
            finally:
                server.shutdown()
                thread.join()
    return results


def print_probe(results):
    print('probe ok: True')
    for key, value in results.items():
        print(f'  [ok] {key}: {json.dumps(value, ensure_ascii=False)[:120]}')


if __name__ == '__main__':
    raise SystemExit('run through examples.handover_night')

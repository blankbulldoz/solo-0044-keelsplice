"""End-to-end HTTP cost of the browser review loop, as shipped today.

Starts the real server from ``keelsplice.api``, seals a capture through the
JSON API, then loads the capture detail page and follows several trace links
through the HTML routes.  Each request is measured with its own connection,
matching a browser.  No product code is patched.
"""

from __future__ import annotations

import argparse
import gc
import json
import re
import sqlite3
import statistics
import sys
import tempfile
from contextlib import closing
from pathlib import Path
from threading import Thread
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from keelsplice.api import create_server

from . import fixtures
from .harness import Reader, edge_ids

REPS = 5


def _get(base, path):
    with urlopen(base + path, timeout=30) as response:
        return response.read()


def _post(base, path, payload):
    body = urlencode(payload, doseq=True).encode('ascii')
    request = Request(base + path, data=body, headers={'Origin': base})
    with urlopen(request, timeout=30) as response:
        return response.read()


def _json_call(base, path, payload=None, method='POST'):
    data = None if payload is None else json.dumps(payload).encode('utf-8')
    request = Request(base + path, data=data, headers={'Content-Type': 'application/json'}, method=method)
    with urlopen(request, timeout=30) as response:
        return json.loads(response.read())


def measure(run):
    run()
    samples = []
    for _ in range(REPS):
        gc.collect()  # same protocol as the in-process harness
        with Reader() as reader:
            run()
        samples.append((reader.elapsed, reader.bytes))
    return {'median_s': statistics.median(s[0] for s in samples),
            'median_read_bytes': statistics.median(s[1] for s in samples)}


def run_case(spec, root: Path):
    source = root / 'source.sqlite'
    fx = fixtures.seed(spec, source)
    with create_server(('127.0.0.1', 0), root / 'repo.sqlite', {'s': source}) as server:
        thread = Thread(target=server.serve_forever, kwargs={'poll_interval': 0.02})
        thread.start()
        base = f'http://127.0.0.1:{server.server_port}'
        try:
            catalog = _json_call(base, '/sources/s/catalog', method='GET')
            body = {'source': 's', 'roots': fx['roots'],
                    'follow_children': edge_ids(catalog, fx['follow_children']),
                    'row_limit': fx['row_limit']}
            _json_call(base, '/recipes/review/versions', body)
            capture = _json_call(base, '/captures', {'name': 'review', 'version': 1})
            capture_id = capture['id']
            detail = f'/workbench/captures/{capture_id}'
            _get(base, detail)  # warm the page route

            trace_payload = {'table': fx['lookups'][0][0], 'key': json.dumps(fx['lookups'][0][1])}
            _post(base, detail + '/trace', trace_payload)
            pages = {
                'detail': measure(lambda: _get(base, detail)),
                'trace': measure(lambda: _post(base, detail + '/trace', trace_payload)),
                'captures_index': measure(lambda: _get(base, '/workbench/captures')),
            }
        finally:
            server.shutdown()
            thread.join()
    return {'name': fx['name'], 'class': fx['class'], 'rows': capture['summary']['rows'],
            'witnesses': capture['summary']['witnesses'],
            'sealed_json_bytes': len(json.dumps(
                sqlite3.connect(root / 'repo.sqlite').execute(
                    'SELECT sealed FROM captures WHERE id=?', (capture_id,)).fetchone()[0])),
            'pages': pages}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--suite', choices=('default', 'sweep'), default='default')
    args = parser.parse_args(argv)
    specs = fixtures.DEFAULT_SUITE if args.suite == 'default' else fixtures.SWEEP_SUITE
    cases = []
    with tempfile.TemporaryDirectory(prefix='keelsplice-http-') as directory:
        for number, spec in enumerate(specs):
            workdir = Path(directory) / f'{number:02d}'
            workdir.mkdir()
            case = run_case(spec, workdir)
            cases.append(case)
            print(f"{case['name']:<14} arcs={case['witnesses']:<6} "
                  f"detail={case['pages']['detail']['median_read_bytes']:>9.0f}B/"
                  f"{case['pages']['detail']['median_s']*1e3:>8.2f}ms "
                  f"trace={case['pages']['trace']['median_read_bytes']:>9.0f}B/"
                  f"{case['pages']['trace']['median_s']*1e3:>8.2f}ms")
    if args.output:
        args.output.write_text(json.dumps({'generated_by': 'evaluation/http_cost.py',
                                           'reps': REPS, 'cases': cases},
                                          ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        print(f'wrote {args.output}')
    return 0


if __name__ == '__main__':
    sys.exit(main())

"""Stress the job registry: many concurrent previews with interleaved cancels."""

import sys
import threading
import time
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from keelsplice.api import create_server
from test_preview_cancel import seed_fanout, wait_terminal  # noqa: E402
import urllib.request, json  # noqa: E402


def call(base, route, body=None):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(base + route, data=data,
                                 headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=15) as r:
        return r.status, json.loads(r.read())


def main():
    with TemporaryDirectory() as d:
        root = Path(d)
        source = root / 'fan.sqlite'
        seed_fanout(source, children=800)
        srv = create_server(('127.0.0.1', 0), root / 'state.sqlite', {'fanout': source})
        t = threading.Thread(target=srv.serve_forever, kwargs={'poll_interval': 0.02})
        t.start()
        base = f'http://127.0.0.1:{srv.server_port}'
        try:
            edge = call(base, '/sources/fanout/catalog')[1]['schema']['edges'][0]
            call(base, '/recipes/fan/versions',
                 {'source': 'fanout', 'roots': [{'table': 'root', 'key': [1]}],
                  'follow_children': [{'child': 'leaf', 'id': edge['id']}], 'row_limit': 5000})
            lookup = {'name': 'fan', 'version': 1}

            ids = []
            for _ in range(12):
                _, job = call(base, '/preview/jobs', lookup)
                ids.append(job['id'])
            # Cancel roughly half while the rest run to completion.
            cancelled_ids = set(ids[::2])
            for jid in cancelled_ids:
                call(base, f'/preview/jobs/{jid}/cancel', {})

            outcomes = {}
            for jid in ids:
                rec = wait_terminal(base, jid, timeout=30)
                outcomes[jid] = rec['state']

            assert all(s in ('completed', 'cancelled') for s in outcomes.values()), outcomes
            # Jobs never targeted by a cancel cannot end any way but completed.
            for jid in ids:
                if jid not in cancelled_ids:
                    assert outcomes[jid] == 'completed', (jid, outcomes[jid])
            # Cancel-targeted jobs are either still running when cancelled, or had already
            # finished first: a late cancel must never overwrite that completion.
            for jid in cancelled_ids:
                assert outcomes[jid] in ('cancelled', 'completed'), (jid, outcomes[jid])
            n_cancelled = sum(1 for jid in cancelled_ids if outcomes[jid] == 'cancelled')
            assert n_cancelled > 0, 'expected at least one in-flight cancellation'
            # Terminal states are stable on repeat reads and repeat cancels.
            for jid in ids:
                first = call(base, f'/preview/jobs/{jid}')[1]['state']
                if jid in cancelled_ids:
                    call(base, f'/preview/jobs/{jid}/cancel', {})
                assert call(base, f'/preview/jobs/{jid}')[1]['state'] == first == outcomes[jid]
            # Every job id is unique.
            assert len(set(ids)) == len(ids)
            # A brand-new job is unaffected by all the prior cancellations.
            _, fresh = call(base, '/preview/jobs', lookup)
            assert wait_terminal(base, fresh['id'], timeout=30)['state'] == 'completed'
        finally:
            srv.shutdown()
            t.join()
            srv.server_close()
    print(f'STRESS OK: 12 concurrent jobs, {n_cancelled} in-flight cancels, late cancels preserved completions')


if __name__ == '__main__':
    main()

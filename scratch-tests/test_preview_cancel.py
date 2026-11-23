"""Scenario checks for cancellable high-fanout preview jobs.

These tests deliberately control commit/finish ordering with the Workbench.preview_gate
seam so every race in the task description is exercised deterministically:

  1. cancel while running -> cancelled, read transaction released
  2. late cancel after completion -> completed result is not masked
  3. restart while running -> inconclusive, conclusion lost
  4. two users / two jobs on the same recipe, cancel one -> the other completes
  5. concurrent formal capture is unaffected by a preview cancel
  6. a fresh job never inherits a previous cancellation
"""

import json
import sqlite3
import sys
import threading
import time
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.error import HTTPError
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from keelsplice.api import create_server  # noqa: E402


def seed_fanout(file, children=4000):
    """One root with a large reverse fanout so closure work spans many SQL steps."""
    with closing(sqlite3.connect(file)) as conn:
        conn.executescript(
            """
            CREATE TABLE root (id INTEGER PRIMARY KEY, label TEXT NOT NULL);
            CREATE TABLE leaf (
                id INTEGER PRIMARY KEY, root_id INTEGER NOT NULL REFERENCES root,
                payload TEXT NOT NULL
            );
            CREATE INDEX leaf_root ON leaf(root_id);
            """
        )
        with conn:
            conn.execute('INSERT INTO root VALUES (1, "anchor")')
            conn.executemany('INSERT INTO leaf(root_id, payload) VALUES (1, ?)',
                             ((f'leaf-{i}',) for i in range(children)))


def call(base, route, body=None, status=200):
    headers = {'Content-Type': 'application/json'}
    data = None if body is None else json.dumps(body).encode('utf-8')
    try:
        response = urlopen(Request(base + route, data, headers), timeout=10)
    except HTTPError as exc:
        response = exc
    with response:
        payload = response.read()
        assert response.status == status, (route, response.status, payload[:400])
        return json.loads(payload)


def wait_terminal(base, job_id, timeout=15):
    deadline = time.time() + timeout
    while time.time() < deadline:
        record = call(base, f'/preview/jobs/{job_id}')
        if record['state'] != 'running':
            return record
        time.sleep(0.02)
    raise AssertionError(f'job {job_id} did not settle')


class Gate:
    """Block a preview worker inside its open read transaction until released."""

    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()
        self.controls = {}
        self._lock = threading.Lock()

    def __call__(self, job_id, conn, control):
        with self._lock:
            self.controls[job_id] = (conn, control)
        self.entered.set()
        self.release.wait(timeout=15)


def main():
    with TemporaryDirectory(prefix='keelsplice-cancel-') as directory:
        root = Path(directory)
        source = root / 'fanout.sqlite'
        seed_fanout(source)
        server = create_server(('127.0.0.1', 0), root / 'state.sqlite', {'fanout': source})
        bench = server.workbench
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.02})
        thread.start()
        base = f'http://127.0.0.1:{server.server_port}'
        try:
            edge = call(base, '/sources/fanout/catalog')['schema']['edges']
            assert edge and edge[0]['child'] == 'leaf'
            recipe_body = {'source': 'fanout', 'roots': [{'table': 'root', 'key': [1]}],
                           'follow_children': [{'child': 'leaf', 'id': edge[0]['id']}],
                           'row_limit': 5000}
            call(base, '/recipes/fan/versions', recipe_body, 201)
            lookup = {'name': 'fan', 'version': 1}

            # --- Scenario 1: cancel while the read transaction is open -------------
            gate = Gate()
            bench.preview_gate = gate
            job = call(base, '/preview/jobs', lookup, 202)
            assert job['state'] == 'running'
            assert gate.entered.wait(10), 'worker never entered the gated transaction'
            held_conn, control = gate.controls[job['id']]
            cancelled = call(base, f'/preview/jobs/{job["id"]}/cancel', {})
            assert cancelled['state'] == 'cancelled' and cancelled['detail'] is None
            gate.release.set()
            settled = wait_terminal(base, job['id'])
            assert settled['state'] == 'cancelled', settled
            # The interrupted read transaction's own connection is closed (snapshot released).
            closed = False
            try:
                held_conn.execute('SELECT 1')
            except sqlite3.ProgrammingError:
                closed = True
            assert closed, 'cancelled preview did not release its source connection'
            # idempotent repeat cancel keeps the same durable outcome
            again = call(base, f'/preview/jobs/{job["id"]}/cancel', {})
            assert again['state'] == 'cancelled'
            print('scenario 1 ok: running preview cancelled, transaction released')

            # --- Scenario 2: late cancel must not mask a completed result ----------
            bench.preview_gate = None
            done_job = call(base, '/preview/jobs', lookup, 202)
            done = wait_terminal(base, done_job['id'])
            assert done['state'] == 'completed'
            assert done['detail']['summary']['counts'] == {'root': 1, 'leaf': 4000}
            late = call(base, f'/preview/jobs/{done_job["id"]}/cancel', {})
            assert late['state'] == 'completed', 'late cancel overwrote the completed result'
            reread = call(base, f'/preview/jobs/{done_job["id"]}')
            assert reread['state'] == 'completed' and reread['detail'] is not None
            print('scenario 2 ok: late cancel cannot mask the earlier completion')

            # --- Scenario 2b: both commit/finish orderings at the repository layer --
            repo = bench.repository
            # Ordering A: completion committed first, cancellation arrives late.
            j_first = repo.add_preview('fan', 1)
            assert repo.finish_preview(j_first, 'completed', {'note': 'done'})['state'] == 'completed'
            assert repo.request_preview_cancel(j_first)['state'] == 'completed'
            # Ordering B: cancellation committed first, completion lands late.
            j_second = repo.add_preview('fan', 1)
            assert repo.request_preview_cancel(j_second)['state'] == 'cancelled'
            late_finish = repo.finish_preview(j_second, 'completed', {'note': 'too late'})
            assert late_finish['state'] == 'cancelled' and late_finish['detail'] is None
            print('scenario 2b ok: conditional terminal transitions hold in both orderings')

            # --- Scenario 4: two jobs on the same recipe; cancel only one ----------
            gates = {}
            gates_lock = threading.Lock()

            def routed(job_id, conn, control):
                with gates_lock:
                    gate = gates.setdefault(job_id, Gate())
                gate(job_id, conn, control)

            bench.preview_gate = routed
            job_a = call(base, '/preview/jobs', lookup, 202)
            gate_a = gates[job_a['id']]
            assert gate_a.entered.wait(10), 'job A never reached its gate'
            job_b = call(base, '/preview/jobs', lookup, 202)
            deadline = time.time() + 10
            while job_b['id'] not in gates and time.time() < deadline:
                time.sleep(0.01)
            gate_b = gates[job_b['id']]
            assert gate_b.entered.wait(10), 'job B never reached its gate'
            # two independent read transactions are open concurrently
            assert len(gates) == 2
            call(base, f'/preview/jobs/{job_a["id"]}/cancel', {})
            gate_a.release.set()
            gate_b.release.set()
            final_a = wait_terminal(base, job_a['id'])
            final_b = wait_terminal(base, job_b['id'])
            assert final_a['state'] == 'cancelled', final_a
            assert final_b['state'] == 'completed', final_b
            assert final_b['detail']['summary']['rows'] == 4001
            print('scenario 4 ok: cancelling one preview left the concurrent preview intact')

            # --- Scenario 5: formal capture unaffected by the preview cancellation -
            bench.preview_gate = None
            capture = call(base, '/captures', lookup, 201)
            assert capture['state'] == 'sealed'
            assert capture['summary']['counts'] == {'root': 1, 'leaf': 4000}
            assert call(base, f'/captures/{capture["id"]}')['state'] == 'sealed'
            print('scenario 5 ok: formal capture independent of preview cancellation')

            # --- Scenario 6: a new job never inherits the old cancellation ---------
            fresh = call(base, '/preview/jobs', lookup, 202)
            assert fresh['id'] not in (job['id'], job_a['id'], done_job['id'])
            fresh_done = wait_terminal(base, fresh['id'])
            assert fresh_done['state'] == 'completed', fresh_done
            # the old cancelled job still reports its own outcome, untouched
            assert call(base, f'/preview/jobs/{job_a["id"]}')['state'] == 'cancelled'
            print('scenario 6 ok: fresh job does not inherit a previous cancellation')

            # --- Unknown job id is a clean 404 -------------------------------------
            call(base, '/preview/jobs/' + '0' * 32, status=404)
            call(base, '/preview/jobs/' + 'z' * 32, status=404)
            print('scenario extra ok: unknown preview ids are rejected')

        finally:
            server.shutdown()
            thread.join()
            server.server_close()

        # --- Scenario 3: restart while running loses the conclusion ----------------
        # Reopen the same repository with a brand-new server/process-equivalent object.
        orphan_id = 'a' * 32
        # Insert an orphaned 'running' row exactly as a hard-killed process would leave.
        with closing(sqlite3.connect(root / 'state.sqlite')) as conn:
            conn.execute("INSERT INTO preview_jobs (id,name,version,state) VALUES (?,?,?,'running')",
                         (orphan_id, 'fan', 1))
            conn.commit()
        # A genuinely fresh server runs Repository recovery on construction.
        server3 = create_server(('127.0.0.1', 0), root / 'state.sqlite', {'fanout': source})
        t3 = threading.Thread(target=server3.serve_forever, kwargs={'poll_interval': 0.02})
        t3.start()
        try:
            assert server3.workbench.preview_status(orphan_id)['state'] == 'inconclusive'
            # New work is still possible after the restart recovery sweep.
            base3 = f'http://127.0.0.1:{server3.server_port}'
            new_job = call(base3, '/preview/jobs', lookup, 202)
            assert wait_terminal(base3, new_job['id'])['state'] == 'completed'
        finally:
            server3.shutdown()
            t3.join()
            server3.server_close()
        print('scenario 3 ok: restart marks stranded previews inconclusive, new work still runs')
        print('ALL CANCELLATION SCENARIOS PASSED')


if __name__ == '__main__':
    main()

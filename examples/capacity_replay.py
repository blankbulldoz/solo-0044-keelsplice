"""Replay discriminative capacity experiments against the two lab sources.

Run after ``examples/capacity_seed.py`` has built the two SQLite files::

    python3 -m examples.capacity_replay [output_dir]

For every scenario the harness registers a real recipe version through the
HTTP JSON service, calls ``POST /preview`` and records the observed limit
(``row_budget`` / ``edge_budget`` / ``scan_budget``) or the success summary.
The same on-disk sources are reused; only the recipe inputs change, so the
table proves which intervention is effective against which ceiling.

The final exit status is non-zero if any expected observation drifts, which
lets an on-call engineer detect that a workbench or SQLite change altered the
capacity behaviour.
"""

import json
import sqlite3
import sys
import threading
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from keelsplice.api import create_server

from .capacity_seed import seed


def call(base, route, body=None, method='POST'):
    data = json.dumps(body).encode() if body is not None else None
    request = Request(base + route, data, {'Content-Type': 'application/json'}, method=method)
    try:
        with urlopen(request, timeout=30) as response:
            return response.status, json.loads(response.read())
    except HTTPError as exc:
        return exc.code, json.loads(exc.read())


def observe(base, alias, roots, follow, row_limit, name):
    """Register one recipe and preview it; return a compact observation."""
    status, catalog = call(base, f'/sources/{alias}/catalog', method='GET')
    assert status == 200, (alias, catalog)
    known = {(edge['child'], edge['id']) for edge in catalog['schema']['edges']}
    for selection in follow:
        assert (selection['child'], selection['id']) in known, selection
    body = {'source': alias, 'roots': roots, 'follow_children': follow, 'row_limit': row_limit}
    status, version = call(base, f'/recipes/{name}/versions', body)
    assert status == 201, (name, version)
    status, result = call(base, '/preview', {'name': name, 'version': version['version']})
    if status == 200:
        summary = result['summary']
        return {'outcome': 'ok', 'rows': summary['rows'],
                'witnesses': summary['witnesses'], 'counts': summary['counts']}
    return {'outcome': result['error'], 'message': result['message']}


def edges_for(base, alias, child, parent=None):
    status, catalog = call(base, f'/sources/{alias}/catalog', method='GET')
    assert status == 200
    edges = [edge for edge in catalog['schema']['edges'] if edge['child'] == child
             and (parent is None or edge['parent'] == parent)]
    return [{'child': edge['child'], 'id': edge['id']} for edge in edges]


def explain_scan(source):
    """Show the source-side query plan a reverse lookup actually gets."""
    with closing(sqlite3.connect(source)) as conn:
        plan = conn.execute(
            'EXPLAIN QUERY PLAN SELECT c.* FROM "event" p JOIN "event" c '
            'ON p."eid" = +c."parent_eid" WHERE p."eid" = ?', (1,)).fetchall()
        rows = conn.execute('SELECT count(*) FROM event').fetchone()[0]
    return rows, [step[3] for step in plan]


def arc_split(source, recipe):
    """Run the real collector in-process and split witnesses by direction.

    The HTTP preview only returns a total witness count.  Opening the source
    directly through the same ``capture.collect`` lets the runbook show why an
    edge budget fired: parent-direction witnesses come from mandatory foreign
    keys, child-direction witnesses only from the selected reverse edges.
    """
    from keelsplice import capture as capture_mod
    from keelsplice import catalog as catalog_mod

    with closing(sqlite3.connect(source)) as conn:
        conn.row_factory = sqlite3.Row
        schema = catalog_mod.snapshot(conn)['schema']
        result = capture_mod.collect(conn, schema, recipe)
    split = {'parent': 0, 'child': 0}
    for arc in result['arcs']:
        split[arc['direction']] += 1
    return split, capture_mod.summarize(result)


def run(output_dir):
    output_dir = Path(output_dir)
    dense, scan = seed(output_dir)
    # A separate, source-side pruned copy of the scan lab (retention/partition
    # applied before registration).  Same schema, same recipe, fewer rows.
    archived = output_dir / 'capacity_scan_archived.sqlite'
    for suffix in ('', '-wal', '-shm', '-journal'):
        stale = Path(str(archived) + suffix)
        if stale.exists():
            stale.unlink()
    with closing(sqlite3.connect(scan)) as src, closing(sqlite3.connect(archived)) as dst:
        dst.executescript('''
            CREATE TABLE customer (cid INTEGER PRIMARY KEY, name TEXT NOT NULL);
            CREATE TABLE event (eid INTEGER PRIMARY KEY, cid INTEGER NOT NULL REFERENCES customer,
                parent_eid INTEGER REFERENCES event, memo TEXT);
        ''')
        dst.execute('ATTACH DATABASE ? AS lab', (str(scan),))
        dst.execute('INSERT INTO customer SELECT * FROM lab.customer WHERE cid = 1')
        dst.execute('INSERT INTO event SELECT * FROM lab.event WHERE cid = 1')
        dst.commit()

    with TemporaryDirectory(prefix='keelsplice-capacity-') as repository_dir:
        repository = Path(repository_dir) / 'state.sqlite'
        server = create_server(('127.0.0.1', 0), repository,
                               {'dense': dense, 'scan': scan, 'archived': archived})
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.05})
        thread.start()
        base = f'http://127.0.0.1:{server.server_port}'
        try:
            report = build_report(base, dense, scan, archived)
        finally:
            server.shutdown()
            thread.join()
    return report


def build_report(base, dense, scan, archived):
    dense_evt_edges = edges_for(base, 'dense', 'evt')
    assert len(dense_evt_edges) == 12
    chain_edge = edges_for(base, 'scan', 'event', parent='event')
    customer_edge = edges_for(base, 'scan', 'event', parent='customer')
    assert len(chain_edge) == len(customer_edge) == 1
    archived_chain_edge = edges_for(base, 'archived', 'event', parent='event')

    hub_root = [{'table': 'hub', 'key': [1]}]
    event_root = [{'table': 'event', 'key': [1]}]
    customer_root = [{'table': 'customer', 'key': [1]}]

    # Scenario ids are stable so the runbook can reference them.
    experiments = [
        # --- witness-dense source: the edge ceiling binds first ---
        ('A0 dense: 12 reverse FKs, row_limit 5000 (baseline)',
         observe(base, 'dense', hub_root, dense_evt_edges, 5000, 'a0')),
        ('A1 dense: tighten to 6 reverse FKs',
         observe(base, 'dense', hub_root, dense_evt_edges[:6], 5000, 'a1')),
        ('A2 dense: same 12 FKs but row_limit 100 (row ceiling made dominant)',
         observe(base, 'dense', hub_root, dense_evt_edges, 100, 'a2')),
        ('A3 dense: add two extra roots (closure already complete)',
         observe(base, 'dense', hub_root + [{'table': 'hub', 'key': [2]},
                                            {'table': 'hub', 'key': [3]}],
                 dense_evt_edges, 5000, 'a3')),

        # --- read-work-heavy source: the scan ceiling binds first ---
        ('B0 scan: self-chain reverse edge, row_limit 5000 (baseline)',
         observe(base, 'scan', event_root, chain_edge, 5000, 'b0')),
        ('B1 scan: tighten by dropping the reverse edge',
         observe(base, 'scan', event_root, [], 5000, 'b1')),
        ('B2 scan: smaller input (customer root reaches no chain child)',
         observe(base, 'scan', customer_root, chain_edge, 5000, 'b2')),
        ('B3 scan: follow only the customer FK from the customer root',
         observe(base, 'scan', customer_root, customer_edge, 5000, 'b3')),
        ('B4 archived source: identical recipe after source-side retention',
         observe(base, 'archived', event_root, archived_chain_edge, 5000, 'b4')),
    ]

    # What the runbook asserts about each scenario's first-hit limit.
    expectations = {
        'A0': 'edge_budget', 'A1': 'ok', 'A2': 'row_budget', 'A3': 'edge_budget',
        'B0': 'scan_budget', 'B1': 'ok', 'B2': 'ok', 'B3': 'ok', 'B4': 'ok',
    }
    rows, plan = explain_scan(scan)

    # In-process witness split for the two dense recipes that succeed, proving
    # that tightening follow_children removes only child-direction witnesses.
    def dense_recipe(edge_ids):
        return {'source': 'dense', 'roots': [{'table': 'hub', 'key': [1]}],
                'follow_children': [{'child': 'evt', 'id': edge_id} for edge_id in edge_ids],
                'row_limit': 5000}

    dense_edge_ids = [selection['id'] for selection in dense_evt_edges]
    witness_diagnostics = {}
    for tag, edge_ids in (('three_edges', dense_edge_ids[:3]), ('six_edges', dense_edge_ids[:6])):
        split, summary = arc_split(dense, dense_recipe(edge_ids))
        witness_diagnostics[tag] = {'arc_directions': split, 'rows': summary['rows'],
                                    'witnesses': summary['witnesses']}

    report = {
        'sources': {
            'dense': str(dense),
            'scan': str(scan),
            'archived': str(archived),
        },
        'scan_lab_event_rows': rows,
        'scan_lab_reverse_lookup_plan': plan,
        'thresholds': {'row_limit_max': 5000, 'edge_budget': 50000,
                       'scan_progress_ticks': 5000, 'progress_instruction_interval': 1000},
        'dense_witness_diagnostics': witness_diagnostics,
        'experiments': [{'scenario': name, **observation} for name, observation in experiments],
        'expectations': expectations,
    }
    report['matched'] = all(
        observation['outcome'] == expectations[name[:2]]
        for name, observation in experiments
    )
    return report


def main(argv):
    output_dir = argv[1] if len(argv) > 1 and not argv[1].startswith('-') else str(
        Path(__file__).resolve().parent.parent / 'capacity-labs')
    report = run(output_dir)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    if not report['matched']:
        print('CAPACITY REPLAY DRIFTED FROM EXPECTED OBSERVATIONS', file=sys.stderr)
        return 1
    print('All capacity experiments matched the expected first-hit limits.', file=sys.stderr)
    return 0


if __name__ == '__main__':
    raise SystemExit(main(sys.argv))

"""Measure per-review and per-handoff cost on real sealed captures.

Protocol notes (so the numbers are not a single stopwatch sample):

* Every measured operation opens its own SQLite connection, matching what a
  browser request does; no connection or page cache is reused between samples.
* Each callable is warmed once before sampling. Samples are reported as median
  and p90 over repeated operations of the same kind.
* Read accounting comes from ``/proc/self/io`` ``rchar``: bytes this process
  asked the kernel for.  It therefore includes the full JSON/BLOB pull even
  when the page cache is warm, and does not depend on disk speed.
* The evidence index is one derived file per capture, built from the
  in-memory sealed result at seal time.  Repair (rebuild from storage) is
  measured separately and reported as its own cost, not folded into lookups.
"""

from __future__ import annotations

import gc
import json
import os
import sqlite3
import statistics
import time
from contextlib import closing
from pathlib import Path

from keelsplice import bundle
from keelsplice.codec import fingerprint
from keelsplice.service import Workbench
from keelsplice.storage import Repository

from . import evidence_index, fixtures, split_sealed

REPS = 15
SESSION_REPS = 5


def _io():
    with open('/proc/self/io', encoding='ascii') as handle:
        return {key: int(value) for key, value in (line.split(': ') for line in handle)}


class Reader:
    def __init__(self):
        self.bytes = 0
        self.elapsed = 0.0

    def __enter__(self):
        self.start = _io()['rchar']
        self.t0 = time.perf_counter()
        return self

    def __exit__(self, *exc):
        self.elapsed = time.perf_counter() - self.t0
        self.bytes = _io()['rchar'] - self.start


def pct(values, q):
    if not values:
        return float('nan')
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, max(0, round(q * (len(ordered) - 1))))]


def repeat(fn, n=REPS):
    """Sample ``fn`` with a full collection before each timed call.

    Without this, earlier decoded sealed captures still sitting on the heap
    trigger generational collections during the timed call.  That inflated a
    45k-witness trace from ~90 ms to ~350 ms in-process -- a measurement
    artifact, not a property of the read path.  ``gc.collect()`` is outside
    the measured window and the reader accounting.
    """
    fn()  # warm-up, untracked
    times, reads = [], []
    for _ in range(n):
        gc.collect()
        with Reader() as r:
            fn()
        times.append(r.elapsed)
        reads.append(r.bytes)
    return {'n': n, 'median_s': statistics.median(times), 'p90_s': pct(times, 0.9),
            'median_read_bytes': statistics.median(reads), 'max_read_bytes': max(reads)}


def edge_ids(catalog, specs):
    result = []
    for want in specs:
        edge = next(e for e in catalog['schema']['edges']
                    if e['child'] == want['child'] and e['parent'] == want['parent'])
        result.append({'child': edge['child'], 'id': edge['id']})
    return result


def _index_conn(path):
    conn = sqlite3.connect(path, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA query_only=ON')
    return conn


def _lookup_index(path, schema, table, values):
    with closing(_index_conn(path)) as conn:
        return evidence_index.lookup(conn, schema, {'table': table, 'key': values})


def _split_lookup(path, capture_id, table, values):
    with closing(sqlite3.connect(path)) as conn:
        evidence = split_sealed.load(conn, capture_id)
    return split_sealed.lookup(evidence, {'table': table, 'key': values})


def _trace_full(bench, capture_id, table, values):
    return bench.trace(capture_id, {'table': table, 'key': values})


def _expect_missing(fn):
    try:
        fn()
    except Exception as exc:  # noqa: BLE001 - both paths raise the same Fault
        if getattr(exc, 'code', None) == 'row_not_captured':
            return
        raise
    raise AssertionError('a missing key unexpectedly resolved')


def _recertify(artifact, sealed):
    # Re-runs the same independent-read verification the workbench performs.
    return bundle.certify(artifact, sealed)


def _download(repo_path, capture_id):
    with closing(sqlite3.connect(repo_path)) as conn:
        conn.row_factory = sqlite3.Row
        data = conn.execute('SELECT artifact FROM captures WHERE id=?', (capture_id,)).fetchone()['artifact']
    return bundle.digest_bytes(data)


def _file_bytes(path):
    with closing(sqlite3.connect(path)) as conn:
        return conn.execute('PRAGMA page_count').fetchone()[0] * conn.execute('PRAGMA page_size').fetchone()[0]


def _index_info(path):
    with closing(_index_conn(path)) as conn:
        return evidence_index.info(conn)


def run_case(spec, workdir: Path):
    source = workdir / f'{spec["kind"]}.sqlite'
    repo_path = workdir / f'{spec["kind"]}-repo.sqlite'
    fx = fixtures.seed(spec, source)
    repo = Repository(repo_path, {'s': source})
    bench = Workbench(repo)
    catalog = bench.catalog('s')
    body = {'source': 's', 'roots': fx['roots'],
            'follow_children': edge_ids(catalog, fx['follow_children']),
            'row_limit': fx['row_limit']}
    bench.register('review', body)
    try:
        capture = bench.capture({'name': 'review', 'version': 1})
    except Exception as exc:  # explicit rejection at the budget boundary
        return {'name': fx['name'], 'class': fx['class'], 'description': fx['description'],
                'rejected': getattr(exc, 'code', type(exc).__name__)}
    capture_id = capture['id']

    sealed = repo.load_capture(capture_id)['sealed']
    sealed_bytes = len(json.dumps(sealed, ensure_ascii=True, sort_keys=True, separators=(',', ':')).encode())

    # Index built from the in-memory result, as a seal-time hook would.
    index_path = workdir / 'evidence.sqlite'
    with closing(sqlite3.connect(index_path)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA journal_mode=OFF')
        conn.execute('PRAGMA synchronous=OFF')
        with Reader() as index_build:
            evidence_index.build(conn, sealed)
        index_storage = evidence_index.storage_bytes(conn)
        with Reader() as index_verify_write:
            evidence_index.verify(conn)
    index_info = _index_info(index_path)

    # Repair path: load the whole sealed capture from storage, rebuild index.
    with Reader() as repair_read:
        reloaded = repo.load_capture(capture_id)['sealed']
    with closing(sqlite3.connect(index_path)) as conn:
        conn.row_factory = sqlite3.Row
        with Reader() as repair_build:
            evidence_index.build(conn, reloaded)

    # Split-storage option: same sealed capture, evidence column separated
    # from row values.  Stored in its own side database for the measurement.
    split_path = workdir / 'split.sqlite'
    with closing(sqlite3.connect(split_path)) as conn:
        conn.execute('PRAGMA journal_mode=OFF')
        conn.execute('PRAGMA synchronous=OFF')
        evidence_len, payload_len = split_sealed.store(conn, capture_id, sealed)

    # Drop the decoded captures before timing.  Leaving them on the heap makes
    # every timed call pay generational-collection work proportional to them:
    # measured at 90 ms -> 350 ms for a 45k-witness trace.  A browser request
    # never carries a decoded capture in from a previous request, so keeping
    # them here would measure our own scaffolding.
    schema = sealed['schema']
    del sealed
    del reloaded
    gc.collect()

    # Capture-detail page: today it reloads and re-summarises the whole sealed
    # capture just to show counts and roots.
    def detail_full():
        return bench.status(capture_id)

    def detail_index():
        with closing(_index_conn(index_path)) as conn:
            return evidence_index.summary(conn)

    detail_full_m = repeat(detail_full)
    detail_index_m = repeat(detail_index)
    assert detail_full_m and detail_index_m
    assert detail_index()['counts'] == detail_full()['summary']['counts']
    assert detail_index()['roots'] == detail_full()['roots']

    table0, values0 = fx['lookups'][0]
    lookup_full = repeat(lambda: _trace_full(bench, capture_id, table0, values0))
    lookup_split = repeat(lambda: _split_lookup(split_path, capture_id, table0, values0))
    lookup_index = repeat(lambda: _lookup_index(index_path, schema, table0, values0))
    full_missing = repeat(lambda: _expect_missing(lambda: _trace_full(bench, capture_id, *fx['missing'])))
    index_missing = repeat(lambda: _expect_missing(
        lambda: _lookup_index(index_path, schema, *fx['missing'])))

    def session_full():
        for table, values in fx['session']:
            _trace_full(bench, capture_id, table, values)

    def session_index():
        for table, values in fx['session']:
            _lookup_index(index_path, schema, table, values)

    def session_split():
        for table, values in fx['session']:
            _split_lookup(split_path, capture_id, table, values)

    session_full_m = repeat(session_full, SESSION_REPS)
    session_index_m = repeat(session_index, SESSION_REPS)
    session_split_m = repeat(session_split, SESSION_REPS)

    artifact = bench.build(capture_id)
    artifact_bytes = len(bench.repository.load_capture(capture_id, with_artifact=True)['artifact'])
    with closing(sqlite3.connect(repo_path)) as conn:
        conn.row_factory = sqlite3.Row
        stored_artifact = conn.execute('SELECT artifact FROM captures WHERE id=?', (capture_id,)).fetchone()['artifact']
    certify_full = repeat(lambda: _recertify(stored_artifact, bench.repository.load_capture(capture_id)['sealed']), 3)
    download_full = repeat(lambda: _download(repo_path, capture_id), 5)
    with closing(sqlite3.connect(repo_path)) as conn:
        repo_bytes = conn.execute('PRAGMA page_count').fetchone()[0] * conn.execute('PRAGMA page_size').fetchone()[0]

    # Integrity checks on the index itself, priced separately from lookups.
    with closing(_index_conn(index_path)) as conn:
        with Reader() as verify_index:
            evidence_index.verify(conn)
        with Reader() as audit_index:
            # A real audit cannot trust the copy already in memory: it must
            # re-read the sealed capture from storage, then check the index.
            evidence_index.audit(conn, repo.load_capture(capture_id)['sealed'])

    # Equivalence: all three paths must produce identical answers, including
    # the missing-key rejection.
    for table, values in fx['lookups']:
        full = _trace_full(bench, capture_id, table, values)
        assert full == _lookup_index(index_path, schema, table, values), (table, values)
        assert full == _split_lookup(split_path, capture_id, table, values), (table, values)
    _expect_missing(lambda: _trace_full(bench, capture_id, *fx['missing']))
    _expect_missing(lambda: _lookup_index(index_path, schema, *fx['missing']))
    _expect_missing(lambda: _split_lookup(split_path, capture_id, *fx['missing']))

    return {
        'name': fx['name'],
        'class': fx['class'],
        'description': fx['description'],
        'rows': capture['summary']['rows'],
        'witnesses': capture['summary']['witnesses'],
        'root_occurrences': capture['summary']['root_occurrences'],
        'sealed_json_bytes': sealed_bytes,
        'artifact_bytes': artifact_bytes,
        'repository_bytes': repo_bytes,
        'index_storage_bytes': index_storage,
        'index_file_bytes': _file_bytes(index_path),
        'index_digest_matches': index_info.capture_digest == fingerprint(
            repo.load_capture(capture_id)['sealed']),
        'index_nodes': index_info.nodes,
        'index_arcs': index_info.arcs,
        'index_roots': index_info.roots,
        'index_build': {'bytes': index_build.bytes, 's': index_build.elapsed},
        'index_write_verify': {'bytes': index_verify_write.bytes, 's': index_verify_write.elapsed},
        'index_repair_load': {'bytes': repair_read.bytes, 's': repair_read.elapsed},
        'index_repair_build': {'bytes': repair_build.bytes, 's': repair_build.elapsed},
        'index_verify': {'bytes': verify_index.bytes, 's': verify_index.elapsed},
        'index_audit': {'bytes': audit_index.bytes, 's': audit_index.elapsed},
        'split_evidence_bytes': evidence_len,
        'split_payload_bytes': payload_len,
        'split_storage_bytes': _file_bytes(split_path),
        'lookup_split': lookup_split,
        'lookup_split_missing': repeat(lambda: _expect_missing(
            lambda: _split_lookup(split_path, capture_id, *fx['missing'])), 3),
        'session_split': session_split_m,
        'detail_full': detail_full_m,
        'detail_index': detail_index_m,
        'lookup_full': lookup_full,
        'lookup_index': lookup_index,
        'lookup_full_missing': full_missing,
        'lookup_index_missing': index_missing,
        'session_full': session_full_m,
        'session_index': session_index_m,
        'certify_full': certify_full,
        'download_full': download_full,
    }

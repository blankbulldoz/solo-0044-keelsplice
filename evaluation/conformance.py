"""Verify the two alternatives answer exactly like the shipped trace path.

For every captured row of every fixture -- not a hand-picked sample -- check
that:

* the derived index returns the same ``trace`` payload;
* the split store reassembles to the *exact* sealed object, so the certificate's
  ``capture_digest`` still verifies;
* a key that was never captured is rejected with ``row_not_captured`` by all
  three paths;
* an index with one altered row is rejected by its own digest.

Exit code 0 means every check passed.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import tempfile
from contextlib import closing
from pathlib import Path

from keelsplice.codec import Fault, fingerprint, loads
from keelsplice.service import Workbench
from keelsplice.storage import Repository

from . import evidence_index, fixtures, split_sealed
from .harness import edge_ids


def _check_fixture(spec, root: Path):
    source = root / 'source.sqlite'
    fx = fixtures.seed(spec, source)
    repo = Repository(root / 'repo.sqlite', {'s': source})
    bench = Workbench(repo)
    catalog = bench.catalog('s')
    body = {'source': 's', 'roots': fx['roots'],
            'follow_children': edge_ids(catalog, fx['follow_children']),
            'row_limit': fx['row_limit']}
    # A duplicated root exercises occurrence order.
    body['roots'] = body['roots'] + body['roots'][:1]
    bench.register('review', body)
    capture_id = bench.capture({'name': 'review', 'version': 1})['id']
    sealed = repo.load_capture(capture_id)['sealed']

    index_path = root / 'index.sqlite'
    with closing(sqlite3.connect(index_path)) as conn:
        conn.row_factory = sqlite3.Row
        evidence_index.build(conn, sealed)
        evidence_index.verify(conn)
        info = evidence_index.info(conn)
    split_path = root / 'split.sqlite'
    with closing(sqlite3.connect(split_path)) as conn:
        split_sealed.store(conn, capture_id, sealed)
        rebuilt = split_sealed.reassemble(split_sealed.load(conn, capture_id),
                                          split_sealed.load_payload(conn, capture_id))
    assert rebuilt == sealed, 'split store does not reassemble to the sealed capture'
    assert fingerprint(rebuilt) == fingerprint(sealed), 'split store changes capture_digest'

    checked = 0
    for table, rows in sealed['result']['rows'].items():
        for token in rows:
            address = {'table': table, 'key': loads(token)}
            want = bench.trace(capture_id, address)
            with closing(sqlite3.connect(index_path)) as conn:
                conn.row_factory = sqlite3.Row
                got = evidence_index.lookup(conn, sealed['schema'], address)
            assert got == want, (fx['name'], address, got, want)
            checked += 1

    missing = {'table': fx['missing'][0], 'key': fx['missing'][1]}

    def indexed_missing():
        with closing(sqlite3.connect(index_path)) as conn:
            conn.row_factory = sqlite3.Row
            return evidence_index.lookup(conn, sealed['schema'], missing)

    def split_missing():
        with closing(sqlite3.connect(split_path)) as conn:
            return split_sealed.lookup(split_sealed.load(conn, capture_id), missing)

    for label, call in (('sealed', lambda: bench.trace(capture_id, missing)),
                        ('split', split_missing), ('index', indexed_missing)):
        try:
            call()
        except Fault as exc:
            assert exc.code == 'row_not_captured', (label, exc.code)
        else:
            raise AssertionError(f'{label} accepted a missing key')

    # Tamper detection: one altered node must break the index's own digest.
    with closing(sqlite3.connect(index_path)) as conn:
        conn.execute("UPDATE ev_nodes SET token=? WHERE rowid=1", ('["tampered"]',))
        conn.commit()
        try:
            evidence_index.verify(conn)
        except Fault as exc:
            assert exc.code == 'index_corrupt', exc.code
        else:
            raise AssertionError('tampered index passed verification')
    evidence, payload = split_sealed.split(sealed)
    return {'name': fx['name'], 'rows_checked': checked, 'witnesses': info.arcs,
            'split_evidence_bytes': len(evidence.encode('ascii')),
            'split_payload_bytes': len(payload.encode('ascii'))}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    args = parser.parse_args(argv)
    specs = [
        {'kind': 'microscope'},
        {'kind': 'fanout', 'probes': 25},
        {'kind': 'fanout', 'probes': 250},
        {'kind': 'wide', 'roots': 5, 'per_root': 20},
        {'kind': 'blob', 'rows': 2, 'payload_bytes': 64 * 1024},
        {'kind': 'blob', 'rows': 2, 'payload_bytes': 1024 * 1024},
    ]
    with tempfile.TemporaryDirectory(prefix='keelsplice-conformance-') as directory:
        for number, spec in enumerate(specs):
            root = Path(directory) / f'{number:02d}'
            root.mkdir()
            result = _check_fixture(spec, root)
            print(f"{result['name']:<14} rows_checked={result['rows_checked']:<5} arcs={result['witnesses']}")
    print('conformance: every captured row, both alternatives, all agree')
    return 0


if __name__ == '__main__':
    sys.exit(main())

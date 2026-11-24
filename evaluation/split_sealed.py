"""Prototype: keep the sealed capture whole, but store it in pieces.

The middle option between "read the whole sealed capture" and "keep a separate
derived index".  The sealed JSON is split into two columns of the same record:

``evidence``  schema, row *identities*, witnesses and roots (no row values)
``payload``   the row values

A lookup reads only ``evidence``.  It still scans the witness list in memory,
so this option buys bytes but not CPU on high-fan-out captures.  It needs no
second derivation step and no repair: it is the same sealed capture, just
stored in two pieces.

Correctness note: the pair must be written in one statement so the "same
capture" guarantee survives.  ``payload`` is not used by lookups, so unlike
``evidence_index`` this design still pays a full payload read whenever a
review path needs a row value (none of the current review pages do).
"""

from __future__ import annotations

import json

from keelsplice.codec import Fault, dumps, key
from keelsplice.recipe import identity

DDL = """
CREATE TABLE IF NOT EXISTS split_evidence (
    capture_id TEXT PRIMARY KEY,
    evidence TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS split_payload (
    capture_id INTEGER PRIMARY KEY,
    payload TEXT NOT NULL
);
"""


def split(sealed):
    """Return (evidence_json, payload_json) for one sealed capture."""
    result = sealed['result']
    evidence = {
        # ``recipe`` must be carried: without it the canonical sealed object
        # cannot be rebuilt, so ``fingerprint(sealed)`` -- the certificate's
        # ``capture_digest`` and the index's staleness check -- would fail.
        'recipe': sealed['recipe'],
        'schema': sealed['schema'],
        'schema_digest': sealed['schema_digest'],
        'rows': {table: sorted(rows) for table, rows in result['rows'].items()},
        'arcs': result['arcs'],
        'roots': result['roots'],
    }
    return dumps(evidence), dumps(result['rows'])


def reassemble(evidence, payload):
    """Rebuild the exact sealed object; its fingerprint must match the certificate."""
    return {
        'recipe': evidence['recipe'],
        'schema': evidence['schema'],
        'schema_digest': evidence['schema_digest'],
        'result': {'rows': payload, 'arcs': evidence['arcs'], 'roots': evidence['roots']},
    }


def store(conn, capture_id, sealed):
    conn.executescript(DDL)
    evidence, payload = split(sealed)
    with conn:
        conn.execute('INSERT OR REPLACE INTO split_evidence VALUES (?,?)', (capture_id, evidence))
        conn.execute('INSERT OR REPLACE INTO split_payload VALUES (?,?)', (hash(capture_id) & 0x7fffffff, payload))
    return len(evidence.encode('ascii')), len(payload.encode('ascii'))


def load(conn, capture_id):
    """Read only the evidence table.

    Payload lives in a *separate table*, not a separate column: SQLite reads a
    whole overflow chain when it touches a row with a large value, so a small
    column beside a 6 MiB BLOB would still cost the full BLOB.  That was
    measured before this split; see results-default-single-table.json.
    """
    row = conn.execute('SELECT evidence FROM split_evidence WHERE capture_id=?', (capture_id,)).fetchone()
    if row is None:
        raise Fault('unknown_capture', 'Capture does not exist', 404)
    return json.loads(row[0])


def load_payload(conn, capture_id):
    row = conn.execute('SELECT payload FROM split_payload WHERE capture_id=?',
                       (hash(capture_id) & 0x7fffffff,)).fetchone()
    return json.loads(row[0])


def lookup(evidence, address):
    """Answer the same question as ``keelsplice.capture.trace``."""
    address = identity(evidence['schema'], address)
    table, values = address['table'], address['key']
    target = [table, values]
    if key(values) not in set(evidence['rows'][table]):
        raise Fault('row_not_captured', 'Use the actual typed key stored in the capture', 404)
    return {
        'row': target,
        'roots': [i for i, root in enumerate(evidence['roots']) if root == target],
        'causes': [arc for arc in evidence['arcs'] if arc['target'] == target],
        'dependencies': [arc for arc in evidence['arcs']
                         if arc['origin'] == target and arc['direction'] == 'parent'],
    }

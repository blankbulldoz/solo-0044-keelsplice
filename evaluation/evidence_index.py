"""Prototype of a derived evidence index for read-mostly row review.

The prototype deliberately lives outside ``keelsplice``: this evaluation is
about whether an index is worth its storage and lifecycle cost, not about
changing the product's contract yet.  It stores *identities and witnesses*
only, never payload values, so BLOB-heavy captures pay almost nothing.

Design choices that matter for the comparison
---------------------------------------------
* One index file per capture.  The capture identity is the filename, so no
  32-byte ``capture_id`` is repeated on every witness row, and deleting a
  capture index is a single unlink.
* Table names are interned in ``ev_tables``; witnesses store two integers.
* Arc insertion order is the canonical ``dumps`` order used by the sealed
  capture, and the arc table has no explicit primary key, so its rowid *is*
  the witness sequence.  ``ORDER BY rowid`` can then be served by the
  per-node index (SQLite appends rowid to every index key), which avoids the
  measured failure mode where ``ORDER BY seq`` forced a full-arc scan.
* Lookup answers reproduce ``keelsplice.capture.trace`` exactly: same typed
  key canonicalisation, same root ordinals, same arc ordering.

Schema shape
------------
``ev_meta``    format version and the sealed-capture digest it derives from
``ev_tables``  interned table names
``ev_nodes``   one row per captured row identity (table + canonical key token)
``ev_arcs``    one row per witness, by node id, with edge and direction
``ev_roots``   input-root occurrences, ordinal -> node
"""

from __future__ import annotations

import sqlite3
import struct
from dataclasses import dataclass

import hashlib

from keelsplice.codec import Fault, dumps, fingerprint, key, loads
from keelsplice.recipe import identity

FORMAT = 1
PARENT, CHILD = 0, 1
ARCS = struct.Struct('>qqqqqq')

DDL = """
CREATE TABLE IF NOT EXISTS ev_meta (
    name TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ev_tables (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS ev_nodes (
    id INTEGER PRIMARY KEY,
    tbl INTEGER NOT NULL REFERENCES ev_tables(id),
    token TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ev_nodes_lookup ON ev_nodes(tbl, token);
-- No explicit primary key: rowid is the canonical witness sequence.
CREATE TABLE IF NOT EXISTS ev_arcs (
    origin INTEGER NOT NULL REFERENCES ev_nodes(id),
    target INTEGER NOT NULL REFERENCES ev_nodes(id),
    direction INTEGER NOT NULL,
    edge_child INTEGER NOT NULL REFERENCES ev_tables(id),
    edge_id INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ev_arcs_by_target ON ev_arcs(target);
CREATE INDEX IF NOT EXISTS ev_arcs_by_origin ON ev_arcs(origin, direction);
CREATE TABLE IF NOT EXISTS ev_roots (
    ordinal INTEGER PRIMARY KEY,
    node INTEGER NOT NULL REFERENCES ev_nodes(id)
);
CREATE INDEX IF NOT EXISTS ev_roots_by_node ON ev_roots(node);
"""

TABLES = ('ev_meta', 'ev_tables', 'ev_nodes', 'ev_arcs', 'ev_roots')


def create(conn):
    conn.executescript(DDL)


def drop(conn):
    for name in ('ev_roots', 'ev_arcs', 'ev_nodes', 'ev_tables', 'ev_meta'):
        conn.execute(f'DROP TABLE IF EXISTS {name}')


def build(conn, sealed):
    """Derive the index from an already-decoded sealed capture.

    A production hook would call this inside ``Repository.add_capture`` while
    the same result object is still in memory, so no second read is needed.
    Here we also accept an object loaded back from storage to measure repair.
    """
    conn.executescript(DDL)
    result = sealed['result']
    digest = fingerprint(sealed)

    names = sorted(result['rows'])
    table_ids = {name: index + 1 for index, name in enumerate(names)}

    node_ids = {}
    node_rows = []
    node_id = 0
    for table in names:
        for token in sorted(result['rows'][table]):
            node_id += 1
            node_ids[(table, token)] = node_id
            node_rows.append((node_id, table_ids[table], token))

    arc_rows = []
    for arc in result['arcs']:
        origin = node_ids[(arc['origin'][0], key(arc['origin'][1]))]
        target = node_ids[(arc['target'][0], key(arc['target'][1]))]
        direction = PARENT if arc['direction'] == 'parent' else CHILD
        arc_rows.append((origin, target, direction, table_ids[arc['edge'][0]], arc['edge'][1]))

    # Input-root order is the ordinal; duplicates are retained.
    root_rows = [(ordinal, node_ids[(table, key(values))])
                 for ordinal, (table, values) in enumerate(result['roots'])]

    with conn:
        conn.execute('DELETE FROM ev_roots')
        conn.execute('DELETE FROM ev_arcs')
        conn.execute('DELETE FROM ev_nodes')
        conn.execute('DELETE FROM ev_tables')
        conn.execute('DELETE FROM ev_meta')
        conn.executemany('INSERT INTO ev_tables VALUES (?,?)',
                         [(table_ids[name], name) for name in names])
        conn.executemany('INSERT INTO ev_nodes VALUES (?,?,?)', node_rows)
        conn.executemany('INSERT INTO ev_arcs VALUES (?,?,?,?,?)', arc_rows)
        conn.executemany('INSERT INTO ev_roots VALUES (?,?)', root_rows)
        conn.executemany('INSERT INTO ev_meta VALUES (?,?)', [
            ('format', str(FORMAT)),
            ('capture_digest', digest),
            ('nodes', str(len(node_rows))),
            ('arcs', str(len(arc_rows))),
            ('roots', str(len(root_rows))),
            ('schema_digest', sealed['schema_digest']),
            ('content_digest', content_digest(conn)),
        ])
    return len(node_rows), len(arc_rows), len(root_rows)


def content_digest(conn):
    """Cheap self-digest over identities, witnesses and roots, no payload.

    Recomputed by ``verify`` to detect local corruption without touching the
    sealed capture.  It is *not* proof of equivalence to the sealed bytes; that
    is ``audit``, which costs one full sealed read.
    """
    sha = hashlib.sha256()

    def text(value):
        data = value.encode('utf-8')
        return len(data).to_bytes(4, 'big') + data

    for table_id, name in conn.execute('SELECT id, name FROM ev_tables ORDER BY id'):
        sha.update(b'T' + table_id.to_bytes(4, 'big') + text(name))
    for node_id, table_id, token in conn.execute('SELECT id, tbl, token FROM ev_nodes ORDER BY id'):
        sha.update(b'N' + node_id.to_bytes(8, 'big') + table_id.to_bytes(4, 'big') + text(token))
    # pack each arc row in one struct call: 5 big-endian integers
    for row in conn.execute(
            'SELECT rowid, origin, target, direction, edge_child, edge_id FROM ev_arcs ORDER BY rowid'):
        sha.update(b'A' + ARCS.pack(*row))
    for ordinal, node in conn.execute('SELECT ordinal, node FROM ev_roots ORDER BY ordinal'):
        sha.update(b'R' + ordinal.to_bytes(8, 'big') + node.to_bytes(8, 'big'))
    return sha.hexdigest()


def verify(conn):
    """Recompute the self-digest and compare with what build recorded."""
    stored = dict(conn.execute('SELECT name, value FROM ev_meta')).get('content_digest')
    if stored is None:
        raise Fault('index_unverified', 'Evidence index has no content digest')
    if content_digest(conn) != stored:
        raise Fault('index_corrupt', 'Evidence index contents do not match its own digest')
    return stored


def audit(conn, sealed):
    """Prove the index derives from this exact sealed capture.

    Costs one full read of the sealed capture (the price of not trusting the
    index).  Compares both the capture digest and the derivation digest.
    """
    values = dict(conn.execute('SELECT name, value FROM ev_meta'))
    if values.get('capture_digest') != fingerprint(sealed):
        raise Fault('index_stale', 'Evidence index was derived from a different sealed capture')
    return verify(conn)


@dataclass
class IndexInfo:
    capture_digest: str
    schema_digest: str
    format: int
    nodes: int
    arcs: int
    roots: int
    content_digest: str | None


def info(conn):
    values = dict(conn.execute('SELECT name, value FROM ev_meta'))
    if not values:
        return None
    return IndexInfo(values['capture_digest'], values['schema_digest'], int(values['format']),
                     int(values['nodes']), int(values['arcs']), int(values['roots']),
                     values.get('content_digest'))


def _arc(row, table_names):
    return {
        'origin': [table_names[row['origin_tbl']], loads(row['origin_key'])],
        'target': [table_names[row['target_tbl']], loads(row['target_key'])],
        'edge': [table_names[row['edge_child']], row['edge_id']],
        'direction': 'parent' if row['direction'] == PARENT else 'child',
    }


ARC_SELECT = """
SELECT a.rowid AS seq, a.edge_child, a.edge_id, a.direction,
       o.tbl AS origin_tbl, o.token AS origin_key,
       t.tbl AS target_tbl, t.token AS target_key
FROM ev_arcs a
JOIN ev_nodes o ON o.id=a.origin
JOIN ev_nodes t ON t.id=a.target
"""


def lookup(conn, schema, address):
    """Answer the same question as ``keelsplice.capture.trace``."""
    conn.row_factory = sqlite3.Row
    address = identity(schema, address)
    table, values = address['table'], address['key']
    token = key(values)
    node = conn.execute(
        'SELECT n.id FROM ev_nodes n JOIN ev_tables t ON t.id=n.tbl WHERE t.name=? AND n.token=?',
        (table, token)).fetchone()
    if node is None:
        raise Fault('row_not_captured', 'Use the actual typed key stored in the capture', 404)
    node_id = node['id']
    roots = [row['ordinal'] for row in conn.execute(
        'SELECT ordinal FROM ev_roots WHERE node=? ORDER BY ordinal', (node_id,))]
    causes = [row for row in conn.execute(
        ARC_SELECT + ' WHERE a.target=? ORDER BY a.rowid', (node_id,))]
    dependencies = [row for row in conn.execute(
        ARC_SELECT + ' WHERE a.origin=? AND a.direction=? ORDER BY a.rowid', (node_id, PARENT))]
    names = dict(conn.execute('SELECT id, name FROM ev_tables'))
    return {'row': [table, values], 'roots': roots,
            'causes': [_arc(row, names) for row in causes],
            'dependencies': [_arc(row, names) for row in dependencies]}


def summary(conn):
    """The facts the capture-detail page shows, without reading row values.

    Matches ``capture.summarize`` plus the canonical root list.
    """
    conn.row_factory = sqlite3.Row
    # Empty tables must still appear with a zero count, like ``summarize``.
    counts = {row['name']: row['n'] for row in conn.execute(
        'SELECT t.name AS name, COUNT(n.id) AS n FROM ev_tables t LEFT JOIN ev_nodes n ON n.tbl=t.id '
        'GROUP BY t.name')}
    meta = info(conn)
    roots = [[row['name'], loads(row['token'])] for row in conn.execute(
        'SELECT t.name AS name, n.token AS token FROM ev_roots r '
        'JOIN ev_nodes n ON n.id=r.node JOIN ev_tables t ON t.id=n.tbl ORDER BY r.ordinal')]
    return {'counts': counts, 'rows': sum(counts.values()), 'witnesses': meta.arcs,
            'root_occurrences': meta.roots, 'roots': roots}


def storage_bytes(conn):
    """Logical bytes attributable to the index (tables, indexes, overflow)."""
    return conn.execute('SELECT COALESCE(SUM(pgsize), 0) FROM dbstat').fetchone()[0]

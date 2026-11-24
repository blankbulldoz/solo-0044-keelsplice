"""Seed two small SQLite sources that hit different Keelsplice capacity limits.

The two databases are deliberately the same order of business size but have
opposite shape/indexing so that a comparable recipe first meets a different
hard ceiling:

* ``capacity_dense.sqlite``  -- witness-dense.  A small hub table is referenced
  by many foreign keys of a small event table, so relationship witnesses (arcs)
  pile up through re-sharing while distinct selected rows stay few.  The edge
  budget is reached before the row or the source-scan budget.

* ``capacity_scan.sqlite`` -- read-work-heavy.  A wide event table has an
  unindexed self-referential foreign key and a narrow reachable chain inside
  it.  Every reverse lookup full-scans the wide table (the workbench child join
  applies unary ``+`` which suppresses ordinary indexes), so the snapshot
  progress-handler budget is reached while the closure stays tiny.

The row budget is a property of the recipe (``row_limit``), not of a database,
so both files can also be driven into ``row_budget``; the replay module shows
when that happens and how it changes the diagnosis.

Running this module writes the two files into a directory given on the command
line (default: a ``capacity-labs`` directory next to the repository root).
The generation is deterministic: rerunning produces identical bytes.
"""

import sqlite3
import sys
from contextlib import closing
from pathlib import Path

# Shared business-scale envelope.  Both sources stay around 2.5k-18k rows so a
# reviewer can hold the whole shape in mind, yet each crosses a real 5000/50000
# ceiling used by the workbench.
DENSE_HUBS = 24
DENSE_EVENTS = 2500
DENSE_FOREIGN_KEYS = 12

SCAN_CUSTOMERS = 32
SCAN_EVENTS = 18000
SCAN_CHAIN = 58


def _connect(path):
    conn = sqlite3.connect(path)
    # Deterministic bytes across runs: rollback journal, no WAL side files.
    conn.execute('PRAGMA journal_mode=DELETE')
    conn.execute('PRAGMA foreign_keys=ON')
    return conn


def seed_dense(path):
    """Witness-dense source: many re-sharing foreign keys on a small child."""
    with closing(_connect(path)) as conn:
        conn.execute('CREATE TABLE hub (hid INTEGER PRIMARY KEY, label TEXT NOT NULL)')
        fk_columns = ', '.join(
            f'link{k} INTEGER NOT NULL REFERENCES hub' for k in range(DENSE_FOREIGN_KEYS)
        )
        conn.execute(f'CREATE TABLE evt (eid INTEGER PRIMARY KEY, {fk_columns}, memo TEXT)')
        conn.executemany('INSERT INTO hub VALUES (?, ?)',
                         ((h, f'hub-{h}') for h in range(1, DENSE_HUBS + 1)))
        placeholders = ', '.join('?' * (DENSE_FOREIGN_KEYS + 2))

        def rows():
            for e in range(1, DENSE_EVENTS + 1):
                # Every link lands inside the small hub band.  Distinct events
                # grow linearly, but each event emits one witness per foreign
                # key, so witnesses grow DENSE_FOREIGN_KEYS times faster.
                links = tuple(((e * (k + 3) + k) % DENSE_HUBS) + 1
                              for k in range(DENSE_FOREIGN_KEYS))
                yield (e,) + links + (f'memo-{e}',)

        conn.executemany(f'INSERT INTO evt VALUES ({placeholders})', rows())
        conn.commit()


def seed_scan(path):
    """Read-work-heavy source: wide unindexed table with a narrow chain."""
    with closing(_connect(path)) as conn:
        conn.executescript(
            '''
            CREATE TABLE customer (
                cid INTEGER PRIMARY KEY,
                name TEXT NOT NULL
            );
            CREATE TABLE event (
                eid INTEGER PRIMARY KEY,
                cid INTEGER NOT NULL REFERENCES customer,
                parent_eid INTEGER REFERENCES event,
                memo TEXT
            );
            '''
        )
        # Deliberately NO index on event.cid or event.parent_eid.  The
        # workbench child join compares with unary +, so even adding a plain
        # index later would not change the SCAN plan; the replay documents this.
        conn.executemany('INSERT INTO customer VALUES (?, ?)',
                         ((c, f'cust-{c}') for c in range(1, SCAN_CUSTOMERS + 1)))
        # Filler rows form the wide, unrelated bulk that makes each reverse
        # lookup expensive.  They point at customers 2..31, never at cid 1.
        conn.executemany(
            'INSERT INTO event (eid, cid, parent_eid, memo) VALUES (?, ?, NULL, ?)',
            ((e, ((e % 30) + 2), f'bulk-{e}') for e in range(1, SCAN_EVENTS + 1)),
        )
        # A short, reachable self-parent chain rooted at event 1 / customer 1.
        # Only these rows belong to the closure, but each step full-scans the
        # whole event table to find its children.
        conn.executemany(
            'INSERT OR REPLACE INTO event (eid, cid, parent_eid, memo) '
            'VALUES (?, 1, ?, ?)',
            ((e, (e - 1) if e > 1 else None, f'chain-{e}') for e in range(1, SCAN_CHAIN + 1)),
        )
        conn.commit()


def seed(directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    dense = directory / 'capacity_dense.sqlite'
    scan = directory / 'capacity_scan.sqlite'
    # Remove stale copies (and any journal side files) so reruns are identical
    # byte-for-byte instead of accumulating duplicate schema.
    for path in (dense, scan):
        for suffix in ('', '-wal', '-shm', '-journal'):
            stale = Path(str(path) + suffix)
            if stale.exists():
                stale.unlink()
    seed_dense(dense)
    seed_scan(scan)
    return dense, scan


def main(argv):
    directory = Path(argv[1]) if len(argv) > 1 else Path(__file__).resolve().parent.parent / 'capacity-labs'
    dense, scan = seed(directory)
    print(dense)
    print(scan)


if __name__ == '__main__':
    main(sys.argv)

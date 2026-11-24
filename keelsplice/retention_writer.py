"""Background writer used by the retention archive scenario.

Run as a subprocess so it holds its own connection and, for a pending point,
keeps an uncommitted transaction open while the workbench captures a sample::

    python3 -m keelsplice.retention_writer --source FILE --mode pending \
        --statements JSON --image FILE --ready FILE --release FILE

``pending`` opens ``BEGIN IMMEDIATE``, applies the statements, writes the
writer's own full snapshot with ``Connection.serialize()`` and announces
readiness.  The snapshot is the only faithful view of pages that SQLite still
holds in the page cache; a raw WAL overlay cannot reach them.  The transaction
is rolled back when the release file appears.

``commit`` applies the statements, commits, announces readiness and then waits
so the committed WAL generation stays on disk while the archive snapshots it.
"""

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path

POLL_SECONDS = 0.01
IDLE_TIMEOUT = 180


def wait_for(path, timeout=IDLE_TIMEOUT):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if Path(path).exists():
            return True
        time.sleep(POLL_SECONDS)
    return False


def connect(source):
    conn = sqlite3.connect(source, isolation_level=None, timeout=10)
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA wal_autocheckpoint=0')
    conn.execute('PRAGMA synchronous=FULL')
    # A tiny page cache makes the writer spill dirty pages into the WAL during
    # an open transaction, so the pending point has real frames to inspect.
    conn.execute('PRAGMA cache_size=1')
    return conn


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True)
    parser.add_argument('--mode', choices=('pending', 'commit'), required=True)
    parser.add_argument('--statements', required=True)
    parser.add_argument('--image')
    parser.add_argument('--ready', required=True)
    parser.add_argument('--release', required=True)
    args = parser.parse_args(argv)

    statements = json.loads(args.statements)
    conn = connect(args.source)
    try:
        conn.execute('BEGIN IMMEDIATE')
        for statement in statements:
            conn.execute(statement)
        if args.mode == 'pending':
            if not args.image:
                parser.error('pending mode needs --image')
            if not hasattr(conn, 'serialize'):
                raise SystemExit('This Python build cannot snapshot the writer view')
            Path(args.image).write_bytes(conn.serialize())
        else:
            conn.execute('COMMIT')
        Path(args.ready).write_text(args.mode, encoding='ascii')
        released = wait_for(args.release)
        if not released:
            print('release timeout', file=sys.stderr, flush=True)
        if args.mode == 'pending':
            conn.execute('ROLLBACK')
    finally:
        conn.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())

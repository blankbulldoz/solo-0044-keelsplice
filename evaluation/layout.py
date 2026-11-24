"""Measure the cost of the repository's own row layout.

``captures`` is declared as

    id, name, version, sealed, state, artifact, certificate

SQLite must walk a large value's overflow chain to reach columns stored after
it.  So selecting only small columns (the capture list) or only ``artifact``
(the download path) still pays for ``sealed``.  This script measures the
existing layout against candidate layouts on identical data.
"""

from __future__ import annotations

import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path

from .harness import Reader

SEALED = 's' * (4 * 1024 * 1024)
ARTIFACT = b'a' * (176 * 1024)
CERTIFICATE = 'c' * 2000


def _time(conn, query):
    with Reader() as reader:
        value = conn.execute(query).fetchall()
    return reader.bytes, value


def _build(kind, path):
    conn = sqlite3.connect(path)
    if kind == 'current':
        conn.execute('CREATE TABLE captures(id TEXT PRIMARY KEY, name TEXT, version INT, sealed TEXT, '
                     'state TEXT, artifact BLOB, certificate TEXT)')
        conn.execute('INSERT INTO captures VALUES(?,?,?,?,?,?,?)',
                     ('x', 'r', 1, SEALED, 'certified', ARTIFACT, CERTIFICATE))
    elif kind == 'reordered':
        conn.execute('CREATE TABLE captures(id TEXT PRIMARY KEY, name TEXT, version INT, state TEXT, '
                     'sealed TEXT, artifact BLOB, certificate TEXT)')
        conn.execute('INSERT INTO captures VALUES(?,?,?,?,?,?,?)',
                     ('x', 'r', 1, 'certified', SEALED, ARTIFACT, CERTIFICATE))
    elif kind == 'blobs_split':
        conn.execute('CREATE TABLE captures(id TEXT PRIMARY KEY, name TEXT, version INT, state TEXT, '
                     'certificate TEXT)')
        conn.execute('CREATE TABLE capture_blobs(id TEXT PRIMARY KEY, sealed TEXT, artifact BLOB)')
        conn.execute('INSERT INTO captures VALUES(?,?,?,?,?)', ('x', 'r', 1, 'certified', CERTIFICATE))
        conn.execute('INSERT INTO capture_blobs VALUES(?,?,?)', ('x', SEALED, ARTIFACT))
    elif kind == 'blobs_split_separate':
        # artifact and sealed in *different* tables, so neither drags the other.
        conn.execute('CREATE TABLE captures(id TEXT PRIMARY KEY, name TEXT, version INT, state TEXT, '
                     'certificate TEXT)')
        conn.execute('CREATE TABLE capture_sealed(id TEXT PRIMARY KEY, sealed TEXT)')
        conn.execute('CREATE TABLE capture_artifact(id TEXT PRIMARY KEY, artifact BLOB)')
        conn.execute('INSERT INTO captures VALUES(?,?,?,?,?)', ('x', 'r', 1, 'certified', CERTIFICATE))
        conn.execute('INSERT INTO capture_sealed VALUES(?,?)', ('x', SEALED))
        conn.execute('INSERT INTO capture_artifact VALUES(?,?)', ('x', ARTIFACT))
    conn.commit()
    conn.close()


QUERIES = {
    'current': {
        'list page (id,name,version,state)': 'SELECT id,name,version,state FROM captures',
        'trace (sealed)': 'SELECT sealed FROM captures WHERE id="x"',
        'download (artifact)': 'SELECT artifact FROM captures WHERE id="x"',
    },
    'reordered': {
        'list page (id,name,version,state)': 'SELECT id,name,version,state FROM captures',
        'trace (sealed)': 'SELECT sealed FROM captures WHERE id="x"',
        'download (artifact)': 'SELECT artifact FROM captures WHERE id="x"',
    },
    'blobs_split': {
        'list page (id,name,version,state)': 'SELECT id,name,version,state FROM captures',
        'trace (sealed)': 'SELECT sealed FROM capture_blobs WHERE id="x"',
        'download (artifact)': 'SELECT artifact FROM capture_blobs WHERE id="x"',
    },
    'blobs_split_separate': {
        'list page (id,name,version,state)': 'SELECT id,name,version,state FROM captures',
        'trace (sealed)': 'SELECT sealed FROM capture_sealed WHERE id="x"',
        'download (artifact)': 'SELECT artifact FROM capture_artifact WHERE id="x"',
    },
}


def run():
    results = {}
    with tempfile.TemporaryDirectory(prefix='keelsplice-layout-') as directory:
        for kind, queries in QUERIES.items():
            path = Path(directory) / f'{kind}.sqlite'
            _build(kind, path)
            with closing(sqlite3.connect(path)) as conn:
                results[kind] = {label: _time(conn, sql)[0] for label, sql in queries.items()}
    return {'sealed_bytes': len(SEALED), 'artifact_bytes': len(ARTIFACT), 'layouts': results}


def main():
    results = run()
    print(f"sealed={results['sealed_bytes']} artifact={results['artifact_bytes']}")
    for kind, queries in results['layouts'].items():
        print(f'  {kind}')
        for label, value in queries.items():
            print(f'    {label:<36} rchar={value:>9}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

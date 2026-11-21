"""Create SQLite fixture bytes and certify independent reads of those bytes."""

import sqlite3
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

from .catalog import inspect
from .codec import Fault, decode, digest, fingerprint, key, quote, row_values


def digest_bytes(data):
    return digest(data)


def check_references(conn):
    violation = conn.execute('PRAGMA main.foreign_key_check').fetchone()
    if violation:
        raise Fault('artifact_reference', f'Foreign key {violation[3]} failed in {violation[0]}')


def build(sealed):
    schema = sealed['schema']
    captures = sealed['result']['rows']
    with TemporaryDirectory(prefix='keelsplice-build-') as directory:
        file = Path(directory) / 'fixture.sqlite'
        with closing(sqlite3.connect(file)) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute('PRAGMA trusted_schema=OFF')
            conn.execute('PRAGMA foreign_keys=ON')
            with conn:
                conn.execute('BEGIN')
                conn.execute('PRAGMA defer_foreign_keys=ON')
                for table in schema['tables'].values():
                    conn.execute(table['ddl'])
                for index in schema['indexes']:
                    conn.execute(index['sql'])
                for table, meta in schema['tables'].items():
                    offsets = [i for i, col in enumerate(meta['columns']) if col['hidden'] == 0]
                    cols = ','.join(quote(meta['columns'][i]['name']) for i in offsets)
                    slots = ','.join('?' for _ in offsets)
                    values = ([decode(row[i]) for i in offsets] for row in captures[table].values())
                    conn.executemany(f'INSERT INTO {quote(table)} ({cols}) VALUES ({slots})', values)
                check_references(conn)
        return file.read_bytes()


def certify(data, sealed):
    with TemporaryDirectory(prefix='keelsplice-read-') as directory:
        file = Path(directory) / 'fixture.sqlite'
        file.write_bytes(data)
        with closing(sqlite3.connect(file.as_uri() + '?mode=ro', uri=True)) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute('PRAGMA trusted_schema=OFF')
            if [row[0] for row in conn.execute('PRAGMA main.integrity_check')] != ['ok']:
                raise Fault('artifact_integrity', 'SQLite integrity check failed')
            schema = inspect(conn)
            if fingerprint(schema) != sealed['schema_digest']:
                raise Fault('artifact_schema', 'Database schema differs from the sealed schema')
            check_references(conn)
            counts = {}
            for table, meta in schema['tables'].items():
                expected = sealed['result']['rows'][table]
                count = 0
                for row in conn.execute(f'SELECT * FROM {quote(table)}'):
                    actual = row_values(row, [col['name'] for col in meta['columns']])
                    token = key(row_values(row, meta['pk']))
                    if expected.get(token) != actual:
                        raise Fault('artifact_content', f'Unexpected row or values in {table}')
                    count += 1
                if count != len(expected):
                    raise Fault('artifact_content', f'Missing selected rows in {table}')
                counts[table] = count
    return {'sha256': digest_bytes(data), 'bytes': len(data), 'schema_digest': sealed['schema_digest'],
            'counts': counts, 'capture_digest': fingerprint(sealed)}

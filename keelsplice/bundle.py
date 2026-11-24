"""Create SQLite fixture bytes, certify them, and hold delivery layout profiles.

The ``default`` profile reproduces the historical builder exactly: no page-size
override and no reserved free pages. The ``wide`` profile is the second
receiver contract: 8192-byte pages with exactly eight free pages. The two are
built independently from the same sealed rows, and each certificate is bound
to the bytes of its own file.
"""

import sqlite3
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

from .catalog import inspect
from .codec import Fault, decode, digest, fingerprint, key, quote, row_values

LAYOUTS = {
    'default': {
        'label': '默认布局',
        'description': '沿用既有交付字节：SQLite 默认页大小，无空闲页。',
        'page_size': None,
        'free_pages': 0,
    },
    'wide': {
        'label': '宽页布局',
        'description': '8192 字节页，保留 8 个空闲页。',
        'page_size': 8192,
        'free_pages': 8,
    },
}

PAD_TABLE = '__keelsplice_layout_pad'


def digest_bytes(data):
    return digest(data)


def profile(name):
    if not isinstance(name, str) or name not in LAYOUTS:
        raise Fault('unknown_layout', f'Unknown delivery layout: {name!r}', 404)
    return name, LAYOUTS[name]


def check_references(conn):
    violation = conn.execute('PRAGMA main.foreign_key_check').fetchone()
    if violation:
        raise Fault('artifact_reference', f'Foreign key {violation[3]} failed in {violation[0]}')


def _pad_name(conn):
    tables = {row[0].casefold() for row in conn.execute(
        "SELECT name FROM main.sqlite_schema WHERE type='table'")}
    name = PAD_TABLE
    while name.casefold() in tables:
        name += '_'
    return name


def _reserve_free_pages(conn, target):
    """Leave exactly ``target`` free pages in the main database."""
    if target <= 0:
        return
    page_size = conn.execute('PRAGMA main.page_size').fetchone()[0]
    overflow = page_size - 4
    # One page holds the table root; every further page is one overflow page.
    size = page_size - 35 + max(0, target - 2) * overflow
    table = quote(_pad_name(conn))
    for _ in range(12):
        conn.execute(f'CREATE TABLE {table} (payload BLOB)')
        if target > 1:
            conn.execute(f'INSERT INTO {table} VALUES (zeroblob(?))', (max(0, size),))
        conn.execute(f'DROP TABLE {table}')
        actual = conn.execute('PRAGMA main.freelist_count').fetchone()[0]
        if actual == target:
            return
        size += (target - actual) * overflow
        size = max(0, size)
    raise Fault('artifact_layout', f'Could not leave {target} free pages in the fixture')


def _physical(conn):
    return {
        'page_size': conn.execute('PRAGMA main.page_size').fetchone()[0],
        'free_pages': conn.execute('PRAGMA main.freelist_count').fetchone()[0],
        'page_count': conn.execute('PRAGMA main.page_count').fetchone()[0],
    }


def _check_physical(name, wanted, actual):
    if name == 'default':
        return
    if wanted['page_size'] is not None and actual['page_size'] != wanted['page_size']:
        raise Fault('artifact_layout',
                    f'Layout {name} needs page size {wanted["page_size"]}, found {actual["page_size"]}')
    if actual['free_pages'] != wanted['free_pages']:
        raise Fault('artifact_layout',
                    f'Layout {name} needs {wanted["free_pages"]} free pages, found {actual["free_pages"]}')


def build(sealed, layout='default'):
    name, wanted = profile(layout)
    schema = sealed['schema']
    captures = sealed['result']['rows']
    with TemporaryDirectory(prefix='keelsplice-build-') as directory:
        file = Path(directory) / 'fixture.sqlite'
        with closing(sqlite3.connect(file)) as conn:
            conn.row_factory = sqlite3.Row
            if wanted['page_size'] is not None:
                conn.execute(f'PRAGMA main.page_size={wanted["page_size"]}')
            if layout != 'default':
                conn.execute('PRAGMA main.auto_vacuum=NONE')
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
                _reserve_free_pages(conn, wanted['free_pages'])
                check_references(conn)
            _check_physical(name, wanted, _physical(conn))
        data = file.read_bytes()
        if not data:
            raise Fault('artifact_layout', 'SQLite produced an empty fixture')
        return data


def certify(data, sealed, layout='default'):
    name, wanted = profile(layout)
    with TemporaryDirectory(prefix='keelsplice-read-') as directory:
        file = Path(directory) / 'fixture.sqlite'
        file.write_bytes(data)
        with closing(sqlite3.connect(file.as_uri() + '?mode=ro', uri=True)) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute('PRAGMA trusted_schema=OFF')
            if [row[0] for row in conn.execute('PRAGMA main.integrity_check')] != ['ok']:
                raise Fault('artifact_integrity', 'SQLite integrity check failed')
            physical = _physical(conn)
            _check_physical(name, wanted, physical)
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
    certificate = {'sha256': digest_bytes(data), 'bytes': len(data), 'schema_digest': sealed['schema_digest'],
                   'counts': counts, 'capture_digest': fingerprint(sealed)}
    if name != 'default':
        certificate |= {'layout': name, 'page_size': physical['page_size'],
                        'free_pages': physical['free_pages'], 'page_count': physical['page_count']}
    return certificate

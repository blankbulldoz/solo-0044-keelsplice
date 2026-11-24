"""Verify a Keelsplice WAL retention archive with the standard library only.

The archive is meant to leave this machine, so this module embeds the small
amount of SQLite WAL reasoning it needs and never imports Keelsplice.  A copy
is placed in each archive as ``verify.py``::

    python3 verify.py /path/to/archive

Each evidence point is decided from bytes and typed SQLite values:

* the point's source material is one main database file plus one WAL segment;
* the reader image is the main file with only frames through the last commit
  frame applied -- the image a fresh SQLite reader may see;
* the workbench capture must agree with that image at typed primary keys,
  including stored generated values and the parent key reached through the
  declared foreign key;
* a valid WAL frame after the last commit belongs to a writer, not a reader,
  and the archived writer image is checked separately.

Timestamps are never consulted.  An equal schema digest is not accepted as
evidence of equal rows: the scenario relations must hold on visible values.
"""

import base64
import binascii
import hashlib
import json
import sqlite3
import struct
import sys
import tempfile
from collections import defaultdict
from pathlib import Path

MAGIC_LITTLE = 0x377F0682
MAGIC_BIG = 0x377F0683
WAL_VERSION = 3007000
PAGE_SIZES = frozenset((512, 1024, 2048, 4096, 8192, 16384, 32768, 65536))
MAX_IMAGE_BYTES = 64 * 1024 * 1024


class VerifyError(Exception):
    """A claim in the archive is not supported by its bytes."""


def dumps(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(',', ':'), allow_nan=False)


def loads(text):
    return json.loads(text)


def fingerprint(value):
    return hashlib.sha256(dumps(value).encode('ascii')).hexdigest()


def encode(value):
    if value is None or type(value) in (int, str):
        return value
    if isinstance(value, bytes):
        return {'blob': base64.b64encode(value).decode('ascii')}
    return {'real': value.hex()}


def decode(value):
    if value is None or type(value) in (int, str):
        return value
    if isinstance(value, dict) and len(value) == 1:
        try:
            if 'blob' in value and isinstance(value['blob'], str):
                return base64.b64decode(value['blob'], validate=True)
            if 'real' in value and isinstance(value['real'], str):
                return float.fromhex(value['real'])
        except (ValueError, binascii.Error):
            pass
    raise VerifyError(f'Not a canonical SQLite value: {value!r}')


def quote(identifier):
    return '"' + identifier.replace('"', '""') + '"'


def _checksum(data, seed1, seed2, big_endian):
    if len(data) % 8:
        raise VerifyError('WAL checksum input is not a whole number of 8-byte words')
    first, second = seed1, seed2
    order = '>II' if big_endian else '<II'
    for offset in range(0, len(data), 8):
        left, right = struct.unpack_from(order, data, offset)
        first = (first + left + second) & 0xFFFFFFFF
        second = (second + right + first) & 0xFFFFFFFF
    return first, second


def parse_wal(segment):
    """Validate frames and locate the last commit boundary."""
    segment = bytes(segment)
    info = {'bytes': len(segment), 'state': 'empty', 'page_size': None, 'salt': None,
            'frame_count': 0, 'valid_frames': 0, 'commit_frames': [],
            'last_commit': None, 'has_uncommitted_tail': False, '_frames': []}
    if not segment:
        return info
    if len(segment) < 32:
        raise VerifyError('WAL segment is shorter than its header')
    magic, version, page_size, _sequence, salt1, salt2, c1, c2 = struct.unpack('>IIIIIIII', segment[:32])
    if magic not in (MAGIC_LITTLE, MAGIC_BIG) or version != WAL_VERSION or page_size not in PAGE_SIZES:
        raise VerifyError('WAL header is not a supported SQLite WAL')
    big_endian = magic == MAGIC_BIG
    if _checksum(segment[:24], 0, 0, big_endian) != (c1, c2):
        raise VerifyError('WAL header checksum does not match')
    running = (c1, c2)
    offset = 32
    index = 0
    while offset + 24 + page_size <= len(segment):
        page, database_pages, fs1, fs2, fc1, fc2 = struct.unpack('>IIIIII', segment[offset:offset + 24])
        payload = segment[offset + 24:offset + 24 + page_size]
        running = _checksum(segment[offset:offset + 8], running[0], running[1], big_endian)
        running = _checksum(payload, running[0], running[1], big_endian)
        if (fs1, fs2) != (salt1, salt2) or (fc1, fc2) != running or not 1 <= page <= 0x7FFFFFFF:
            break
        info['_frames'].append({'index': index, 'page': page, 'offset': offset,
                                'commit': bool(database_pages),
                                'database_pages': database_pages})
        if database_pages:
            info['commit_frames'].append(index)
            info['last_commit'] = index
        index += 1
        offset += 24 + page_size
    info.update(state='wal' if index else 'header-only', page_size=page_size,
                salt=[salt1, salt2], frame_count=index, valid_frames=index)
    info['has_uncommitted_tail'] = index > (0 if info['last_commit'] is None else info['last_commit'] + 1)
    return info


def replay_committed(base, segment):
    """Apply only frames through the last commit frame; ignore any later ones."""
    info = parse_wal(segment)
    if info['state'] != 'wal':
        return bytes(base), info
    page_size = info['page_size']
    limit = 0 if info['last_commit'] is None else info['last_commit'] + 1
    image = bytearray(base)
    for frame in info['_frames'][:limit]:
        start = (frame['page'] - 1) * page_size
        end = start + page_size
        if end > MAX_IMAGE_BYTES:
            raise VerifyError('WAL frame address exceeds the image budget')
        if len(image) < end:
            image.extend(b'\x00' * (end - len(image)))
        image[start:end] = segment[frame['offset'] + 24:frame['offset'] + 24 + page_size]
    if info['last_commit'] is not None:
        pages = info['_frames'][info['last_commit']]['database_pages']
        size = pages * page_size
        if not size:
            raise VerifyError('Commit frame has a zero database size')
        if len(image) > size:
            del image[size:]
        elif len(image) < size:
            image.extend(b'\x00' * (size - len(image)))
    return bytes(image), info


def schema_description(conn):
    """Mirror of keelsplice.catalog.inspect for a standalone fingerprint."""
    tables = {}
    for entry in sorted(conn.execute('PRAGMA main.table_list').fetchall(), key=lambda row: row['name']):
        name = entry['name']
        if entry['schema'] != 'main' or name.startswith('sqlite_') or entry['type'] == 'view':
            continue
        if entry['type'] != 'table':
            raise VerifyError(f'Unsupported schema object {name}')
        columns = [dict(row) for row in conn.execute(f'PRAGMA main.table_xinfo({quote(name)})')]
        pk = [column['name'] for column in sorted(columns, key=lambda item: item['pk']) if column['pk']]
        ddl = conn.execute('SELECT sql FROM main.sqlite_schema WHERE type=? AND name=?',
                           ('table', name)).fetchone()[0]
        tables[name] = dict(ddl=ddl, columns=columns, pk=pk,
                            without_rowid=bool(entry['wr']), strict=bool(entry['strict']))
    if not tables:
        raise VerifyError('Replay database has no ordinary tables')
    edges = []
    for name, table in tables.items():
        groups = defaultdict(list)
        for row in conn.execute(f'PRAGMA main.foreign_key_list({quote(name)})'):
            groups[row['id']].append(row)
        for number, rows in sorted(groups.items()):
            rows.sort(key=lambda row: row['seq'])
            parent = resolve(tables, rows[0]['table'])
            child_cols = [resolve((column['name'] for column in table['columns']), row['from']) for row in rows]
            parent_cols = tables[parent]['pk'] if rows[0]['to'] is None else [
                resolve((column['name'] for column in tables[parent]['columns']), row['to']) for row in rows]
            edges.append(dict(child=name, id=number, parent=parent, child_cols=child_cols,
                              parent_cols=parent_cols, on_update=rows[0]['on_update'],
                              on_delete=rows[0]['on_delete']))
    indexes = [dict(row) for row in conn.execute(
        "SELECT name, tbl_name, sql FROM main.sqlite_schema WHERE type='index' AND sql IS NOT NULL ORDER BY name"
    ) if row['tbl_name'] in tables]
    return {'tables': tables, 'edges': edges, 'indexes': indexes}


def sql_name(name):
    return name.translate(str.maketrans('ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'))


def resolve(names, value):
    if isinstance(value, str):
        for name in names:
            if sql_name(name) == sql_name(value):
                return name
    raise VerifyError(f'Unknown identifier {value!r}')


def open_readonly(path):
    conn = sqlite3.connect(Path(path).as_uri() + '?mode=ro', uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def select_one(conn, table, key_columns, key_values):
    predicate = ' AND '.join(f'{quote(column)} = ?' for column in key_columns)
    rows = conn.execute(f'SELECT * FROM {quote(table)} WHERE {predicate}', key_values).fetchall()
    if len(rows) != 1:
        raise VerifyError(f'{table} key {list(key_values)!r} matched {len(rows)} rows')
    return rows[0]


def find_edge(schema, reference):
    if not reference:
        return None
    for edge in schema['edges']:
        if edge['child'] == reference.get('child') and edge['id'] == reference.get('id'):
            return edge
    raise VerifyError(f'Foreign key {reference!r} is not in the sealed schema')


def parent_target(conn, row, edge):
    """Return ``[parent_table, parent_key]`` using SQLite comparison semantics."""
    if edge is None:
        return None
    values = [row[column] for column in edge['child_cols']]
    if any(value is None for value in values):
        return None
    predicate = ' AND '.join(f'{quote(column)} = ?' for column in edge['parent_cols'])
    columns = ', '.join(quote(column) for column in edge['parent_cols'])
    rows = conn.execute(f'SELECT {columns} FROM {quote(edge["parent"])} WHERE {predicate}', values).fetchall()
    if len(rows) != 1:
        raise VerifyError(f'Expected one parent in {edge["parent"]} for {values!r}, found {len(rows)}')
    return [edge['parent'], [encode(value) for value in rows[0]]]


def derive_from_database(conn, schema, probe):
    table = probe['table']
    meta = schema['tables'][table]
    row = select_one(conn, table, meta['pk'], [decode(value) for value in probe['key']])
    columns = {column: encode(row[column]) for column in probe['columns']}
    parent = parent_target(conn, row, find_edge(schema, probe.get('parent_edge')))
    return {'columns': columns, 'parent': parent}


def derive_from_capture(sealed, probe):
    table = probe['table']
    meta = sealed['schema']['tables'][table]
    names = [column['name'] for column in meta['columns']]
    token = dumps(probe['key'])
    rows = sealed['result']['rows'][table]
    if token not in rows:
        raise VerifyError(f'Capture {table} {probe["key"]!r} is absent')
    stored = dict(zip(names, rows[token]))
    columns = {column: stored[column] for column in probe['columns']}
    parent = None
    reference = probe.get('parent_edge')
    if reference:
        wanted = [reference['child'], reference['id']]
        targets = [arc['target'] for arc in sealed['result']['arcs']
                   if arc['origin'] == [table, probe['key']]
                   and arc['direction'] == 'parent' and arc['edge'] == wanted]
        if len(targets) > 1:
            raise VerifyError(f'Capture has duplicate parent witnesses for {table} {probe["key"]!r}')
        parent = targets[0] if targets else None
    return {'columns': columns, 'parent': parent}


def check_capture_rows(conn, sealed, kind):
    """Every captured row must exist with identical values in the replay image."""
    schema = sealed['schema']
    for table, rows in sealed['result']['rows'].items():
        meta = schema['tables'][table]
        names = [column['name'] for column in meta['columns']]
        for token, stored in rows.items():
            key = loads(token)
            row = select_one(conn, table, meta['pk'], [decode(value) for value in key])
            for name, expected in zip(names, stored):
                if encode(row[name]) != expected:
                    raise VerifyError(f'{kind}: {table} {key!r} column {name} differs from the replay image')


def check_generated(probe, visible):
    """A stored generated value must equal what SQLite derives from its input."""
    if 'doubled' in probe['columns'] and 'length_um' in probe['columns']:
        length = decode(visible['columns']['length_um'])
        doubled = decode(visible['columns']['doubled'])
        if doubled != length * 2:
            raise VerifyError(f'{probe["id"]}: doubled {doubled!r} is not twice length_um {length!r}')


def relation_holds(relation, views):
    left, right, kind = relation['left'], relation['right'], relation['relation']
    if kind == 'same_visible':
        return views[left] == views[right]
    if kind == 'different_visible':
        return views[left] != views[right]
    raise VerifyError(f'Unknown relation {kind!r}')


def wal_prefix_matches(earlier, later):
    """True when ``earlier`` valid frames are byte-identical to the start of ``later``."""
    first, second = parse_wal(earlier), parse_wal(later)
    if first['state'] != 'wal' or second['state'] != 'wal' or first['page_size'] != second['page_size']:
        return False
    if first['salt'] != second['salt'] or second['valid_frames'] < first['valid_frames']:
        return False
    size = 24 + first['page_size']
    for a, b in zip(first['_frames'], second['_frames']):
        start = a['offset']
        if earlier[start:start + size] != later[b['offset']:b['offset'] + size]:
            return False
    return True


def verify_archive(root):
    root = Path(root)
    manifest = loads((root / 'manifest.json').read_text(encoding='utf-8'))
    if manifest.get('format') != 'keelsplice-retention/1':
        raise VerifyError('manifest format is not keelsplice-retention/1')
    checks = []

    def check(name, condition, detail=''):
        checks.append({'check': name, 'ok': bool(condition), 'detail': detail})
        if not condition:
            raise VerifyError(f'{name}: {detail}')

    probes = manifest['probes']
    points = manifest['points']
    ids = [point['id'] for point in points]
    check('point-ids-unique', len(ids) == len(set(ids)), f'points {ids!r}')
    main_hashes = set()
    schema_digests = set()
    views = {}
    segments = {}
    content_digests = {}

    with tempfile.TemporaryDirectory(prefix='keelsplice-verify-') as directory:
        directory = Path(directory)
        for point in points:
            label = point['id']
            base = (root / point['main']['file']).read_bytes()
            segment = (root / point['wal']['file']).read_bytes()
            segments[label] = segment
            main_hashes.add(hashlib.sha256(base).hexdigest())
            check(f'{label}-main-hash', hashlib.sha256(base).hexdigest() == point['main']['sha256'],
                  'main database bytes match manifest')
            check(f'{label}-wal-hash', hashlib.sha256(segment).hexdigest() == point['wal']['sha256'],
                  'WAL segment bytes match manifest')

            info = parse_wal(segment)
            for field in ('state', 'frame_count', 'valid_frames', 'commit_frames',
                          'last_commit', 'has_uncommitted_tail', 'salt'):
                check(f'{label}-wal-{field}', info[field] == point['wal'][field],
                      f'derived {info[field]!r}, manifest {point["wal"][field]!r}')

            image, _ = replay_committed(base, segment)
            image_path = directory / f'{label}-replay.sqlite'
            image_path.write_bytes(image)

            replay_dir = directory / f'{label}-sqlite'
            replay_dir.mkdir()
            (replay_dir / 'source.sqlite').write_bytes(base)
            if segment:
                (replay_dir / 'source.sqlite-wal').write_bytes(segment)
            databases = {'raw': open_readonly(image_path),
                         'sqlite': open_readonly(replay_dir / 'source.sqlite')}
            try:
                for kind, conn in databases.items():
                    integrity = [row[0] for row in conn.execute('PRAGMA integrity_check')]
                    check(f'{label}-{kind}-integrity', integrity == ['ok'], 'SQLite integrity check')
                    check(f'{label}-{kind}-foreign-keys',
                          conn.execute('PRAGMA foreign_key_check').fetchall() == [],
                          'no dangling foreign keys')
                    schema = schema_description(conn)
                    digest = fingerprint(schema)
                    schema_digests.add(digest)
                    check(f'{label}-{kind}-schema', digest == point['schema_digest'],
                          'replayed schema fingerprint equals the sealed digest')

                capture_bytes = (root / point['capture']['file']).read_bytes()
                check(f'{label}-capture-hash', hashlib.sha256(capture_bytes).hexdigest()
                      == point['capture']['sha256'], 'capture record bytes match manifest')
                record = loads(capture_bytes)
                sealed = record['sealed']
                check(f'{label}-sealed-digest',
                      fingerprint(sealed) == point['capture']['sealed_digest'],
                      'sealed capture digest matches manifest')
                check(f'{label}-schema-digest-match',
                      sealed['schema_digest'] == point['schema_digest'],
                      'capture and point agree on the schema digest')
                content_digests[label] = fingerprint(sealed['result'])

                for kind, conn in databases.items():
                    check_capture_rows(conn, sealed, f'{label}-{kind}')

                visible = {}
                for probe in probes:
                    from_capture = derive_from_capture(sealed, probe)
                    from_raw = derive_from_database(databases['raw'], sealed['schema'], probe)
                    from_sqlite = derive_from_database(databases['sqlite'], sealed['schema'], probe)
                    expected = point['visible'][probe['id']]
                    check(f'{label}-{probe["id"]}-capture-visible', from_capture == expected,
                          f'capture {from_capture!r}, expected {expected!r}')
                    check(f'{label}-{probe["id"]}-raw-replay', from_raw == expected,
                          f'replay {from_raw!r}, expected {expected!r}')
                    check(f'{label}-{probe["id"]}-sqlite-replay', from_sqlite == expected,
                          f'SQLite replay {from_sqlite!r}, expected {expected!r}')
                    check_generated(probe, from_capture)
                    visible[probe['id']] = from_capture
                views[label] = visible

                writer = point.get('writer_image')
                if writer:
                    writer_bytes = (root / writer['file']).read_bytes()
                    check(f'{label}-writer-hash',
                          hashlib.sha256(writer_bytes).hexdigest() == writer['sha256'],
                          'writer image bytes match manifest')
                    writer_path = directory / f'{label}-writer.sqlite'
                    writer_path.write_bytes(writer_bytes)
                    with open_readonly(writer_path) as conn:
                        check(f'{label}-writer-integrity',
                              [row[0] for row in conn.execute('PRAGMA integrity_check')] == ['ok'],
                              'writer image integrity')
                        writer_schema = schema_description(conn)
                        check(f'{label}-writer-schema',
                              fingerprint(writer_schema) == point['schema_digest'],
                              'writer image has the sealed schema')
                        writer_visible = {}
                        for probe in probes:
                            derived = derive_from_database(conn, sealed['schema'], probe)
                            check(f'{label}-{probe["id"]}-writer-visible',
                                  derived == point['writer_visible'][probe['id']],
                                  f'writer image {derived!r}, expected {point["writer_visible"][probe["id"]]!r}')
                            writer_visible[probe['id']] = derived
                    views[f'writer:{label}'] = writer_visible
            finally:
                for conn in databases.values():
                    conn.close()

    check('main-file-shared', len(main_hashes) == 1,
          'every point uses the same main database bytes; commits live in the WAL')
    check('schema-digest-shared', len(schema_digests) == 1,
          f'all points replay the same schema: {sorted(schema_digests)!r}')

    for relation in manifest.get('relations', []):
        check(f'relation-{relation["left"]}-{relation["right"]}-{relation["relation"]}',
              relation_holds(relation, views), relation.get('why', ''))

    # A shared digest must not stand in for row identity.  Each declared
    # relation is re-decided from the sealed content digest of the captures
    # (not from the schema digest), so an equal schema digest cannot carry the
    # claim on its own.
    for relation in manifest.get('relations', []):
        left, right = relation['left'], relation['right']
        sealed_equal = content_digests[left] == content_digests[right]
        check(f'content-{left}-{right}-{relation["relation"]}',
              sealed_equal == (relation['relation'] == 'same_visible'),
              f'sealed content digests {content_digests[left][:12]} and {content_digests[right][:12]}')

    # Committed WAL generations: later segments extend earlier ones and the
    # last commit ordinal only grows.  No timestamps are consulted.
    for point in points:
        extends = point.get('extends')
        if extends:
            check(f'{point["id"]}-extends-{extends}',
                  wal_prefix_matches(segments[extends], segments[point['id']]),
                  'later committed WAL keeps the earlier valid frames and salt')
            check(f'{point["id"]}-commit-grows',
                  point['wal']['last_commit'] is not None
                  and next(item for item in points if item['id'] == extends)['wal']['last_commit']
                  is not None
                  and point['wal']['last_commit']
                  > next(item for item in points if item['id'] == extends)['wal']['last_commit'],
                  'commit frame ordinal increases')
    ordinals = [point['wal']['last_commit'] for point in points if point['wal']['last_commit'] is not None]
    check('commit-ordinals-increase', ordinals == sorted(ordinals),
          f'commit frame ordinals {ordinals!r}')

    pending = manifest.get('pending_absent', {})
    for label, visible in views.items():
        if label.startswith('writer:'):
            continue
        for probe_id, staged in pending.items():
            check(f'{label}-{probe_id}-pending-absent', visible[probe_id] != staged,
                  'the uncommitted writer image is not reader-visible')

    return {'ok': True, 'checks': checks, 'points': ids,
            'schema_digests': sorted(schema_digests),
            'content_digests': content_digests, 'visible': views}


def readable(value):
    """Turn encoded typed values into something a reader can scan directly."""
    if isinstance(value, dict) and 'real' in value:
        return float.fromhex(value['real'])
    if isinstance(value, dict) and 'blob' in value:
        return f"blob({len(decode(value))} bytes)"
    return value


def point_report(report, probes):
    """Summarise the derived reader view at each point for the audit log."""
    def rows(visible):
        return {
            probe['id']: {
                'columns': {name: readable(value)
                            for name, value in visible[probe['id']]['columns'].items()},
                'parent': visible[probe['id']]['parent'],
            }
            for probe in probes
        }

    summary = {'reader': {}}
    for label, visible in report['visible'].items():
        if label.startswith('writer:'):
            summary.setdefault('writer', {})[label.split(':', 1)[1]] = rows(visible)
        else:
            summary['reader'][label] = rows(visible)
    return summary


def main(argv):
    root = Path(argv[1]) if len(argv) > 1 else Path('.')
    try:
        report = verify_archive(root)
    except (VerifyError, OSError, ValueError, KeyError, IndexError, sqlite3.Error) as exc:
        print(f'FAIL: {exc}')
        return 1
    for check in report['checks']:
        print(('PASS' if check['ok'] else 'FAIL') + ' ' + check['check'])
    probes = loads((root / 'manifest.json').read_text(encoding='utf-8'))['probes']
    print(dumps({'ok': True, 'points': report['points'],
                 'schema_digests': report['schema_digests'],
                 'content_digests': report['content_digests'],
                 'visible': point_report(report, probes),
                 'checks': len(report['checks'])}))
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))

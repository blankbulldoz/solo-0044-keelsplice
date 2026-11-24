"""Inspect and normalize a SQLite main schema within the caller's snapshot."""

from collections import defaultdict

from .codec import Fault, fingerprint, quote


def sql_name(name):
    return name.translate(str.maketrans('ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'))


def resolve(names, value):
    if isinstance(value, str):
        for name in names:
            if sql_name(name) == sql_name(value):
                return name
    raise Fault('unknown_identifier', f'Unknown table or column: {value!r}')


def pk_order(conn, name, pk):
    """Read the effective SQLite ordering of a primary key from its PK index."""
    position = {column: index for index, column in enumerate(pk)}
    default = [dict(column=column, collation='BINARY', descending=False) for column in pk]
    index = next((row for row in conn.execute(f'PRAGMA main.index_list({quote(name)})')
                  if row['origin'] == 'pk'), None)
    if index is None:
        return default
    found = {}
    for part in conn.execute(f'PRAGMA main.index_xinfo({quote(index["name"])})'):
        if part['key'] and part['name'] in position:
            found[part['name']] = part
    if len(found) != len(pk):
        return default
    return [dict(column=part['name'], collation=part['coll'] or 'BINARY', descending=bool(part['desc']))
            for part in sorted(found.values(), key=lambda item: item['seqno'])]


def inspect(conn):
    tables = {}
    listings = conn.execute('PRAGMA main.table_list').fetchall()
    for entry in sorted(listings, key=lambda row: row['name']):
        name = entry['name']
        if entry['schema'] != 'main' or name.startswith('sqlite_') or entry['type'] == 'view':
            continue
        if entry['type'] != 'table':
            raise Fault('unsupported_schema', f'Table {name} is {entry["type"]}')
        columns = [dict(row) for row in conn.execute(f'PRAGMA main.table_xinfo({quote(name)})')]
        pk = [col['name'] for col in sorted(columns, key=lambda x: x['pk']) if col['pk']]
        if not pk:
            raise Fault('unsupported_schema', f'Table {name} needs a declared primary key')
        ddl = conn.execute('SELECT sql FROM main.sqlite_schema WHERE type=? AND name=?', ('table', name)).fetchone()[0]
        tables[name] = dict(ddl=ddl, columns=columns, pk=pk, key_order=pk_order(conn, name, pk),
                            without_rowid=bool(entry['wr']), strict=bool(entry['strict']))
    if not tables:
        raise Fault('unsupported_schema', 'No ordinary tables found')
    edges = []
    for name, table in tables.items():
        groups = defaultdict(list)
        for row in conn.execute(f'PRAGMA main.foreign_key_list({quote(name)})'):
            groups[row['id']].append(row)
        for number, rows in sorted(groups.items()):
            rows.sort(key=lambda row: row['seq'])
            parent = resolve(tables, rows[0]['table'])
            child_cols = [resolve((c['name'] for c in table['columns']), row['from']) for row in rows]
            parent_cols = tables[parent]['pk'] if rows[0]['to'] is None else [
                resolve((c['name'] for c in tables[parent]['columns']), row['to']) for row in rows]
            if len(parent_cols) != len(child_cols):
                raise Fault('unsupported_schema', f'Mismatched foreign key dimensions in {name}')
            edges.append(dict(child=name, id=number, parent=parent, child_cols=child_cols,
                              parent_cols=parent_cols, on_update=rows[0]['on_update'], on_delete=rows[0]['on_delete']))
    indexes = [dict(row) for row in conn.execute(
        "SELECT name, tbl_name, sql FROM main.sqlite_schema WHERE type='index' AND sql IS NOT NULL ORDER BY name"
    ) if row['tbl_name'] in tables]
    return {'tables': tables, 'edges': edges, 'indexes': indexes}


def schema_digest(schema):
    """Hash the declared schema, leaving out the derived primary-key ordering."""
    declared = {'tables': {name: {key: value for key, value in table.items() if key != 'key_order'}
                           for name, table in schema['tables'].items()},
                'edges': schema['edges'], 'indexes': schema['indexes']}
    return fingerprint(declared)


def snapshot(conn):
    schema = inspect(conn)
    return {'schema': schema, 'schema_digest': schema_digest(schema)}

#!/usr/bin/env python3
"""Receiver B: opens the handoff with sqlite3 and audits it independently.

Every list is recomputed from the file; nothing trusts the producer's answers.
Collations, the GENERATED declaration, primary keys and edges are read from the
database itself.  The roster only supplies the formula the receivers agreed to
recheck (which column, which input, what factor), because a formula is not a
schema fact.

`--export-material` additionally diffs a plain CSV export from that material
against the database, to measure what that exchange would have dropped.

Exit status: 0 clean, 1 unreadable material, 2 findings present (integrity,
byte/count mismatch, or foreign-key violations).  The control fixture is meant
to produce 2.
"""

import argparse
import base64
import csv
import hashlib
import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

def quote(name):
    return '"' + name.replace('"', '""') + '"'


def sha256(path):
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for block in iter(lambda: handle.read(1 << 20), b''):
            digest.update(block)
    return digest.hexdigest()


def typed(value):
    if value is None:
        return {'t': 'null'}
    if isinstance(value, int):
        return {'t': 'int', 'v': str(value)}
    if isinstance(value, float):
        return {'t': 'real', 'h': value.hex()}
    if isinstance(value, bytes):
        return {'t': 'blob', 'b64': base64.b64encode(value).decode('ascii')}
    return {'t': 'text', 'v': value}


def label(values):
    """Canonical row identity, identical to the CSV receiver's labels."""
    return json.dumps([typed(value) for value in values], sort_keys=True, separators=(',', ':'))


def schema(conn):
    tables = {}
    for (name,) in conn.execute("SELECT name FROM main.sqlite_schema WHERE type='table' "
                                "AND name NOT LIKE 'sqlite_%' ORDER BY name"):
        columns = [dict(row) for row in conn.execute(f'PRAGMA main.table_xinfo({quote(name)})')]
        tables[name] = {
            'columns': [[col['name'], col['type'], col['notnull'], col['hidden']]
                        for col in columns if col['hidden'] != 1],
            'pk': [col['name'] for col in sorted(columns, key=lambda col: col['pk']) if col['pk']],
            'generated': [col['name'] for col in columns if col['hidden'] in (2, 3)],
            'ddl': conn.execute("SELECT sql FROM main.sqlite_schema WHERE type='table' AND name=?",
                                (name,)).fetchone()[0],
        }
    edges = []
    for name in tables:
        groups = {}
        for row in conn.execute(f'PRAGMA main.foreign_key_list({quote(name)})'):
            groups.setdefault(row['id'], []).append(dict(row))
        for number, rows in sorted(groups.items()):
            rows.sort(key=lambda row: row['seq'])
            parent = rows[0]['table']
            parent_cols = [row['to'] for row in rows]
            if any(col is None for col in parent_cols):
                parent_cols = tables[parent]['pk']
            edges.append({'child': name, 'id': number, 'parent': parent,
                          'child_cols': [row['from'] for row in rows], 'parent_cols': parent_cols})
    collations = {}
    for name in tables:
        for index in conn.execute(f'PRAGMA main.index_list({quote(name)})'):
            if index['origin'] != 'pk':
                continue
            for info in conn.execute(f'PRAGMA main.index_xinfo({quote(index["name"])})'):
                if info['key'] and info['name'] is not None:
                    collations[(name, info['name'])] = info['coll']
    return tables, edges, collations


def rows_with(conn, edge, tables, where):
    pk = tables[edge['child']]['pk']
    keys = [f'c.{quote(col)}' for col in pk]
    sql = (f'SELECT {", ".join(keys)} FROM {quote(edge["child"])} c WHERE {where} '
           f'ORDER BY {", ".join(keys)}')
    return [label(row) for row in conn.execute(sql)]


def null_component(conn, edge, tables):
    clause = ' OR '.join(f'c.{quote(col)} IS NULL' for col in edge['child_cols'])
    return rows_with(conn, edge, tables, clause)


def missing_parent(conn, edge, collations, tables):
    """Complete child key with no parent row, compared under the parent's collation.

    Written `parent = +child COLLATE <declared>` on purpose.  A bare `p = c` lets
    the child column's own BINARY collation win when the child table has an
    index; `COLLATE BINARY` would kill NOCASE outright.  Unary plus drops the
    child's affinity and leaves the parent operand's declared collation in charge.
    """
    usable = ' AND '.join(f'c.{quote(col)} IS NOT NULL' for col in edge['child_cols'])
    clauses = []
    for parent_col, child_col in zip(edge['parent_cols'], edge['child_cols']):
        collation = collations.get((edge['parent'], parent_col), 'BINARY')
        clauses.append(f'p.{quote(parent_col)} = +c.{quote(child_col)} COLLATE {collation}')
    where = (f'{usable} AND NOT EXISTS (SELECT 1 FROM {quote(edge["parent"])} p '
             f'WHERE {" AND ".join(clauses)})')
    return rows_with(conn, edge, tables, where)


def collation_only(conn, edge, collations, tables):
    """Rows that match a parent only after the declared case folding is applied."""
    usable = ' AND '.join(f'c.{quote(col)} IS NOT NULL' for col in edge['child_cols'])
    folded, strict = [], []
    for parent_col, child_col in zip(edge['parent_cols'], edge['child_cols']):
        collation = collations.get((edge['parent'], parent_col), 'BINARY')
        folded.append(f'p.{quote(parent_col)} = +c.{quote(child_col)} COLLATE {collation}')
        strict.append(f'p.{quote(parent_col)} = +c.{quote(child_col)} COLLATE BINARY')
    where = (f'{usable} '
             f'AND EXISTS (SELECT 1 FROM {quote(edge["parent"])} p WHERE {" AND ".join(folded)}) '
             f'AND NOT EXISTS (SELECT 1 FROM {quote(edge["parent"])} p WHERE {" AND ".join(strict)})')
    return rows_with(conn, edge, tables, where)


def recompute(conn, roster, tables):
    results = {}
    for table, rule in roster['tables'].items():
        if table not in tables:
            continue
        formula = rule.get('generated') or rule.get('claimed')
        if formula is None:
            results[table] = {'declaration': None}
            continue
        column, source, factor = formula['column'], formula['input'], formula['factor']
        declared = [name for name, _type, _notnull, hidden in tables[table]['columns']
                    if name == column and hidden in (2, 3)]
        expression = f'{quote(source)} * {factor!r}'
        mismatch = [label(row) for row in conn.execute(
            f'SELECT {", ".join(quote(col) for col in tables[table]["pk"])} FROM {quote(table)} '
            f'WHERE {quote(column)} IS NOT ({expression}) ORDER BY 1')]
        results[table] = {'declaration': 'declared' if declared else 'none',
                          'column': column, 'declared_generated_columns': declared,
                          'checked': conn.execute(f'SELECT count(*) FROM {quote(table)}').fetchone()[0],
                          'mismatch': mismatch, 'expression': expression}
        if not declared:
            results[table]['note'] = ('the file stores this column; a formula that happens to fit does '
                                      'not make it generated')
    return results


def fk_rows(conn):
    return [{'table': row[0], 'rowid': row[1], 'parent': row[2], 'fkid': row[3]}
            for row in conn.execute('PRAGMA main.foreign_key_check')]


def compare_export(conn, material, roster, dataset):
    """Diff a plain CSV export against the database and record what it lost.

    Rows are keyed by the typed primary key, so a finding points at a row the
    other receiver also names.  The counts are measured, not assumed.
    """
    directory = material / 'naive' / dataset
    if not directory.is_dir():
        return None
    result = {'dataset': dataset, 'tables': {}, 'losses': [],
              'loss_counts': {'null_spelling': 0, 'empty_text_spelling': 0,
                              'real_inexact': 0, 'blob_as_text': 0, 'storage_class': 0}}
    for table, rule in roster['tables'].items():
        path = directory / f'{table}.csv'
        if not path.is_file():
            continue
        columns, pk = rule['columns'], rule['pk']
        with path.open(newline='', encoding='utf-8') as handle:
            exported = list(csv.reader(handle))
        if not exported or exported[0] != columns:
            result['losses'].append({'table': table, 'note': 'export header differs from the roster'})
            continue
        actual = conn.execute(
            f'SELECT {",".join(quote(col) for col in columns)} FROM {quote(table)} '
            f'ORDER BY {",".join(quote(col) for col in pk)}').fetchall()
        counts = {name: {'null': 0, 'empty_text': 0, 'blob': 0, 'real_inexact': 0,
                         'non_text_cells': 0} for name in columns}
        if len(exported) - 1 != len(actual):
            result['losses'].append({'table': table,
                                     'note': f'export has {len(exported) - 1} rows, database has {len(actual)}'})
        for db_row, cells in zip(actual, exported[1:]):
            if len(cells) != len(columns):
                result['losses'].append({'table': table, 'note': 'short export row'})
                continue
            where = label([db_row[columns.index(name)] for name in pk])
            for position, (name, value) in enumerate(zip(columns, db_row)):
                cell = cells[position]
                stats = counts[name]
                if value is not None and not isinstance(value, (str, bytes)):
                    stats['non_text_cells'] += 1
                    result['loss_counts']['storage_class'] += 1
                if value is None:
                    stats['null'] += 1
                    result['loss_counts']['null_spelling'] += 1
                    if cell != '':
                        result['losses'].append({'table': table, 'row': where, 'column': name,
                                                 'lost': 'NULL did not use the empty-field convention'})
                elif isinstance(value, float):
                    try:
                        reproduced = float(cell) == value
                    except (TypeError, ValueError):
                        reproduced = False
                    if not reproduced:
                        stats['real_inexact'] += 1
                        result['loss_counts']['real_inexact'] += 1
                        result['losses'].append({'table': table, 'row': where, 'column': name,
                                                 'lost': f'REAL {value.hex()} printed as {cell!r}'})
                elif isinstance(value, bytes):
                    stats['blob'] += 1
                    result['loss_counts']['blob_as_text'] += 1
                    result['loss_counts']['storage_class'] += 1
                    if cell != value.hex():
                        result['losses'].append({'table': table, 'row': where, 'column': name,
                                                 'lost': 'blob bytes no longer recoverable'})
                elif isinstance(value, str):
                    if value == '':
                        stats['empty_text'] += 1
                        result['loss_counts']['empty_text_spelling'] += 1
                    elif cell != value:
                        result['losses'].append({'table': table, 'row': where, 'column': name,
                                                 'lost': 'text spelling changed'})
        result['tables'][table] = {'rows': len(actual), 'columns': counts}
    # Ambiguity is a property of the column, not of one row: an empty field means
    # NULL, so any column that holds NULL, empty text or both cannot be split.
    for table, stats in result['tables'].items():
        for column, counts in stats['columns'].items():
            if counts['null'] and counts['empty_text']:
                result['losses'].append({'table': table, 'column': column,
                                         'lost': f"NULL ({counts['null']}) and empty text "
                                                 f"({counts['empty_text']}) share one spelling"})
            elif counts['null']:
                result['losses'].append({'table': table, 'column': column,
                                         'lost': f"NULL ({counts['null']}) is spelled like empty text"})
            elif counts['empty_text']:
                result['losses'].append({'table': table, 'column': column,
                                         'lost': f"empty text ({counts['empty_text']}) is indistinguishable from NULL"})
            if counts['blob']:
                result['losses'].append({'table': table, 'column': column,
                                         'lost': f"blob ({counts['blob']}) became hex text; the storage class is gone"})
            if counts['non_text_cells']:
                result['losses'].append({'table': table, 'column': column,
                                         'lost': f"{counts['non_text_cells']} non-text cells arrived as text; "
                                                 f"the storage class is gone"})
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('material', type=Path)
    parser.add_argument('--database', default='artifact.sqlite')
    parser.add_argument('--certificate', default='certificate.json')
    parser.add_argument('--dataset', default='certified')
    parser.add_argument('--no-certificate', action='store_true',
                        help='audit a fixture that has no certificate (the control)')
    parser.add_argument('--export-material', type=Path,
                        help='material-a-csv directory whose plain export should be diffed')
    parser.add_argument('--json-out', type=Path)
    args = parser.parse_args(argv)
    database = args.material / args.database
    if not database.is_file():
        print(json.dumps({'receiver': 'sqlite3', 'error': f'no database at {database}'}))
        return 1
    roster = json.loads((args.material / 'roster.json').read_text(encoding='utf-8'))
    certificate = None
    if not args.no_certificate:
        certificate = json.loads((args.material / args.certificate).read_text(encoding='utf-8'))
    digest = sha256(database)
    report = {'receiver': 'sqlite3', 'material': str(args.material),
              'database': str(database), 'sha256': digest,
              'bytes': database.stat().st_size, 'has_certificate': certificate is not None}
    if certificate is not None:
        report |= {'certificate_sha256': certificate['sha256'],
                   'certificate_bytes': certificate['bytes'],
                   'bytes_match_certificate': digest == certificate['sha256']}
    with closing(sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        tables, edges, collations = schema(conn)
        report['declared'] = {
            'tables': {name: {'pk': meta['pk'], 'generated': meta['generated'],
                              'columns': [[name_, type_] for name_, type_, _notnull, hidden in meta['columns']
                                          if hidden == 0]}
                       for name, meta in tables.items()},
            'edges': edges,
            'pk_collations': {f'{table}.{column}': collation
                              for (table, column), collation in sorted(collations.items())
                              if table in tables},
        }
        report['integrity'] = [row[0] for row in conn.execute('PRAGMA main.integrity_check')]
        report['foreign_key_check'] = fk_rows(conn)
        report['counts'] = {table: conn.execute(f'SELECT count(*) FROM {quote(table)}').fetchone()[0]
                            for table in tables}
        if certificate is not None:
            report['counts_match_certificate'] = report['counts'] == certificate['counts']
        report['null_component'] = {}
        report['missing_parent'] = {}
        report['collation_only'] = {}
        for edge in edges:
            name = f'{edge["child"]}#{edge["id"]}'
            report['null_component'][name] = null_component(conn, edge, tables)
            report['missing_parent'][name] = missing_parent(conn, edge, collations, tables)
            report['collation_only'][name] = collation_only(conn, edge, collations, tables)
        report['recompute'] = recompute(conn, roster, tables)
        report['limitations'] = [
            'SQLite cannot report which rows a CSV export dropped; it only sees the database.',
            'foreign_key_check treats a NULL key component as satisfied, so it never separates NULL '
            'from a genuinely absent parent; null_component and missing_parent are separate on purpose.',
            'The GENERATED declaration is visible here; a CSV receiver must be told it by a roster.',
        ]
        if args.export_material is not None:
            report['export_comparison'] = compare_export(conn, args.export_material, roster, args.dataset)
    text = json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False)
    print(text)
    if args.json_out:
        args.json_out.write_text(text + '\n', encoding='utf-8')
    ok = (report['integrity'] == ['ok'] and not report['foreign_key_check']
          and report.get('bytes_match_certificate', True)
          and report.get('counts_match_certificate', True))
    return 0 if ok else 2


if __name__ == '__main__':
    sys.exit(main())

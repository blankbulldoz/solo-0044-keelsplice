#!/usr/bin/env python3
"""Receiver A: standard library CSV and JSON only.

Material layout written by examples/handoff_drill.py:

  roster.json                    schema, primary keys, the focus edge, each
                                 parent-key column's declared collation, and
                                 the column formulas the roster licenses
  typed/<dataset>/<table>.csv    one typed JSON cell per column
  naive/<dataset>/<table>.csv    lossy plain-text export of the same rows

A typed cell states its SQLite storage class, so a CSV cell can still be exact:

  {"t":"null"}  {"t":"text","v":"..."}  {"t":"int","v":"11"}
  {"t":"real","h":"0x1.a000000000000p+1"}  {"t":"blob","b64":"AAD/"}

`--mode naive` reads the plain export under an "empty field means NULL"
convention and reports what that convention costs.  This module never imports
sqlite3, so it cannot open the certified database at all.

Exit status: 0 clean, 1 unreadable material, 2 integrity problems in the rows.
"""

import argparse
import base64
import binascii
import csv
import json
import sys
from pathlib import Path

ASCII_FOLD = str.maketrans('ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz',
                           'abcdefghijklmnopqrstuvwxyzabcdefghijklmnopqrstuvwxyz')
TAGS = {'null', 'text', 'int', 'real', 'blob'}


class MaterialError(Exception):
    pass


def typed_cell(text, where):
    try:
        cell = json.loads(text)
    except ValueError as exc:
        raise MaterialError(f'{where}: cell is not JSON') from exc
    if not isinstance(cell, dict) or cell.get('t') not in TAGS:
        raise MaterialError(f'{where}: cell has no known type tag')
    tag, keys = cell['t'], set(cell)
    if tag == 'null':
        if keys != {'t'}:
            raise MaterialError(f'{where}: NULL cell carries extra fields')
    elif tag == 'text':
        if keys != {'t', 'v'} or not isinstance(cell['v'], str):
            raise MaterialError(f'{where}: text value is not a string')
    elif tag == 'int':
        if keys != {'t', 'v'} or not isinstance(cell['v'], str):
            raise MaterialError(f'{where}: malformed int cell')
        try:
            value = int(cell['v'])
        except ValueError as exc:
            raise MaterialError(f'{where}: int value is not an integer') from exc
        if str(value) != cell['v']:
            raise MaterialError(f'{where}: int value is not canonical')
    elif tag == 'real':
        if keys != {'t', 'h'} or not isinstance(cell['h'], str):
            raise MaterialError(f'{where}: malformed real cell')
        try:
            cell['_f'] = float.fromhex(cell['h'])
        except ValueError as exc:
            raise MaterialError(f'{where}: real value is not a hex float') from exc
        if cell['_f'].hex() != cell['h']:
            raise MaterialError(f'{where}: real value is not canonical')
    else:
        if keys != {'t', 'b64'} or not isinstance(cell['b64'], str):
            raise MaterialError(f'{where}: malformed blob cell')
        try:
            cell['_b'] = base64.b64decode(cell['b64'], validate=True)
        except (ValueError, binascii.Error) as exc:
            raise MaterialError(f'{where}: blob is not canonical base64') from exc
        if base64.b64encode(cell['_b']).decode('ascii') != cell['b64']:
            raise MaterialError(f'{where}: blob is not canonical base64')
    return cell


def plain(cell):
    return {key: value for key, value in cell.items() if not key.startswith('_')}


def key_token(cells):
    return json.dumps([plain(cell) for cell in cells], sort_keys=True, separators=(',', ':'))


def label(row, pk):
    return key_token([row[name] for name in pk])


def numeric(cell):
    """The number a cell carries, or None when it carries no number.

    In typed mode only int/real cells carry numbers.  In naive mode every cell is
    text, so a numeric spelling is parsed and a non-numeric one yields None.
    """
    if cell['t'] == 'real':
        return cell['_f']
    if cell['t'] == 'int':
        return float(int(cell['v']))
    if cell['t'] == 'text':
        try:
            return float(cell['v'])
        except ValueError:
            return None
    return None


def same_text(left, right, collation, fold):
    if collation == 'BINARY':
        return left == right
    if collation != 'NOCASE':
        raise MaterialError(f'roster names unsupported collation {collation!r}')
    if fold == 'nocase':
        # SQLite NOCASE folds A-Z only.
        return left.translate(ASCII_FOLD) == right.translate(ASCII_FOLD)
    # Python casefold folds more than SQLite does; kept to show the difference.
    return left.casefold() == right.casefold()


def same_value(left, right, collation, fold):
    if left['t'] != right['t']:
        return False
    if left['t'] == 'text':
        return same_text(left['v'], right['v'], collation, fold)
    if left['t'] == 'int':
        return int(left['v']) == int(right['v'])
    if left['t'] == 'real':
        return left['_f'] == right['_f']
    return left['_b'] == right['_b']


def binary_same(left, right):
    if left['t'] != right['t']:
        return False
    if left['t'] == 'text':
        return left['v'] == right['v']
    if left['t'] == 'int':
        return int(left['v']) == int(right['v'])
    if left['t'] == 'real':
        return left['_f'] == right['_f']
    return left['_b'] == right['_b']


def read_typed(material, dataset, table, columns):
    path = material / 'typed' / dataset / f'{table}.csv'
    with path.open(newline='', encoding='utf-8') as handle:
        reader = csv.reader(handle)
        header = next(reader, None)
        if header != columns:
            raise MaterialError(f'{path}: header does not match the roster')
        rows = []
        for number, raw in enumerate(reader, start=2):
            if len(raw) != len(header):
                raise MaterialError(f'{path}:{number}: expected {len(header)} fields, found {len(raw)}')
            rows.append({name: typed_cell(cell, f'{path.name}:{number}:{name}')
                         for name, cell in zip(header, raw)})
    return rows


def read_naive(material, dataset, table, columns, null_token):
    path = material / 'naive' / dataset / f'{table}.csv'
    with path.open(newline='', encoding='utf-8') as handle:
        reader = csv.reader(handle)
        header = next(reader, None)
        if header != columns:
            raise MaterialError(f'{path}: header does not match the roster')
        rows = []
        for number, raw in enumerate(reader, start=2):
            if len(raw) != len(header):
                raise MaterialError(f'{path}:{number}: wrong field count')
            cells = {}
            for name, text in zip(header, raw):
                if text == null_token:
                    cells[name] = {'t': 'null', '_ambiguous': True}
                else:
                    cells[name] = {'t': 'text', 'v': text}
                    try:
                        cells[name]['_f'] = float(text)
                    except ValueError:
                        pass
            rows.append(cells)
    return rows


def classify(dataset_tables, roster, fold, ambiguous):
    focus = roster['focus']
    child, parent = focus['child'], focus['parent']
    child_cols = focus['child_cols']
    parent_cols = focus['parent_cols']
    collations = focus['collations']
    pk = roster['tables'][child]['pk']
    if parent not in dataset_tables:
        raise MaterialError(f'dataset has no parent table {parent!r}; the edge cannot be checked')
    parents = dataset_tables[parent]
    null_keys, missing, only_collation = [], [], []
    for row in dataset_tables[child]:
        cells = [row[name] for name in child_cols]
        if any(cell['t'] == 'null' for cell in cells):
            if any(cell.get('_ambiguous') for cell in cells):
                ambiguous.append({'row': label(row, pk),
                                  'why': 'an empty cell; the export cannot say whether it was NULL '
                                         'or empty text, so this entry may be either'})
            null_keys.append(label(row, pk))
            continue
        match = None
        for candidate in parents:
            if all(same_value(cell, candidate[name], collations[name], fold)
                   for cell, name in zip(cells, parent_cols)):
                match = candidate
                break
        if match is None:
            missing.append(label(row, pk))
        elif not all(binary_same(cell, match[name]) for cell, name in zip(cells, parent_cols)):
            only_collation.append(label(row, pk))
    return {'null_component': sorted(null_keys), 'missing_parent': sorted(missing),
            'collation_only': sorted(only_collation)}


def recompute(dataset_tables, roster):
    results = {}
    for table, rule in roster['tables'].items():
        if table not in dataset_tables:
            continue
        formula = rule.get('generated') or rule.get('claimed')
        if formula is None:
            results[table] = {'declaration': None, 'checked': 0, 'mismatch': []}
            continue
        declaration = 'declared' if rule.get('generated') else 'claimed'
        pk = rule['pk']
        checked, mismatch = 0, []
        for row in dataset_tables[table]:
            left = numeric(row[formula['input']])
            right = numeric(row[formula['column']])
            if left is None or right is None:
                mismatch.append(label(row, pk))
                continue
            checked += 1
            if left * formula['factor'] != right:
                mismatch.append(label(row, pk))
        results[table] = {'declaration': declaration, 'column': formula['column'],
                          'checked': checked, 'mismatch': sorted(mismatch)}
        if declaration == 'claimed':
            results[table]['note'] = ('the roster asserts this formula; cells cannot show that the '
                                      'column is stored rather than generated')
        elif rule.get('generated'):
            results[table]['note'] = ('the roster declares GENERATED, and exact REAL bits let the '
                                      'formula be rechecked; the declaration itself is not in the cells')
    return results


def integrity(dataset_tables, roster):
    problems = []
    for table, rows in dataset_tables.items():
        rule = roster['tables'][table]
        seen = set()
        for row in rows:
            for name in rule['pk']:
                if row[name]['t'] == 'null':
                    problems.append(f'{table}: primary key column {name} is NULL')
            token = key_token([row[name] for name in rule['pk']])
            if token in seen:
                problems.append(f'{table}: duplicate row identity {token}')
            seen.add(token)
    return problems


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('material', type=Path, help='material-a-csv directory')
    parser.add_argument('--dataset', default='certified')
    parser.add_argument('--mode', choices=('typed', 'naive'), default='typed')
    parser.add_argument('--null-token', default='', help='plain-export NULL convention')
    parser.add_argument('--fold', choices=('nocase', 'casefold'), default='nocase',
                        help='case folding for the CSV-side NOCASE comparison')
    parser.add_argument('--json-out', type=Path)
    args = parser.parse_args(argv)
    ambiguous = []
    limitations = []
    try:
        with (args.material / 'roster.json').open(encoding='utf-8') as handle:
            roster = json.load(handle)
        if roster.get('format') != 'keelsplice-handoff-roster/1':
            raise MaterialError('roster.json has an unexpected format tag')
        if args.dataset not in roster['datasets']:
            raise MaterialError(f'dataset {args.dataset!r} is not described by the roster')
        tables = roster['datasets'][args.dataset]['tables']
        if args.mode == 'typed':
            reader = read_typed
        else:
            reader = lambda material, dataset, table, columns: read_naive(  # noqa: E731
                material, dataset, table, columns, args.null_token)
            limitations += [
                'A plain export cannot tell NULL from empty text or from a literal spelling of the '
                'NULL token.  This run reads every empty field as NULL; the opposite convention '
                'misreads real NULLs instead, so neither choice is right.',
                'A plain export drops storage classes, so 11, "11" and 11.0 arrive as the same text.',
                'A plain export drops the GENERATED declaration and the parent-key collations; both '
                'are taken from roster.json instead of from the rows.',
                'A plain export prints REAL with six significant digits, so a correct product that '
                'needs more bits is reported as a mismatch.',
            ]
        dataset_tables = {table: reader(args.material, args.dataset, table,
                                        roster['tables'][table]['columns'])
                          for table in tables}
        if args.fold == 'casefold':
            limitations.append('Python casefold folds more than SQLite NOCASE; this run shows only '
                               'how a receiver guessing at NOCASE would differ.')
        classified = classify(dataset_tables, roster, args.fold, ambiguous)
        problems = integrity(dataset_tables, roster)
        report = {'receiver': 'csv-stdlib', 'material': str(args.material),
                  'dataset': args.dataset, 'mode': args.mode,
                  'null_token': args.null_token if args.mode == 'naive' else None,
                  'fold': args.fold, **classified,
                  'ambiguous_null_cells': ambiguous,
                  'recompute': recompute(dataset_tables, roster),
                  'integrity': problems, 'limitations': limitations}
        text = json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False)
        print(text)
        if args.json_out:
            args.json_out.write_text(text + '\n', encoding='utf-8')
        return 2 if problems else 0
    except (OSError, MaterialError) as exc:
        print(json.dumps({'receiver': 'csv-stdlib', 'error': str(exc)}, indent=2, ensure_ascii=False))
        return 1


if __name__ == '__main__':
    sys.exit(main())

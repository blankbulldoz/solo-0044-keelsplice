"""Secondary-extraction self-check for certified SQLite handoffs.

The workstation issues a digest-only manifest for one specified root of a
certified capture. The offline recipient probes the received database and
produces digest-only material, so no row values leave the recipient. Both
sides run the same judgment to tell apart the original file, legitimate
recipient appends, and files that lost the specified root.
"""

import argparse
import json
import re
import sqlite3
import sys
from collections import Counter
from contextlib import closing
from pathlib import Path

from . import catalog
from .capture import find
from .codec import Fault, decode, digest, dumps, fingerprint, quote, row_values
from .recipe import identity

FORMAT = 'keelsplice-recheck/1'
HEX = re.compile(r'[0-9a-f]{64}\Z')
MAX_JSON_BYTES = 16 * 1024 * 1024


def sha(value):
    return isinstance(value, str) and HEX.fullmatch(value) is not None


def shape(value, keys, code):
    if not isinstance(value, dict) or set(value) != set(keys):
        raise Fault(code, 'Unexpected self-check JSON shape', 422)


def check_file(value, code):
    shape(value, ('sha256', 'bytes'), code)
    if not sha(value['sha256']) or type(value['bytes']) is not int or value['bytes'] < 0:
        raise Fault(code, 'File facts need a SHA-256 digest and a byte count', 422)


def check_tables(value, code):
    if not isinstance(value, dict):
        raise Fault(code, 'Table digests must key on table names', 422)
    tables = {}
    for name, entry in value.items():
        if not isinstance(name, str):
            raise Fault(code, 'Table digests must key on table names', 422)
        shape(entry, ('rows', 'digests'), code)
        digests = entry['digests']
        if (type(entry['rows']) is not int or not isinstance(digests, list)
                or len(digests) != entry['rows'] or any(not sha(item) for item in digests)):
            raise Fault(code, 'Each table needs its row count and row digests', 422)
        tables[name] = sorted(digests)
    return tables


def check_manifest(value):
    code = 'recheck_manifest'
    shape(value, ('format', 'capture_digest', 'schema_digest', 'root', 'file', 'tables'), code)
    if value['format'] != FORMAT or not sha(value['capture_digest']) or not sha(value['schema_digest']):
        raise Fault(code, 'Manifest needs the supported format and SHA-256 digests', 422)
    check_file(value['file'], code)
    root = value['root']
    shape(root, ('table', 'key'), code)
    if not isinstance(root['table'], str) or not isinstance(root['key'], list) or not root['key']:
        raise Fault(code, 'Manifest root needs a table and a key tuple', 422)
    return {'capture_digest': value['capture_digest'], 'schema_digest': value['schema_digest'],
            'root': {'table': root['table'], 'key': root['key']}, 'file': value['file'],
            'tables': check_tables(value['tables'], code)}


def check_material(value):
    code = 'recheck_material'
    shape(value, ('format', 'capture_digest', 'file', 'integrity', 'references',
                  'schema_digest', 'root', 'tables'), code)
    if value['format'] != FORMAT or not sha(value['capture_digest']):
        raise Fault(code, 'Material needs the supported format and the capture digest', 422)
    check_file(value['file'], code)
    if type(value['integrity']) is not bool or type(value['references']) is not bool:
        raise Fault(code, 'Material self-check flags must be booleans', 422)
    if value['schema_digest'] is not None and not sha(value['schema_digest']):
        raise Fault(code, 'Material schema digest must be SHA-256 hex or null', 422)
    root = value['root']
    shape(root, ('table', 'key', 'found', 'actual'), code)
    if (not isinstance(root['table'], str) or not isinstance(root['key'], list)
            or type(root['found']) is not bool
            or (root['actual'] is not None and not isinstance(root['actual'], list))):
        raise Fault(code, 'Material root needs a table, key, found flag and actual key', 422)
    return {'capture_digest': value['capture_digest'], 'file': value['file'],
            'integrity': value['integrity'], 'references': value['references'],
            'schema_digest': value['schema_digest'],
            'root': {'table': root['table'], 'key': root['key'],
                     'found': root['found'], 'actual': root['actual']},
            'tables': check_tables(value['tables'], code)}


def manifest(sealed, certificate, spec):
    root = identity(sealed['schema'], spec)
    if [root['table'], root['key']] not in sealed['result']['roots']:
        raise Fault('unknown_root', 'Specify an actual root recorded in this capture', 404)
    tables = {}
    for name, rows in sealed['result']['rows'].items():
        digests = sorted(fingerprint(stored) for stored in rows.values())
        tables[name] = {'rows': len(digests), 'digests': digests}
    return {'format': FORMAT, 'capture_digest': certificate['capture_digest'],
            'schema_digest': sealed['schema_digest'], 'root': root,
            'file': {'sha256': certificate['sha256'], 'bytes': certificate['bytes']},
            'tables': tables}


def probe(path, manifest_value):
    spec = check_manifest(manifest_value)
    try:
        vals = [decode(component) for component in spec['root']['key']]
    except Fault as exc:
        raise Fault('recheck_manifest', 'Manifest root key values must use the canonical typed encoding', 422) from exc
    data = Path(path).read_bytes()
    material = {'format': FORMAT, 'capture_digest': spec['capture_digest'],
                'file': {'sha256': digest(data), 'bytes': len(data)},
                'integrity': False, 'references': False, 'schema_digest': None,
                'root': {'table': spec['root']['table'], 'key': spec['root']['key'],
                         'found': False, 'actual': None},
                'tables': {}}
    try:
        with closing(sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True)) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute('PRAGMA query_only=ON')
            conn.execute('PRAGMA trusted_schema=OFF')
            try:
                rows = conn.execute('PRAGMA main.integrity_check').fetchall()
                material['integrity'] = [row[0] for row in rows] == ['ok']
                material['references'] = conn.execute('PRAGMA main.foreign_key_check').fetchone() is None
            except sqlite3.Error:
                return material
            try:
                schema = catalog.inspect(conn)
            except Fault:
                return material
            material['schema_digest'] = fingerprint(schema)
            root = spec['root']
            meta = schema['tables'].get(root['table'])
            if meta is not None and len(meta['pk']) == len(root['key']):
                matches = find(conn, root['table'], meta['pk'], vals)
                if len(matches) == 1:
                    material['root']['found'] = True
                    material['root']['actual'] = row_values(matches[0], meta['pk'])
            for name, meta in schema['tables'].items():
                columns = [col['name'] for col in meta['columns']]
                digests = sorted(fingerprint(row_values(row, columns))
                                 for row in conn.execute(f'SELECT * FROM {quote(name)}'))
                material['tables'][name] = {'rows': len(digests), 'digests': digests}
    except sqlite3.Error:
        pass
    return material


def judge(manifest_value, material_value):
    spec = check_manifest(manifest_value)
    proof = check_material(material_value)
    root = proof['root']
    if (proof['capture_digest'] != spec['capture_digest']
            or root['table'] != spec['root']['table'] or root['key'] != spec['root']['key']):
        raise Fault('recheck_mismatch', 'Material was produced for a different capture or root', 409)
    tables = {}
    missing_rows = appended_rows = 0
    same_schema = proof['schema_digest'] == spec['schema_digest']
    if same_schema:
        for name, want in spec['tables'].items():
            got = proof['tables'].get(name, [])
            missing = Counter(want) - Counter(got)
            extra = Counter(got) - Counter(want)
            missing_rows += sum(missing.values())
            appended_rows += sum(extra.values())
            tables[name] = {'missing': sum(missing.values()), 'extra': sum(extra.values())}
    if not proof['integrity'] or not proof['references']:
        verdict, reasons = 'rejected', ['integrity']
    elif not same_schema:
        verdict, reasons = 'rejected', ['schema']
    elif not root['found']:
        verdict, reasons = 'missing_root', ['missing_root']
    elif root['actual'] != spec['root']['key']:
        verdict, reasons = 'rejected', ['root_key']
    elif missing_rows:
        verdict, reasons = 'rejected', ['content']
    elif appended_rows:
        verdict, reasons = 'appended', ['appended']
    else:
        verdict, reasons = 'original', ['original']
    return {'format': FORMAT, 'verdict': verdict, 'reasons': reasons,
            'bytes_match': proof['file'] == spec['file'],
            'root': {'table': spec['root']['table'], 'key': spec['root']['key'], 'found': root['found']},
            'missing_rows': missing_rows, 'appended_rows': appended_rows, 'tables': tables}


def load_json(path, code):
    try:
        data = Path(path).read_bytes()
    except OSError as exc:
        raise Fault(code, f'Cannot read {path}') from exc
    if len(data) > MAX_JSON_BYTES:
        raise Fault(code, 'Self-check JSON exceeds the 16 MiB budget', 422)
    try:
        return json.loads(data.decode('utf-8'))
    except (UnicodeError, ValueError) as exc:
        raise Fault(code, 'Expected a JSON document', 422) from exc


def main(argv=None):
    parser = argparse.ArgumentParser(prog='keelsplice.recheck', description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    probe_command = commands.add_parser('probe', help='从收到的子库生成本机自查材料')
    probe_command.add_argument('database', help='收到的 SQLite 子库')
    probe_command.add_argument('manifest', help='工位出具的自查清单 JSON')
    probe_command.add_argument('-o', '--output', help='材料输出路径，默认为标准输出')
    judge_command = commands.add_parser('judge', help='按清单与材料复跑同一项判定')
    judge_command.add_argument('manifest', help='工位出具的自查清单 JSON')
    judge_command.add_argument('material', help='本机生成的自查材料 JSON')
    args = parser.parse_args(argv)
    try:
        if args.command == 'probe':
            material = probe(args.database, load_json(args.manifest, 'recheck_manifest'))
            text = dumps(material)
            if args.output:
                Path(args.output).write_text(text + '\n', encoding='ascii')
            else:
                print(text)
        else:
            print(dumps(judge(load_json(args.manifest, 'recheck_manifest'),
                              load_json(args.material, 'recheck_material'))))
    except Fault as exc:
        print(dumps({'error': exc.code, 'message': str(exc)}), file=sys.stderr)
        return 2
    except OSError as exc:
        print(dumps({'error': 'recheck_io', 'message': str(exc)}), file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    sys.exit(main())

"""Inventory a Python interpreter and separate runtime facts from SQLite facts.

Two very different things move when a deployment goes from Python 3.11 to
Python 3.14:

1. The CPython runtime and its :mod:`sqlite3` Python binding layer.
2. The SQLite shared library that the binding is linked against.

A deployment may keep the same Python series while swapping libsqlite3, or
move Python while keeping an unchanged library.  This probe therefore emits
two clearly separated sections and stamps every capability with the layer it
belongs to, so an upgrade review can tell a Python 3.14 binding change from a
SQLite engine capability change.  ``compare`` consumes two reports and renders
a go/no-go verdict against the capabilities Keelsplice actually depends on.
"""

import argparse
import json
import platform
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 1

# SQLite capabilities Keelsplice relies on, with the first SQLite release that
# shipped them.  ``minimum`` drives the go/no-go gate; entries above the old
# deployment's library version demonstrate engine-level differences that are
# independent of the CPython upgrade.
SQLITE_CAPABILITIES = (
    ('table_list_pragma', 'PRAGMA main.table_list (catalog listing)', (3, 37, 0),
     'SELECT name, type, wr, strict FROM pragma_table_list LIMIT 1'),
    ('table_xinfo_pragma', 'PRAGMA table_xinfo (hidden/generated columns)', (3, 26, 0),
     'SELECT name, hidden, pk FROM pragma_table_xinfo LIMIT 1'),
    ('generated_columns', 'STORED generated columns', (3, 31, 0),
     'CREATE TABLE t_gen (a INTEGER, b INTEGER GENERATED ALWAYS AS (a * 2) STORED)'),
    ('strict_tables', 'STRICT table mode', (3, 37, 0),
     "CREATE TABLE t_strict (a INTEGER NOT NULL) STRICT"),
    ('without_rowid', 'WITHOUT ROWID tables', (3, 8, 2),
     'CREATE TABLE t_wr (a INTEGER PRIMARY KEY) WITHOUT ROWID'),
    ('partial_index', 'Partial indexes', (3, 6, 19),
     ['CREATE TABLE t_pi (a INTEGER)', 'CREATE INDEX i_pi ON t_pi(a) WHERE a IS NOT NULL']),
    ('defer_foreign_keys', 'PRAGMA defer_foreign_keys', (3, 6, 19),
     'PRAGMA defer_foreign_keys = ON'),
    ('right_join', 'RIGHT JOIN syntax', (3, 39, 0),
     ['CREATE TABLE t_rj (a INTEGER)',
      'SELECT 1 FROM t_rj x RIGHT JOIN t_rj y USING (a)']),
    ('unhex_function', 'unhex() SQL function', (3, 41, 0),
     "SELECT unhex('4B45') = x'4B45'"),
    ('json_builtin', 'Built-in JSON SQL functions', (3, 38, 0),
     "SELECT json_valid('{}') = 1"),
    ('unixepoch_modifier', 'unixepoch datetime modifier', (3, 38, 0),
     "SELECT strftime('%Y', 0, 'unixepoch') = '1970'"),
)

# Python-binding-layer capabilities.  These move with CPython, not with the
# SQLite library, even when the underlying engine already had the feature.
RUNTIME_CAPABILITIES = (
    ('legacy_module_version', 'Legacy sqlite3.version PEP 246 attribute', (3, 11)),
    ('connection_autocommit', 'Writable Connection.autocommit attribute', (3, 12)),
    ('connection_getconfig', 'Connection.getconfig/setconfig', (3, 12)),
    ('dbconfig_constants', 'sqlite3.SQLITE_DBCONFIG_* constants', (3, 12)),
    ('connection_serialize', 'Connection.serialize/deserialize', (3, 11)),
    ('keyword_only_connect', 'sqlite3.connect keyword-only parameters', (3, 15)),
)

REQUIRED_SQLITE = (
    'table_list_pragma', 'table_xinfo_pragma', 'generated_columns',
    'strict_tables', 'without_rowid', 'partial_index', 'defer_foreign_keys',
)
REQUIRED_PYTHON = (3, 11)
SQLITE_FLOOR = (3, 37, 0)


def _version_tuple(values):
    return [int(value) for value in values]


def _probe_sqlite_capability(statements):
    statements = [statements] if isinstance(statements, str) else statements
    conn = sqlite3.connect(':memory:')
    try:
        for statement in statements:
            conn.execute(statement)
        return True, 'ok'
    except sqlite3.Error as exc:
        return False, f'{type(exc).__name__}: {exc}'
    finally:
        conn.close()


def _runtime_capability(name):
    version = sys.version_info[:3]
    if name == 'legacy_module_version':
        return hasattr(sqlite3, 'version'), 'removed in Python 3.12'
    if name == 'connection_autocommit':
        conn = sqlite3.connect(':memory:')
        try:
            supported = hasattr(conn, 'autocommit')
            return supported, 'autocommit attribute present' if supported else 'absent'
        finally:
            conn.close()
    if name == 'connection_getconfig':
        conn = sqlite3.connect(':memory:')
        try:
            return hasattr(conn, 'getconfig') and hasattr(conn, 'setconfig'), ''
        finally:
            conn.close()
    if name == 'dbconfig_constants':
        return any(attr.startswith('SQLITE_DBCONFIG_') for attr in dir(sqlite3)), ''
    if name == 'connection_serialize':
        conn = sqlite3.connect(':memory:')
        try:
            return hasattr(conn, 'serialize') and hasattr(conn, 'deserialize'), ''
        finally:
            conn.close()
    if name == 'keyword_only_connect':
        # The parameters become keyword-only in 3.15; 3.12-3.14 warn at runtime.
        return version >= (3, 15), 'keyword-only from Python 3.15'
    raise KeyError(name)


def _connect_surface():
    """Facts about the Python binding layer that an upgrade review can diff."""
    conn = sqlite3.connect(':memory:')
    try:
        surface = {
            'threadsafety': sqlite3.threadsafety,
            'paramstyle': sqlite3.paramstyle,
            'legacy_version_attribute': getattr(sqlite3, 'version', None),
            'sqlite_version_attribute': sqlite3.sqlite_version,
            'connection_autocommit_attribute': hasattr(conn, 'autocommit'),
            'connection_serialize': hasattr(conn, 'serialize'),
            'connection_getconfig': hasattr(conn, 'getconfig'),
            'dbconfig_constants': sorted(
                attr for attr in dir(sqlite3) if attr.startswith('SQLITE_DBCONFIG_')),
            'connect_doc_excerpt': (sqlite3.connect.__doc__ or '').strip().splitlines()[-1],
            'module_file': getattr(sys.modules['_sqlite3'], '__file__', 'built-in'),
        }
    finally:
        conn.close()
    return surface


def collect():
    """Return a plain-data inventory of the currently running interpreter."""
    sqlite_capabilities = []
    for identifier, label, since, statement in SQLITE_CAPABILITIES:
        supported, detail = _probe_sqlite_capability(statement)
        sqlite_capabilities.append({
            'id': identifier, 'label': label, 'layer': 'sqlite_library',
            'since_sqlite': list(since), 'supported': supported, 'detail': detail,
        })
    runtime_capabilities = []
    for identifier, label, since in RUNTIME_CAPABILITIES:
        supported, detail = _runtime_capability(identifier)
        runtime_capabilities.append({
            'id': identifier, 'label': label, 'layer': 'python_runtime',
            'since_python': list(since), 'supported': supported, 'detail': detail,
        })
    try:
        compile_options = [row[0] for row in sqlite3.connect(':memory:').execute('PRAGMA compile_options')]
    except sqlite3.Error:
        compile_options = []
    return {
        'schema_version': SCHEMA_VERSION,
        'collected_at_utc': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
        'runtime': {
            'implementation': platform.python_implementation(),
            'python_version': sys.version.replace('\n', ' '),
            'python_version_info': list(sys.version_info[:3]),
            'executable': sys.executable,
            'platform': platform.platform(),
            'machine': platform.machine(),
            'binding_surface': _connect_surface(),
            'capabilities': runtime_capabilities,
        },
        'sqlite': {
            'sqlite_version': sqlite3.sqlite_version,
            'sqlite_version_info': list(sqlite3.sqlite_version_info),
            'version_id': getattr(sqlite3, 'sqlite_version', None),
            'compile_options': compile_options,
            'capabilities': sqlite_capabilities,
        },
        'keelsplice_requirements': {
            'python_floor': list(REQUIRED_PYTHON),
            'python_floor_met': sys.version_info[:2] >= REQUIRED_PYTHON,
            'sqlite_floor': list(SQLITE_FLOOR),
            'sqlite_floor_met': sqlite3.sqlite_version_info >= SQLITE_FLOOR,
            'required_sqlite_capabilities': list(REQUIRED_SQLITE),
        },
    }


def _capability_index(report, section):
    return {item['id']: item for item in report[section]['capabilities']}


def compare(old_report, new_report):
    """Diff two collect() reports, tagging each change with its source layer."""
    old_runtime, new_runtime = old_report['runtime'], new_report['runtime']
    runtime_changes = []
    if old_runtime['python_version_info'] != new_runtime['python_version_info']:
        runtime_changes.append({
            'layer': 'python_runtime', 'fact': 'python_version_info',
            'old': old_runtime['python_version_info'], 'new': new_runtime['python_version_info'],
            'significance': 'Interpreter major/minor upgrade',
        })
    for key in ('threadsafety', 'paramstyle', 'legacy_version_attribute',
                'connection_autocommit_attribute', 'connection_serialize',
                'connection_getconfig', 'connect_doc_excerpt'):
        before, after = (old_runtime['binding_surface'][key], new_runtime['binding_surface'][key])
        if before != after:
            runtime_changes.append({
                'layer': 'python_runtime', 'fact': f'binding_surface.{key}',
                'old': before, 'new': after,
                'significance': 'sqlite3 Python binding change, not an engine change',
            })
    if old_runtime['binding_surface']['dbconfig_constants'] != new_runtime['binding_surface']['dbconfig_constants']:
        runtime_changes.append({
            'layer': 'python_runtime', 'fact': 'binding_surface.dbconfig_constants',
            'old': old_runtime['binding_surface']['dbconfig_constants'],
            'new': new_runtime['binding_surface']['dbconfig_constants'],
            'significance': 'New Python-exposed constants; engine support is reported separately',
        })

    library_changes = []
    if old_report['sqlite']['sqlite_version_info'] != new_report['sqlite']['sqlite_version_info']:
        library_changes.append({
            'layer': 'sqlite_library', 'fact': 'sqlite_version_info',
            'old': old_report['sqlite']['sqlite_version_info'],
            'new': new_report['sqlite']['sqlite_version_info'],
            'significance': 'Linked libsqlite3 upgrade, independent of CPython',
        })

    capability_changes = []
    for section, layer in (('runtime', 'python_runtime'), ('sqlite', 'sqlite_library')):
        old_index = _capability_index(old_report, section)
        new_index = _capability_index(new_report, section)
        for identifier, after in sorted(new_index.items()):
            before = old_index.get(identifier)
            if before is None or before['supported'] != after['supported']:
                capability_changes.append({
                    'layer': layer, 'id': identifier, 'label': after['label'],
                    'old_supported': None if before is None else before['supported'],
                    'new_supported': after['supported'],
                    'detail': after['detail'],
                })

    requirements = new_report['keelsplice_requirements']
    new_sqlite_index = _capability_index(new_report, 'sqlite')
    missing_required = [identifier for identifier in REQUIRED_SQLITE
                        if not new_sqlite_index[identifier]['supported']]
    verdict_reasons = []
    if not requirements['python_floor_met']:
        verdict_reasons.append(
            f"Python {'.'.join(map(str, REQUIRED_PYTHON))} floor not met by "
            f"{new_runtime['python_version']}")
    if not requirements['sqlite_floor_met']:
        verdict_reasons.append(
            f"SQLite {'.'.join(map(str, SQLITE_FLOOR))} floor not met by "
            f"{new_report['sqlite']['sqlite_version']}")
    verdict_reasons.extend(f'Required SQLite capability missing: {identifier}'
                           for identifier in missing_required)
    return {
        'schema_version': SCHEMA_VERSION,
        'compared_at_utc': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
        'old_executable': old_runtime['executable'],
        'new_executable': new_runtime['executable'],
        'runtime_changes': runtime_changes,
        'sqlite_library_changes': library_changes,
        'capability_changes': capability_changes,
        'requirements_on_new_runtime': requirements,
        'missing_required_capabilities': missing_required,
        'verdict': 'no-go' if verdict_reasons else 'go',
        'verdict_reasons': verdict_reasons,
    }


def _write(payload, destination):
    path = Path(destination)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + '\n',
                    encoding='utf-8')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    probe = commands.add_parser('probe', help='inventory this interpreter')
    probe.add_argument('--out', required=True)
    diff = commands.add_parser('compare', help='diff two inventory reports')
    diff.add_argument('--old', dest='old_path', required=True)
    diff.add_argument('--new', dest='new_path', required=True)
    diff.add_argument('--out', required=True)
    args = parser.parse_args(argv)
    if args.command == 'probe':
        report = collect()
        _write(report, args.out)
        print(f"python {'.'.join(map(str, report['runtime']['python_version_info']))} "
              f"({report['runtime']['executable']}) / sqlite {report['sqlite']['sqlite_version']} "
              f"-> {args.out}")
        return 0
    old_report = json.loads(Path(args.old_path).read_text(encoding='utf-8'))
    new_report = json.loads(Path(args.new_path).read_text(encoding='utf-8'))
    verdict = compare(old_report, new_report)
    _write(verdict, args.out)
    print(f"verdict={verdict['verdict']} "
          f"runtime_changes={len(verdict['runtime_changes'])} "
          f"library_changes={len(verdict['sqlite_library_changes'])} "
          f"capability_changes={len(verdict['capability_changes'])} -> {args.out}")
    for reason in verdict['verdict_reasons']:
        print('  reason:', reason)
    return 0 if verdict['verdict'] == 'go' else 2


if __name__ == '__main__':
    raise SystemExit(main())

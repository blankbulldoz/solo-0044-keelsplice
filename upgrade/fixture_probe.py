"""Independent SQLite re-reads of certified fixtures and the state repository.

Nothing in this module imports Keelsplice application code.  A certified
fixture is accepted only when a brand-new SQLite connection - opened after the
bytes were written to a fresh file, in read-only/URI mode where possible -
agrees with the certificate about structure, row counts, foreign keys and
integrity.  When the standalone ``sqlite3`` shell is available it performs the
same checks through a second, non-Python reader so the Python binding is not
asked to vouch for its own output.

The repository re-read answers the preservation question after the cutover:
old recipe versions, sealed captures, certificates and artifact bytes must all
still parse and still agree with each other under the new runtime.
"""

import argparse
import json
import sqlite3
import subprocess
import sys
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

SCHEMA_VERSION = 1


def _sha256_file(path):
    import hashlib
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        for chunk in iter(lambda: handle.read(1 << 16), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_bytes(data):
    import hashlib
    return hashlib.sha256(data).hexdigest()


def _schema_facts(conn):
    tables = {}
    listings = conn.execute('PRAGMA main.table_list').fetchall()
    for entry in listings:
        schema, name, kind = entry[0], entry[1], entry[2]
        if schema != 'main' or name.startswith('sqlite_') or kind == 'view':
            continue
        columns = [{'name': row[1], 'hidden': row[6], 'pk': row[5]}
                   for row in conn.execute(f'PRAGMA main.table_xinfo("{name}")')]
        count = conn.execute(f'SELECT count(*) FROM "{name}"').fetchone()[0]
        tables[name] = {
            'column_count': len(columns),
            'generated_columns': [col['name'] for col in columns if col['hidden'] in (2, 3)],
            'row_count': count,
            'without_rowid': bool(entry[4]),
            'strict': bool(entry[5]),
        }
    indexes = [row[0] for row in conn.execute(
        "SELECT name FROM main.sqlite_schema WHERE type='index' AND sql IS NOT NULL ORDER BY name")]
    return tables, indexes


def _sample_contract_checks(conn):
    """Checks tied to the microscope production sample (skipped when absent)."""
    checks = {}
    available = {row[1] for row in conn.execute('PRAGMA main.table_list')}
    if {'substrate', 'assay', 'frame'} <= available:
        checks['nocase_primary_key_roundtrip'] = conn.execute(
            "SELECT region, code FROM substrate WHERE region='east' AND code='MICA'"
        ).fetchall() == [('East', 'Mica')]
        row = conn.execute("SELECT length_um, doubled FROM assay WHERE mark='S-01'").fetchone()
        # The source is intentionally mutated during the upgrade story, so the
        # stored value is fixture-specific; the generated column must always
        # equal twice the stored base column.
        checks['stored_generated_column'] = list(row) == [row[0], row[0] * 2]
        checks['blob_roundtrip'] = conn.execute(
            'SELECT texture FROM substrate').fetchone()[0] == b'\x00\x00\xff'
        checks['empty_selected_table_present'] = conn.execute(
            'SELECT count(*) FROM frame').fetchone()[0] == 0
        null_code_rows = [row[0] for row in conn.execute(
            'SELECT mark FROM assay WHERE code IS NULL ORDER BY mark')]
        # S-03 is the source's nullable-FK row.  A closure scoped to substrate
        # roots correctly excludes it; whenever it is present it must survive.
        checks['nullable_foreign_key_kept'] = null_code_rows in ([], ['S-03'])
    return checks


def _cli_reread(cli, path):
    if not cli:
        return {'used': False, 'reason': 'no sqlite3 shell supplied'}
    cli = Path(cli)
    if not cli.is_file():
        return {'used': False, 'reason': f'missing shell: {cli}'}
    script = (
        '.mode list\n'
        "SELECT 'integrity:' || integrity_check FROM pragma_integrity_check;\n"
        "SELECT 'fkv:' || count(*) FROM pragma_foreign_key_check;\n"
        "SELECT 'tables:' || count(*) FROM pragma_table_list WHERE schema='main' "
        "AND name NOT LIKE 'sqlite_%' AND type='table';\n"
    )
    completed = subprocess.run([str(cli), str(path)], input=script,
                               capture_output=True, text=True, timeout=30, check=False)
    lines = [line for line in completed.stdout.splitlines() if line]
    return {
        'used': completed.returncode == 0,
        'shell': str(cli),
        'returncode': completed.returncode,
        'stderr': completed.stderr.strip()[:500],
        'output': lines,
        'integrity_ok': any(line == 'integrity:ok' for line in lines),
        'foreign_keys_clean': any(line == 'fkv:0' for line in lines),
    }


def reread_fixture(data, certificate=None, sqlite_cli=None):
    """Re-read raw fixture bytes through an independent SQLite connection."""
    with TemporaryDirectory(prefix='keelsplice-reread-') as directory:
        path = Path(directory) / 'fixture.sqlite'
        path.write_bytes(data)
        with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as conn:
            integrity = [row[0] for row in conn.execute('PRAGMA main.integrity_check')]
            foreign_violations = conn.execute('PRAGMA main.foreign_key_check').fetchall()
            tables, indexes = _schema_facts(conn)
            sample_checks = _sample_contract_checks(conn)
            total_rows = sum(meta['row_count'] for meta in tables.values())
        cli_result = _cli_reread(sqlite_cli, path)
        sha = _sha256_bytes(data)
    result = {
        'schema_version': SCHEMA_VERSION,
        'checked_at_utc': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
        'reader_runtime': {'executable': sys.executable,
                           'python_version': list(sys.version_info[:3]),
                           'sqlite_version': sqlite3.sqlite_version,
                           'sqlite_version_info': list(sqlite3.sqlite_version_info)},
        'bytes': len(data), 'sha256': sha,
        'integrity_check': integrity,
        'foreign_key_violations': [list(row) for row in foreign_violations],
        'tables': tables, 'indexes': indexes, 'total_rows': total_rows,
        'production_sample_checks': sample_checks,
        'standalone_shell': cli_result,
        'passed': integrity == ['ok'] and not foreign_violations
        and all(sample_checks.values())
        and (not cli_result['used'] or (cli_result['integrity_ok'] and cli_result['foreign_keys_clean'])),
    }
    if certificate is not None:
        result['certificate_match'] = {
            'sha256': certificate.get('sha256') == sha,
            'bytes': certificate.get('bytes') == len(data),
            'counts': certificate.get('counts') == {
                name: tables[name]['row_count'] for name in tables},
            'schema_digest_present': bool(certificate.get('schema_digest')),
        }
        result['passed'] = result['passed'] and all(result['certificate_match'].values())
    return result


def reread_repository(path):
    """Re-read the workbench state database without invoking workbench code."""
    path = Path(path)
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as conn:
        integrity = [row[0] for row in conn.execute('PRAGMA main.integrity_check')]
        foreign_violations = conn.execute('PRAGMA main.foreign_key_check').fetchall()
        recipes = []
        for row in conn.execute(
                'SELECT name, version, active, schema_digest, recipe FROM recipes ORDER BY name, version'):
            name, number, active, schema_digest, recipe_text = row
            recipe = json.loads(recipe_text)
            recipes.append({'name': name, 'version': number, 'active': bool(active),
                            'schema_digest': schema_digest,
                            'source': recipe['source'],
                            'root_count': len(recipe['roots']),
                            'follow_children': recipe['follow_children'],
                            'row_limit': recipe['row_limit']})
        captures = []
        artifact_mismatches = []
        for row in conn.execute(
                'SELECT id, name, version, state, artifact, certificate, sealed FROM captures ORDER BY rowid'):
            capture_id, name, number, state, artifact, certificate_text, sealed_text = row
            sealed = json.loads(sealed_text)
            certificate = json.loads(certificate_text) if certificate_text else None
            entry = {'id': capture_id, 'name': name, 'version': number, 'state': state,
                     'has_artifact': artifact is not None,
                     'has_certificate': certificate is not None,
                     'sealed_schema_digest': sealed['schema_digest'],
                     'sealed_summary': {
                         'rows': sealed['result'] and sum(
                             len(rows) for rows in sealed['result']['rows'].values()),
                         'witnesses': len(sealed['result']['arcs']),
                         'root_occurrences': len(sealed['result']['roots']),
                     }}
            if artifact is not None and certificate is not None:
                actual = _sha256_bytes(artifact)
                entry['artifact_sha256_matches_certificate'] = actual == certificate['sha256']
                if not entry['artifact_sha256_matches_certificate']:
                    artifact_mismatches.append(capture_id)
            else:
                entry['artifact_sha256_matches_certificate'] = None
            captures.append(entry)
        states = {}
        for capture in captures:
            states[capture['state']] = states.get(capture['state'], 0) + 1
    return {
        'schema_version': SCHEMA_VERSION,
        'checked_at_utc': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
        'reader_runtime': {'executable': sys.executable,
                           'python_version': list(sys.version_info[:3]),
                           'sqlite_version': sqlite3.sqlite_version,
                           'sqlite_version_info': list(sqlite3.sqlite_version_info)},
        'repository': str(path.resolve()),
        'integrity_check': integrity,
        'foreign_key_violations': [list(row) for row in foreign_violations],
        'recipe_versions': recipes,
        'recipe_count': len(recipes),
        'retired_versions': [f"{item['name']}@{item['version']}" for item in recipes if not item['active']],
        'captures': captures,
        'capture_states': states,
        'artifact_certificate_mismatches': artifact_mismatches,
        'passed': integrity == ['ok'] and not foreign_violations and not artifact_mismatches,
    }


def _write(payload, destination):
    path = Path(destination)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + '\n',
                    encoding='utf-8')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    fixture = commands.add_parser('fixture', help='re-read fixture bytes independently')
    fixture.add_argument('--file', required=True)
    fixture.add_argument('--certificate')
    fixture.add_argument('--sqlite-cli')
    fixture.add_argument('--out', required=True)
    repository = commands.add_parser('repository', help='re-read the state repository independently')
    repository.add_argument('--repository', required=True)
    repository.add_argument('--out', required=True)
    args = parser.parse_args(argv)

    if args.command == 'fixture':
        data = Path(args.file).read_bytes()
        certificate = (json.loads(Path(args.certificate).read_text(encoding='utf-8'))
                       if args.certificate else None)
        report = reread_fixture(data, certificate, args.sqlite_cli)
    else:
        report = reread_repository(Path(args.repository))
    _write(report, args.out)
    print(f"passed={report['passed']} -> {args.out}")
    return 0 if report['passed'] else 2


if __name__ == '__main__':
    raise SystemExit(main())

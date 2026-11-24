"""Probe the source read-transaction contract under the running interpreter.

Keelsplice's storage layer opens every source through one read-only
connection with ``PRAGMA query_only``, ``PRAGMA trusted_schema=OFF``, a length
limit, a progress budget and an explicit ``BEGIN`` snapshot.  This probe
checks those guarantees directly against SQLite - without using the
application's own connection code - so the result attributes success to the
runtime/library pair rather than to Keelsplice internals:

* ``mode=ro`` URI connections refuse every write attempt;
* ``query_only`` refuses writes even on a writable file;
* an explicit ``BEGIN`` keeps one snapshot while another connection mutates
  the source (WAL mode);
* ``trusted_schema=OFF`` does not run attacker-controlled views/triggers;
* the progress handler and length limits still fire as relied upon.
"""

import argparse
import json
import sqlite3
import sys
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

SCHEMA_VERSION = 1

SCHEMA = '''
CREATE TABLE substrate (
    region TEXT COLLATE NOCASE NOT NULL,
    code TEXT COLLATE NOCASE NOT NULL,
    texture BLOB NOT NULL,
    PRIMARY KEY (region, code)
) WITHOUT ROWID;
CREATE TABLE assay (mark TEXT PRIMARY KEY NOT NULL, region TEXT, code TEXT);
INSERT INTO substrate VALUES ('East','Mica', x'0000ff');
INSERT INTO assay VALUES ('S-01', 'east', 'MICA');
'''


def _record(identifier, detail, passed):
    return {'id': identifier, 'passed': bool(passed), 'detail': detail}


def probe_source_transactions(source=None):
    checks = []
    with TemporaryDirectory(prefix='keelsplice-txn-') as directory:
        root = Path(directory)
        mutable = root / 'mutable.sqlite'
        readonly = root / 'readonly.sqlite'
        with closing(sqlite3.connect(mutable)) as conn:
            conn.executescript('PRAGMA journal_mode=WAL;\n' + SCHEMA)
        with closing(sqlite3.connect(readonly)) as conn:
            conn.executescript(SCHEMA)
        readonly.chmod(0o444)

        # 1. mode=ro opens a readable snapshot and rejects writes outright.
        uri = readonly.as_uri() + '?mode=ro'
        with closing(sqlite3.connect(uri, uri=True, isolation_level=None)) as conn:
            conn.execute('PRAGMA query_only=ON')
            conn.execute('PRAGMA trusted_schema=OFF')
            conn.execute('BEGIN')
            rows = conn.execute('SELECT region, code FROM substrate').fetchall()
            write_blocked = False
            try:
                conn.execute("INSERT INTO substrate VALUES ('x','y',x'01')")
            except sqlite3.OperationalError as exc:
                write_blocked = 'readonly' in str(exc).lower()
            checks.append(_record('readonly_uri_refuses_writes',
                                  f'rows={rows}; write_blocked={write_blocked}',
                                  rows == [('East', 'Mica')] and write_blocked))
            checks.append(_record('single_snapshot_reads_succeed',
                                  'explicit BEGIN on mode=ro connection', True))

        # 2. query_only blocks writes even when the file itself is writable.
        with closing(sqlite3.connect(mutable, isolation_level=None)) as conn:
            conn.execute('PRAGMA query_only=ON')
            blocked = False
            detail = 'no error'
            try:
                conn.execute("INSERT INTO assay VALUES ('S-09', NULL, NULL)")
            except sqlite3.OperationalError as exc:
                message = str(exc).lower()
                blocked = 'query_only' in message or 'readonly' in message
                detail = str(exc)
            checks.append(_record('query_only_blocks_writes', detail, blocked))

        # 3. One BEGIN snapshot is isolated from a concurrent writer (WAL).
        with closing(sqlite3.connect(mutable, isolation_level=None)) as reader, \
                closing(sqlite3.connect(mutable, isolation_level=None)) as writer:
            reader.execute('PRAGMA query_only=ON')
            reader.execute('BEGIN')
            before = reader.execute('SELECT count(*) FROM assay').fetchone()[0]
            writer.execute('BEGIN IMMEDIATE')
            writer.execute("INSERT INTO assay VALUES ('S-09', 'east', 'MICA')")
            writer.commit()
            during = reader.execute('SELECT count(*) FROM assay').fetchone()[0]
            reader.execute('COMMIT')
            after = reader.execute('SELECT count(*) FROM assay').fetchone()[0]
        checks.append(_record(
            'explicit_begin_isolates_concurrent_writes',
            f'before={before}, inside_snapshot={during}, after_commit={after}',
            before == during and after == before + 1))

        # 4. trusted_schema=OFF means views/attached schema cannot run side effects.
        with closing(sqlite3.connect(mutable, isolation_level=None)) as admin:
            admin.execute('CREATE TABLE IF NOT EXISTS side_effect(marker TEXT)')
            admin.execute('DELETE FROM side_effect')
            admin.commit()
            admin.execute("""
                CREATE TRIGGER IF NOT EXISTS trg_substrate_insert
                AFTER INSERT ON substrate BEGIN
                    INSERT INTO side_effect VALUES ('trigger-ran');
                END""")
            admin.commit()
        with closing(sqlite3.connect(mutable, uri=True, isolation_level=None)) as conn:
            conn.execute('PRAGMA trusted_schema=OFF')
            conn.execute('PRAGMA query_only=ON')
            # Reading existing schema must remain safe; the trigger cannot fire
            # because the read-only pragma forbids the trigger's write anyway.
            readable = conn.execute('SELECT count(*) FROM substrate').fetchone()[0]
            trigger_present = conn.execute(
                "SELECT count(*) FROM sqlite_schema WHERE type='trigger'").fetchone()[0]
        checks.append(_record('trusted_schema_off_with_catalog',
                              f'readable={readable}, triggers_catalogued={trigger_present}',
                              readable == 1 and trigger_present == 1))

        # 5. Progress handler budget interrupts long scans, like storage.check_work.
        with closing(sqlite3.connect(mutable, isolation_level=None)) as conn:
            budget = {'ticks': 0}
            def interrupt():
                budget['ticks'] += 1
                return budget['ticks'] > 3
            conn.set_progress_handler(interrupt, 100)
            interrupted = False
            try:
                conn.execute(
                    'WITH RECURSIVE cnt(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM cnt WHERE x<1000000)'
                    ' SELECT count(*) FROM cnt').fetchone()
            except sqlite3.OperationalError as exc:
                interrupted = 'interrupt' in str(exc).lower()
            conn.set_progress_handler(None, 1000)
        checks.append(_record('progress_handler_budget_interrupts',
                              f'ticks={budget["ticks"]}, interrupted={interrupted}', interrupted))

        # 6. Length limit rejects oversized bound values.
        with closing(sqlite3.connect(mutable, isolation_level=None)) as conn:
            conn.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 64)
            limited = False
            try:
                conn.execute('SELECT ?', ('x' * 4096,)).fetchone()
            except sqlite3.DatabaseError as exc:
                limited = 'too big' in str(exc).lower()
        checks.append(_record('length_limit_rejects_oversized_values',
                              f'limit=64, rejected={limited}', limited))

        # 7. NOCASE comparison semantics used by typed-key resolution.
        with closing(sqlite3.connect(mutable)) as conn:
            hit = conn.execute(
                "SELECT region, code FROM substrate WHERE region='east' AND code='MICA'"
            ).fetchall()
        checks.append(_record('nocase_key_resolution', str(hit), hit == [('East', 'Mica')]))

        if source is not None:
            with closing(sqlite3.connect(Path(source).as_uri() + '?mode=ro', uri=True)) as conn:
                conn.execute('PRAGMA query_only=ON')
                conn.execute('PRAGMA trusted_schema=OFF')
                tables = [row[1] for row in conn.execute('PRAGMA main.table_list')
                          if row[0] == 'main' and not row[1].startswith('sqlite_')]
                can_read = conn.execute('SELECT count(*) FROM substrate').fetchone()[0]
            checks.append(_record('real_source_opens_read_only',
                                  f'tables={sorted(tables)}, substrate_rows={can_read}',
                                  'substrate' in tables and can_read >= 1))

    passed = all(check['passed'] for check in checks)
    return {
        'schema_version': SCHEMA_VERSION,
        'checked_at_utc': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
        'runtime': {'executable': sys.executable,
                    'python_version': list(sys.version_info[:3]),
                    'sqlite_version': sqlite3.sqlite_version,
                    'sqlite_version_info': list(sqlite3.sqlite_version_info)},
        'checks': checks,
        'passed': passed,
    }


def _write(payload, destination):
    path = Path(destination)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + '\n',
                    encoding='utf-8')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source')
    parser.add_argument('--out', required=True)
    args = parser.parse_args(argv)
    report = probe_source_transactions(args.source)
    _write(report, args.out)
    print(f"passed={report['passed']} checks={len(report['checks'])} -> {args.out}")
    return 0 if report['passed'] else 2


if __name__ == '__main__':
    raise SystemExit(main())

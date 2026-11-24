#!/usr/bin/env python3
"""Build two replayable handoff drill materials from one certified fixture.

    python3 -m examples.handoff_drill --out /tmp/keelsplice-drill

Outputs:

  material-a-csv/         typed CSVs + roster.json; no SQLite file at all
    typed/<dataset>/...   one JSON cell per column, storage class preserved
    naive/<dataset>/...   the same rows under an "empty means NULL" export
    received/             what receiver A reported, already produced here
  material-b-sqlite/      the certified bytes, a live control fixture,
                          certificate.json, roster.json, received/
  control-captures.json   the service's own verdicts on the control rows
  ledger.json             per-conclusion answers from both receivers, which
                          representation each one needs, and what the exchange
                          measured as lost
  README.md               replay commands

The certified fixture is produced by the real service: register a recipe with
three roots, seal, build, certify, download.  That path refuses any capture whose
composite edge points at no parent row (`capture.parents` raises
`invalid_reference`) and `bundle.build` runs `foreign_key_check`.  A certified
artifact therefore only ever shows the NULL half of the composite-key question.

The control fixture is generated here from the same DDL and keeps it.  It adds
the rows the certified path must reject: a genuinely absent parent, an empty
string that must not be mistaken for NULL, two collation traps (ASCII case and
two Unicode spellings SQLite's NOCASE leaves alone), and a hand-copied column
with one deliberately wrong value.  Every list in the ledger is recomputed from
the written files; the run aborts if the two receivers disagree.
"""

import argparse
import base64
import csv
import hashlib
import json
import shutil
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from keelsplice.api import create_server

from .walkthrough import seed

RECEIVERS = Path(__file__).resolve().parent / 'receivers'
NULL_TOKEN = ''  # the "empty field means NULL" convention the plain export uses

# The drill's answers on the certified artifact.  These are properties of the
# shipped schema and recipe, not of any run: two runs produce identical bytes.
EXPECTED = {
    'null_component': ['[{"t":"text","v":"S-03"}]'],
    'missing_parent': [],
    'collation_only': ['[{"t":"text","v":"S-01"}]', '[{"t":"text","v":"S-02"}]', '[{"t":"text","v":"S-04"}]'],
    'control_missing_parent': ['[{"t":"text","v":"P-EMPTY"}]', '[{"t":"text","v":"P-FULL"}]',
                               '[{"t":"text","v":"P-LONGS"}]', '[{"t":"text","v":"P-MISS"}]'],
    'control_null_component': ['[{"t":"text","v":"P-NULL"}]', '[{"t":"text","v":"S-03"}]'],
    'control_recompute': ['[{"t":"text","v":"Q-BAD"}]'],
    'naive_control_null_component': ['[{"t":"text","v":"P-EMPTY"}]', '[{"t":"text","v":"P-NULL"}]',
                                     '[{"t":"text","v":"S-03"}]'],
}

CONTROL_EDGE_ROWS = [
    # NULL component: SQLite requires no parent, so this is not a violation.
    ('P-NULL', 'West', None, 11, 1.5),
    # Empty text, not NULL: both components are present, so a parent IS required.
    ('P-EMPTY', '', '', 11, 2.0),
    # Present, complete, no such parent: a real dangling reference.
    ('P-MISS', 'North', 'Quartz', 11, 2.5),
    # Resolves only if NOCASE is applied.
    ('P-CASE', 'east', 'mica', 12, 3.0),
    ('P-PRUNED', 'West', 'Basalt', 13, 3.5),
    # NOCASE is ASCII-only: neither of these resolves.
    ('P-FULL', 'ＥＡＳＴ', 'mica', 13, 4.0),
    ('P-LONGS', 'Eaſt', 'mica', 13, 4.25),
    # ASCII spelling that does resolve.
    ('P-ASCII', 'EAST', 'mica', 13, 4.5),
]
CONTROL_PLAIN_ROWS = [
    # Plain columns whose stored values match the formula ...
    ('Q-GOOD', 'East', 'Mica', 5.0, 10.0),
    # ... except here, so a formula check catches a hand-copied error.
    ('Q-BAD', 'West', 'Basalt', 5.0, 99.0),
    ('Q-NULL', 'West', None, 1.0, 2.0),
    # A correct product that a six-significant-digit export cannot reproduce.
    ('Q-PREC', 'East', 'Mica', 0.3333333333333333, 0.6666666666666666),
]


def checkpoint(path):
    """Fold any WAL back into the file so the handoff is one self-contained file."""
    with closing(sqlite3.connect(path)) as conn:
        conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        conn.execute('PRAGMA journal_mode=DELETE')


def call(base, route, body=None, status=200):
    headers = {'Content-Type': 'application/json'}
    data = None if body is None else json.dumps(body).encode('utf-8')
    try:
        response = urlopen(Request(base + route, data, headers), timeout=10)
    except HTTPError as exc:
        response = exc
    with response:
        raw = response.read()
        if response.status != status:
            raise AssertionError(f'{route}: wanted {status}, got {response.status}: {raw[:400]!r}')
        if response.headers.get_content_type() == 'application/vnd.sqlite3':
            return raw, response.headers['ETag'].strip('"')
        return json.loads(raw)


# ----- rendering ----------------------------------------------------------

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


def naive(value):
    """The common export convention: no types, empty field means NULL."""
    if value is None:
        return NULL_TOKEN
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, float):
        return f'{value:.6g}'
    return str(value)


def render(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def write_dataset(material, dataset, database, roster, lossy):
    tables = roster['datasets'][dataset]['tables']
    directory = material / ('naive' if lossy else 'typed') / dataset
    directory.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(database)) as conn:
        for table in tables:
            columns = roster['tables'][table]['columns']
            pk = roster['tables'][table]['pk']
            projection = ','.join('"' + name.replace('"', '""') + '"' for name in columns)
            order = ','.join('"' + name.replace('"', '""') + '"' for name in pk)
            rows = conn.execute(f'SELECT {projection} FROM "{table}" ORDER BY {order}').fetchall()
            with (directory / f'{table}.csv').open('w', newline='', encoding='utf-8') as handle:
                writer = csv.writer(handle, lineterminator='\n')
                writer.writerow(columns)
                for row in rows:
                    cells = [naive(value) for value in row] if lossy else [render(typed(value)) for value in row]
                    writer.writerow(cells)


# ----- fixtures -----------------------------------------------------------

def build_control(source, target):
    shutil.copyfile(source, target)
    with closing(sqlite3.connect(target)) as conn, conn:
        conn.execute('PRAGMA journal_mode=DELETE')
        conn.execute('PRAGMA foreign_keys=OFF')
        conn.execute('''CREATE TABLE assay_plain (
            mark TEXT PRIMARY KEY NOT NULL,
            region TEXT,
            code TEXT,
            length_um REAL NOT NULL,
            doubled REAL NOT NULL,
            FOREIGN KEY (region, code) REFERENCES substrate
        )''')
        conn.executemany('INSERT INTO assay(mark,region,code,cassette,length_um) VALUES(?,?,?,?,?)',
                         CONTROL_EDGE_ROWS)
        conn.executemany('INSERT INTO assay_plain(mark,region,code,length_um,doubled) VALUES(?,?,?,?,?)',
                         CONTROL_PLAIN_ROWS)


def make_roster(path, datasets):
    """Describe both datasets from the control schema.

    The roster is taken from the live control file, so collations, primary keys
    and the GENERATED clause come from SQLite itself, not from this source file.
    """
    with closing(sqlite3.connect(path)) as conn:
        conn.row_factory = sqlite3.Row
        tables, collations = {}, {}
        names = [row[0] for row in conn.execute(
            "SELECT name FROM main.sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        for name in names:
            columns = [dict(row) for row in conn.execute(f'PRAGMA main.table_xinfo("{name}")')]
            tables[name] = {
                'columns': [col['name'] for col in columns if col['hidden'] in (0, 2, 3)],
                'pk': [col['name'] for col in sorted(columns, key=lambda col: col['pk']) if col['pk']],
                'generated_columns': [col['name'] for col in columns if col['hidden'] in (2, 3)],
                'ddl': conn.execute("SELECT sql FROM main.sqlite_schema WHERE type='table' AND name=?",
                                    (name,)).fetchone()[0],
            }
            for index in conn.execute(f'PRAGMA main.index_list("{name}")'):
                if index['origin'] != 'pk':
                    continue
                for info in conn.execute(f'PRAGMA main.index_xinfo("{index["name"]}")'):
                    if info['key'] and info['name'] is not None:
                        collations.setdefault(name, {})[info['name']] = info['coll']
        groups = {}
        for row in conn.execute('PRAGMA main.foreign_key_list("assay")'):
            groups.setdefault(row['id'], []).append(dict(row))
        rows = sorted(groups.items())[0][1]
        rows.sort(key=lambda row: row['seq'])
        parent = rows[0]['table']
        parent_cols = [row['to'] for row in rows]
        if any(col is None for col in parent_cols):
            parent_cols = tables[parent]['pk']
        edge_id = sorted(groups)[0]
    tables['assay']['generated'] = {
        'column': 'doubled', 'input': 'length_um', 'kind': 'multiply', 'factor': 2.0,
        'declared': [line.strip() for line in tables['assay']['ddl'].splitlines() if 'GENERATED' in line],
    }
    tables['assay_plain']['claimed'] = {
        'column': 'doubled', 'input': 'length_um', 'kind': 'multiply', 'factor': 2.0,
        'note': 'this roster asserts the same formula assay uses; the cells cannot show that '
                'assay_plain.doubled is stored rather than generated',
    }
    return {
        'format': 'keelsplice-handoff-roster/1',
        'datasets': datasets,
        'focus': {'child': 'assay', 'parent': parent, 'edge_id': edge_id,
                  'child_cols': [row['from'] for row in rows], 'parent_cols': parent_cols,
                  'collations': {col: collations.get(parent, {}).get(col, 'BINARY') for col in parent_cols}},
        'tables': tables,
    }


# ----- replay -------------------------------------------------------------

def run_receiver(script, arguments):
    command = [sys.executable, '-I', str(RECEIVERS / script), *map(str, arguments)]
    completed = subprocess.run(command, capture_output=True, text=True)
    if completed.returncode not in (0, 2):
        raise AssertionError(f'{script} failed ({completed.returncode}): {completed.stderr[:1200]}')
    return json.loads(completed.stdout), completed.returncode


def receiver_inputs(csv_material, sqlite_material):
    """Run each receiver on each dataset and return the parsed reports."""
    reports = {}
    for name, dataset in (('typed-certified', 'certified'), ('typed-control', 'control')):
        reports[name], _ = run_receiver('receiver_csv.py', [csv_material, '--dataset', dataset])
    for name, dataset, extra in (
            ('naive-certified', 'certified', []),
            ('naive-control', 'control', []),
            ('naive-control-casefold', 'control', ['--fold', 'casefold'])):
        reports[name], _ = run_receiver('receiver_csv.py', [csv_material, '--dataset', dataset,
                                                            '--mode', 'naive', '--null-token', NULL_TOKEN, *extra])
    reports['sqlite-artifact'], _ = run_receiver(
        'receiver_sqlite.py', [sqlite_material, '--export-material', csv_material])
    reports['sqlite-control'], _ = run_receiver(
        'receiver_sqlite.py', [sqlite_material, '--database', 'control.sqlite', '--no-certificate',
                               '--dataset', 'control', '--export-material', csv_material])
    return reports


def focus_comparisons(reports, focus_edge):
    def pair(csv_key, sqlite_key, field, table=None):
        csv_value = reports[csv_key][field] if table is None else reports[csv_key][field][table]['mismatch']
        sqlite_value = (reports[sqlite_key][field][focus_edge] if table is None
                        else reports[sqlite_key][field][table]['mismatch'])
        return {'csv_typed': csv_value, 'sqlite': sqlite_value, 'agree': csv_value == sqlite_value}

    return {
        'certified': {field: pair('typed-certified', 'sqlite-artifact', field)
                      for field in ('null_component', 'missing_parent', 'collation_only')},
        'control': {field: pair('typed-control', 'sqlite-control', field)
                    for field in ('null_component', 'missing_parent', 'collation_only')},
        'recompute_certified': pair('typed-certified', 'sqlite-artifact', 'recompute', 'assay'),
        'recompute_control': pair('typed-control', 'sqlite-control', 'recompute', 'assay_plain'),
    }


def verify_expectations(reports, roster):
    """Pin the drill's own answers, so a silent semantic drift cannot pass."""
    edge = f"{roster['focus']['child']}#{roster['focus']['edge_id']}"
    observed = {
        'null_component': reports['typed-certified']['null_component'],
        'missing_parent': reports['typed-certified']['missing_parent'],
        'collation_only': reports['typed-certified']['collation_only'],
        'control_null_component': reports['typed-control']['null_component'],
        'control_missing_parent': reports['typed-control']['missing_parent'],
        'control_recompute': reports['typed-control']['recompute']['assay_plain']['mismatch'],
        'naive_control_null_component': reports['naive-control']['null_component'],
    }
    sqlite_values = {
        'null_component': reports['sqlite-artifact']['null_component'][edge],
        'missing_parent': reports['sqlite-artifact']['missing_parent'][edge],
        'collation_only': reports['sqlite-artifact']['collation_only'][edge],
        'control_null_component': reports['sqlite-control']['null_component'][edge],
        'control_missing_parent': reports['sqlite-control']['missing_parent'][edge],
        'control_recompute': reports['sqlite-control']['recompute']['assay_plain']['mismatch'],
    }
    wrong = [f'{name}: csv {observed[name]!r}' for name in observed
             if observed[name] != EXPECTED[name]]
    wrong += [f'{name}: sqlite {sqlite_values[name]!r}' for name in sqlite_values
              if sqlite_values[name] != EXPECTED[name]]
    if wrong:
        raise AssertionError('the drill answers changed: ' + '; '.join(wrong))


def build_ledger(reports, comparisons, certificate, digest):
    naive_control = reports['naive-control']
    casefold_control = reports['naive-control-casefold']
    sqlite_artifact = reports['sqlite-artifact']
    sqlite_control = reports['sqlite-control']

    def answer(dataset, field):
        return comparisons[dataset][field]

    conclusions = [
        {'id': 'C1',
         'question': 'Which child keys have a NULL component, so no parent row is required?',
         'answer': {'certified': answer('certified', 'null_component'),
                    'control': answer('control', 'null_component'),
                    'naive_control': naive_control['null_component']},
         'needs': 'a per-cell NULL marker plus the edge column list',
         'lost_in_csv_exchange': [
             'the plain export spells NULL as an empty field, so P-EMPTY ("","") is read as a '
             'NULL key and joins this list falsely',
             'choosing the opposite convention (empty means empty text) drops the real NULL key '
             'S-03 instead; no single convention is right for both',
             'without the roster the child key columns and their order are unknown'],
         'agree': comparisons['certified']['null_component']['agree']
                  and comparisons['control']['null_component']['agree']},
        {'id': 'C2',
         'question': 'Which complete child keys have no parent row at all?',
         'answer': {'certified': {'csv_typed': reports['typed-certified']['missing_parent'],
                                  'sqlite': sqlite_artifact['missing_parent'][comparisons['focus_edge']]},
                    'control': {'csv_typed': reports['typed-control']['missing_parent'],
                                'sqlite': sqlite_control['missing_parent'][comparisons['focus_edge']],
                                'sqlite_fk_check': sqlite_control['foreign_key_check'],
                                'naive': naive_control['missing_parent']}},
         'needs': 'a control fixture containing a genuinely dangling row; the certified path '
                  'refuses to seal one, so the artifact holds none',
         'lost_in_csv_exchange': [
             'the certified bytes cannot answer this at all: Keelsplice rejects the capture',
             'the plain export loses P-EMPTY (read as NULL) and P-FULL/P-LONGS (spelling kept, '
             'parent comparison still possible only with the roster collation)',
             'foreign_key_check omits the NULL row entirely and returns only a rowid, not typed keys',
             'typed cells plus the roster let the CSV receiver reproduce the SQLite list exactly'],
         'agree': (reports['typed-control']['missing_parent']
                   == sqlite_control['missing_parent'][comparisons['focus_edge']]
                   and reports['typed-certified']['missing_parent'] == []
                   and sqlite_artifact['missing_parent'][comparisons['focus_edge']] == [])},
        {'id': 'C3',
         'question': 'Which child rows reach a parent only because of the declared key collation?',
         'answer': {'certified': answer('certified', 'collation_only'),
                    'control': answer('control', 'collation_only'),
                    'pk_collations': sqlite_artifact['declared']['pk_collations'],
                    'casefold_control': casefold_control['collation_only']},
         'needs': 'the declared collation of every parent-key column (SQLite metadata; roster for CSV)',
         'lost_in_csv_exchange': [
             'a flat text export cannot record COLLATE NOCASE',
             'guessing Python casefold differs from SQLite in both directions: P-LONGS now equals '
             '"east" under casefold and would wrongly resolve, while P-FULL matches under neither',
             'the SQLite receiver reads collations from the file; the CSV receiver needs the roster'],
         'agree': comparisons['certified']['collation_only']['agree']
                  and comparisons['control']['collation_only']['agree']},
        {'id': 'C4',
         'question': 'Does a named column still recompute from its inputs after transfer?',
         'answer': {'certified': comparisons['recompute_certified'],
                    'control': comparisons['recompute_control'],
                    'certified_declared': sqlite_artifact['recompute']['assay']['declared_generated_columns'],
                    'control_declared': sqlite_control['recompute']['assay_plain']['declared_generated_columns'],
                    'naive_control': naive_control['recompute']['assay_plain']},
         'needs': 'two representations: the DDL proves the column is GENERATED; exact REAL bits '
                  'prove the arithmetic.  Neither alone answers both halves.',
         'lost_in_csv_exchange': [
             'the GENERATED declaration lives in the DDL, not in any cell',
             'six-significant-digit REAL text makes the correct Q-PREC product look wrong',
             'the hand-copied assay_plain.doubled matches the formula everywhere except Q-BAD, so a '
             'passing formula check does not prove the column is generated'],
         'agree': comparisons['recompute_certified']['agree'] and comparisons['recompute_control']['agree']},
        {'id': 'C5',
         'question': 'Are the handed-off bytes the certified bytes?',
         'answer': {'sqlite_sha256': sqlite_artifact['sha256'],
                    'certificate_sha256': sqlite_artifact['certificate_sha256'],
                    'bytes_match': sqlite_artifact['bytes_match_certificate'],
                    'counts_match': sqlite_artifact['counts_match_certificate']},
         'needs': 'the original file bytes (SQLite only)',
         'lost_in_csv_exchange': ['a CSV export is derived text; it cannot be hashed against the certificate'],
         'agree': sqlite_artifact['bytes_match_certificate'] and sqlite_artifact['counts_match_certificate']},
        {'id': 'C6',
         'question': 'What does PRAGMA foreign_key_check report, and what does it hide?',
         'answer': {'certified_rows': sqlite_artifact['foreign_key_check'],
                    'control_rows': sqlite_control['foreign_key_check']},
         'needs': 'a live SQLite connection (SQLite only)',
         'lost_in_csv_exchange': ['no SQLite, no pragma'],
         'agree': (sqlite_artifact['foreign_key_check'] == []
                   and bool(sqlite_control['foreign_key_check']))},
        {'id': 'C7',
         'question': 'What does the plain CSV export itself drop, measured against the database?',
         'answer': {'certified': sqlite_artifact.get('export_comparison'),
                    'control': sqlite_control.get('export_comparison')},
         'needs': 'both representations, so the loss can be counted rather than assumed',
         'lost_in_csv_exchange': ['this is the measurement, not a claim'],
         'agree': bool(sqlite_artifact.get('export_comparison')) and bool(sqlite_control.get('export_comparison'))},
    ]
    return {'format': 'keelsplice-handoff-ledger/1',
            'certificate_sha256': digest, 'certificate_counts': certificate['counts'],
            'focus_edge': comparisons['focus_edge'],
            'conclusions': conclusions,
            'naive_losses': {
                'null_list_typed_vs_naive': {'typed': reports['typed-control']['null_component'],
                                             'naive': naive_control['null_component']},
                'ambiguous_plain_cells': naive_control['ambiguous_null_cells'],
                'recompute_typed_vs_naive': {'typed': reports['typed-control']['recompute']['assay_plain']['mismatch'],
                                             'naive': naive_control['recompute']['assay_plain']['mismatch']},
                'casefold_vs_nocase': {'casefold': casefold_control['missing_parent'],
                                       'nocase': reports['typed-control']['missing_parent']}},
            'receiver_checks': [
                {'check': f'{dataset} {field}', 'csv': comparisons[dataset][field]['csv_typed'],
                 'sqlite': comparisons[dataset][field]['sqlite'],
                 'agree': comparisons[dataset][field]['agree']}
                for dataset in ('certified', 'control')
                for field in ('null_component', 'missing_parent', 'collation_only')
            ] + [
                {'check': 'control recompute', 'csv': comparisons['recompute_control']['csv_typed'],
                 'sqlite': comparisons['recompute_control']['sqlite'],
                 'agree': comparisons['recompute_control']['agree']}]}


def write_readmes(out, certificate, ledger):
    repo = RECEIVERS.parent.parent
    replay = f'''# Replay

Python 3.11+ and the standard library only.  These commands work from any
directory once `DRILL` names this output directory and `REPO` the checkout
that produced it:

```sh
DRILL={out}
REPO={repo}
python3 "$REPO/examples/receivers/receiver_csv.py" "$DRILL/material-a-csv" --dataset certified
python3 "$REPO/examples/receivers/receiver_csv.py" "$DRILL/material-a-csv" --dataset control
python3 "$REPO/examples/receivers/receiver_csv.py" "$DRILL/material-a-csv" --dataset control --mode naive --null-token ''
python3 "$REPO/examples/receivers/receiver_csv.py" "$DRILL/material-a-csv" --dataset control --mode naive --null-token '' --fold casefold
python3 "$REPO/examples/receivers/receiver_sqlite.py" "$DRILL/material-b-sqlite" --export-material "$DRILL/material-a-csv"
python3 "$REPO/examples/receivers/receiver_sqlite.py" "$DRILL/material-b-sqlite" --database control.sqlite --no-certificate
```

Exit status 0 means clean and 2 means findings; the control run is built to
report findings, so its 2 is the expected result.

Regenerate both materials and the ledger from the repository root with:

```sh
python3 -m examples.handoff_drill --out <fresh-directory>
```

Certified artifact: SHA-256 `{certificate['sha256']}`, {certificate['bytes']} bytes,
counts {json.dumps(certificate['counts'], sort_keys=True)}.
'''
    (out / 'material-a-csv' / 'README.md').write_text(replay + '''
## Material A — CSV exchange

`roster.json` names the focus edge (`assay.region, assay.code → substrate.region,
substrate.code`), each parent-key column's declared collation (`NOCASE`), primary
keys, and the column formulas this roster licenses.

`typed/` carries one JSON cell per column, so storage class survives: NULL, empty
text, an integer, a hex REAL and a base64 BLOB stay distinct.  This is the form
that can reproduce the SQLite answers.

`naive/` is the same rows under an "empty field means NULL, REAL with six digits"
convention.  Keep it for contrast; it is not a deliverable.

`received/` holds the reports this run already produced.

Receiver A cannot open a SQLite file: there is none in this directory.
''', encoding='utf-8')
    (out / 'material-b-sqlite' / 'README.md').write_text(replay + '''
## Material B — SQLite exchange

`artifact.sqlite` is the downloaded, certified fixture.  `certificate.json` binds
its SHA-256, byte length, per-table counts, capture digest and schema digest.
`control.sqlite` is the live control fixture: the same DDL plus `assay_plain` and
the probe rows the certified path rejects.

Receiver B recomputes everything from the file: primary-key collations from
`PRAGMA index_xinfo`, the GENERATED declaration from `PRAGMA table_xinfo`, parent
existence with a `parent = +child COLLATE <declared>` probe (a bare `child =
parent` silently adopts the child's BINARY collation), and byte identity by
hashing.  `--no-certificate` skips the certificate comparison, which is what the
control run needs.  `--export-material` reads the plain export from material A
and counts what it lost.
''', encoding='utf-8')
    (out / 'README.md').write_text(f'''# Keelsplice handoff drill

Two self-contained materials generated from one certified fixture:

- `material-a-csv/` — a CSV exchange a standard-library-only receiver can read
- `material-b-sqlite/` — the certified bytes, plus a live control fixture

`ledger.json` gives, per conclusion: both receivers' answers, which representation
the conclusion needs, and what the exchange measured as lost.  This run's claims:

| id | conclusion | needs |
| --- | --- | --- |
''' + '\n'.join(f"| {item['id']} | {item['question']} | {item['needs']} |"
                    for item in ledger['conclusions']) + '''

Every list above was recomputed by both receivers from these files; the run
aborted if they disagreed, so a disagreement means these files were edited.
''', encoding='utf-8')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True, help='fresh output directory')
    args = parser.parse_args()
    out = args.out.resolve()
    if out.exists() and any(out.iterdir()):
        parser.error(f'{out} is not empty; choose a fresh directory')
    out.mkdir(parents=True, exist_ok=True)
    material_a = out / 'material-a-csv'
    material_b = out / 'material-b-sqlite'
    material_a.mkdir()
    material_b.mkdir()
    with TemporaryDirectory(prefix='keelsplice-drill-') as directory:
        work = Path(directory)
        source = work / 'source.sqlite'
        seed(source)
        control = work / 'control.sqlite'
        build_control(source, control)
        with create_server(('127.0.0.1', 0), work / 'state.sqlite',
                           {'microscope': source, 'control': control}) as server:
            thread = Thread(target=server.serve_forever, kwargs={'poll_interval': 0.02})
            thread.start()
            base = f'http://127.0.0.1:{server.server_port}'
            try:
                body = {'source': 'microscope',
                        'roots': [{'table': 'substrate', 'key': ['east', 'MICA']},
                                  {'table': 'assay', 'key': ['S-03']},
                                  {'table': 'assay', 'key': ['S-04']}],
                        'follow_children': [{'child': 'assay', 'id': 0}], 'row_limit': 50}
                recipe = call(base, '/recipes/drill/versions', body, 201)
                sealed = call(base, '/captures', {'name': 'drill', 'version': 1}, 201)
                capture_id = sealed['id']
                call(base, f'/captures/{capture_id}/build', {})
                certified = call(base, f'/captures/{capture_id}/certify', {})
                artifact, etag = call(base, f'/captures/{capture_id}/database')
                certificate = certified['certificate']
                if not (digest := hashlib.sha256(artifact).hexdigest()) == etag == certificate['sha256']:
                    raise AssertionError('download bytes, ETag and certificate disagree')
                call(base, '/recipes/missing/versions',
                     {'source': 'control', 'roots': [{'table': 'assay', 'key': ['P-MISS']}], 'row_limit': 20}, 201)
                rejected = call(base, '/captures', {'name': 'missing', 'version': 1}, 422)
                if rejected.get('error') != 'invalid_reference':
                    raise AssertionError(f'expected invalid_reference, got {rejected}')
                call(base, '/recipes/nullref/versions',
                     {'source': 'control', 'roots': [{'table': 'assay', 'key': ['P-NULL']}], 'row_limit': 20}, 201)
                null_capture = call(base, '/captures', {'name': 'nullref', 'version': 1}, 201)
                call(base, f"/captures/{null_capture['id']}/build", {})
                null_certified = call(base, f"/captures/{null_capture['id']}/certify", {})
            finally:
                server.shutdown()
                thread.join()
        checkpoint(control)
        (out / 'artifact.sqlite').write_bytes(artifact)
        (out / 'control.sqlite').write_bytes(control.read_bytes())
        certificate_document = {
            'capture_id': capture_id, 'sha256': digest, 'etag': etag,
            'bytes': certificate['bytes'], 'counts': certificate['counts'],
            'schema_digest': certificate['schema_digest'], 'capture_digest': certificate['capture_digest'],
            'recipe': recipe['recipe'], 'roots': sealed['roots'], 'summary': sealed['summary'],
            'download_state': certified['state'],
            'control_capture': {'missing_parent_rejected': rejected,
                                'null_component_accepted': {'state': null_certified['state'],
                                                            'sha256': null_certified['certificate']['sha256'],
                                                            'counts': null_certified['certificate']['counts']}},
        }
        (out / 'certificate.json').write_text(json.dumps(certificate_document, indent=2, sort_keys=True) + '\n',
                                              encoding='utf-8')
        datasets = {
            'certified': {'tables': ['substrate', 'cassette', 'assay', 'frame'], 'source': 'artifact.sqlite'},
            'control': {'tables': ['substrate', 'cassette', 'assay', 'assay_plain', 'frame'],
                        'source': 'control.sqlite'},
        }
        roster = make_roster(control, datasets)
        write_dataset(material_a, 'certified', out / 'artifact.sqlite', roster, lossy=False)
        write_dataset(material_a, 'certified', out / 'artifact.sqlite', roster, lossy=True)
        write_dataset(material_a, 'control', out / 'control.sqlite', roster, lossy=False)
        write_dataset(material_a, 'control', out / 'control.sqlite', roster, lossy=True)
    (out / 'roster.json').write_text(json.dumps(roster, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    (out / 'control-captures.json').write_text(json.dumps(certificate_document['control_capture'],
                                                         indent=2, sort_keys=True) + '\n', encoding='utf-8')
    shutil.copyfile(out / 'roster.json', material_a / 'roster.json')
    shutil.copyfile(out / 'roster.json', material_b / 'roster.json')
    shutil.copyfile(out / 'certificate.json', material_b / 'certificate.json')
    shutil.copyfile(out / 'artifact.sqlite', material_b / 'artifact.sqlite')
    shutil.copyfile(out / 'control.sqlite', material_b / 'control.sqlite')
    reports = receiver_inputs(material_a, material_b)
    comparisons = focus_comparisons(reports, f"{roster['focus']['child']}#{roster['focus']['edge_id']}")
    comparisons['focus_edge'] = f"{roster['focus']['child']}#{roster['focus']['edge_id']}"
    for directory, payload in ((material_a / 'received', {k: v for k, v in reports.items()
                                                           if k.startswith(('typed', 'naive'))}),
                               (material_b / 'received', {k: v for k, v in reports.items()
                                                          if k.startswith('sqlite')})):
        directory.mkdir()
        for name, report in payload.items():
            (directory / f'{name}.json').write_text(
                json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False) + '\n', encoding='utf-8')
    verify_expectations(reports, roster)
    ledger = build_ledger(reports, comparisons, certificate, digest)
    (out / 'ledger.json').write_text(json.dumps(ledger, indent=2, sort_keys=True, ensure_ascii=False) + '\n',
                                     encoding='utf-8')
    failed = [check['check'] for check in ledger['receiver_checks'] if not check['agree']]
    failed += [item['id'] for item in ledger['conclusions'] if not item['agree']]
    if failed:
        raise AssertionError(f'ledger disagreement: {failed}')
    write_readmes(out, certificate, ledger)
    print(json.dumps({'out': str(out), 'sha256': digest, 'counts': certificate['counts'],
                      'conclusions': len(ledger['conclusions']),
                      'checks': len(ledger['receiver_checks'])}, indent=2, sort_keys=True))


if __name__ == '__main__':
    main()

"""Phase 1: build the legacy repository with a real Python 3.11 interpreter.

The resulting directory is the upgrade input:

* ``source/microscope.sqlite``        - the registered read-only source
* ``state.sqlite``                    - recipes, sealed captures, artifacts
* ``legacy_fixture.sqlite``           - certified bytes handed back over HTTP
* ``legacy_manifest.json``            - ids, digests and counts to re-verify
* ``reports/``                        - runtime, wire, fixture and re-read reports

The phase refuses to run unless the interpreter really is a 3.11 build, so the
legacy artifacts cannot accidentally be produced by the new deployment.
"""

import argparse
import hashlib
import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from upgrade._subprocess import LoopbackServer  # noqa: E402
from upgrade.fixture_probe import reread_fixture, reread_repository  # noqa: E402
from upgrade.http_form_probe import run_probe  # noqa: E402
from upgrade.runtime_probe import collect as runtime_collect  # noqa: E402
from upgrade.source_transaction import probe_source_transactions  # noqa: E402

SCHEMA_VERSION = 1
RECIPE_BODY = {
    'source': 'microscope',
    'roots': [{'table': 'SUBSTRATE', 'key': ['east', 'MICA']}],
    'follow_children': [{'child': 'assay', 'id': 0}],
    'row_limit': 20,
}


def require_python_311():
    if sys.version_info[:2] != (3, 11):
        raise SystemExit(
            f'Legacy repository must be built with Python 3.11, got '
            f'{sys.version_info.major}.{sys.version_info.minor} at {sys.executable}')


def seed_source(path):
    sql = (PROJECT_ROOT / 'examples' / 'microscope.sql').read_text(encoding='utf-8')
    with closing(sqlite3.connect(path)) as conn:
        conn.executescript(sql)


def build_legacy_repository(output, sqlite_cli):
    require_python_311()
    output = Path(output).resolve()
    (output / 'source').mkdir(parents=True, exist_ok=True)
    (output / 'reports').mkdir(parents=True, exist_ok=True)
    source = output / 'source' / 'microscope.sqlite'
    repository = output / 'state.sqlite'
    seed_source(source)

    with LoopbackServer(sys.executable, repository, {'microscope': source}) as server:
        base = server.request
        catalog = base('/sources/microscope/catalog')
        edge = next(edge for edge in catalog['schema']['edges']
                    if edge['child'] == 'assay' and edge['parent'] == 'substrate')
        assert edge['parent_cols'] == ['region', 'code']

        recipe_body = dict(RECIPE_BODY, follow_children=[{'child': 'assay', 'id': edge['id']}])
        version1 = base('/recipes/swatch/versions', recipe_body, 201)
        assert version1['version'] == 1
        # A second, intentionally tight version so retired versions exist too.
        base('/recipes/swatch/versions', dict(recipe_body, row_limit=1), 201)
        retired = base('/recipes/swatch/versions/2/retire', {})
        assert retired['active'] is False
        base('/preview', {'name': 'swatch', 'version': 2}, 409)

        preview = base('/preview', {'name': 'swatch', 'version': 1})
        assert preview['summary']['counts'] == {
            'substrate': 1, 'cassette': 2, 'assay': 2, 'frame': 0}

        first = base('/captures', {'name': 'swatch', 'version': 1}, 201)
        first_id = first['id']
        assert first['roots'] == [['substrate', ['East', 'Mica']]]
        # Mutate the source; sealed captures must keep the pre-mutation snapshot.
        with closing(sqlite3.connect(source)) as conn, conn:
            conn.execute("UPDATE assay SET length_um=4.75 WHERE mark='S-01'")
        second = base('/captures', {'name': 'swatch', 'version': 1}, 201)
        second_id = second['id']

        diff = base('/compare', {'left': first_id, 'right': second_id})
        assert diff['changes']['assay']['modified'] == [['S-01']]

        traced = base(f'/captures/{first_id}/trace',
                      {'table': 'substrate', 'key': ['East', 'Mica']})
        assert traced['roots'] == [0] and len(traced['causes']) == 2
        base(f'/captures/{first_id}/trace',
             {'table': 'substrate', 'key': ['east', 'MICA']}, 404)

        base(f'/captures/{first_id}/certify', {}, 409)
        base(f'/captures/{first_id}/build', {})
        certified = base(f'/captures/{first_id}/certify', {})
        certificate = certified['certificate']
        data, etag = base(f'/captures/{first_id}/database')
        assert hashlib.sha256(data).hexdigest() == etag == certificate['sha256']

        # Full browser form lifecycle against the same repository (3.11 side).
        wire_report = run_probe(repository, source, 'legacy', 'legacy-wire', sqlite_cli)

    fixture_path = output / 'legacy_fixture.sqlite'
    fixture_path.write_bytes(data)
    fixture_report = reread_fixture(data, certificate, sqlite_cli)
    assert fixture_report['passed']
    repository_report = reread_repository(repository)
    assert repository_report['passed']
    transaction_report = probe_source_transactions(source)
    assert transaction_report['passed']

    reports = output / 'reports'
    runtime_report = runtime_collect()
    _write(runtime_report, reports / 'runtime-3.11.json')
    _write(wire_report, reports / 'http-form-3.11.json')
    _write(fixture_report, reports / 'fixture-reread-3.11.json')
    _write(repository_report, reports / 'repository-reread-3.11.json')
    _write(transaction_report, reports / 'source-transaction-3.11.json')

    manifest = {
        'schema_version': SCHEMA_VERSION,
        'built_by_python': list(sys.version_info[:3]),
        'built_by_executable': sys.executable,
        'source_database': str(source),
        'state_repository': str(repository),
        'schema_digest': catalog['schema_digest'],
        'recipes': {'name': 'swatch', 'versions': [1, 2],
                    'retired_versions': [2], 'active_versions': [1]},
        'captures': [
            {'id': first_id, 'order': 1, 'state': 'certified',
             'summary': first['summary'], 'roots': first['roots'],
             'certificate': certificate},
            {'id': second_id, 'order': 2, 'state': 'sealed',
             'summary': second['summary'], 'roots': second['roots']},
        ],
        'certified_fixture': {
            'path': str(fixture_path), 'sha256': etag, 'bytes': len(data),
        },
        'expected_capture_states': {first_id: 'certified', second_id: 'sealed'},
        'expected_recipe_versions': [
            {'name': 'swatch', 'version': 1, 'active': True},
            {'name': 'swatch', 'version': 2, 'active': False},
        ],
        'expected_counts': preview['summary']['counts'],
        'expected_roots': [['substrate', ['East', 'Mica']]],
        'reports': {
            'runtime': str(reports / 'runtime-3.11.json'),
            'http_form': str(reports / 'http-form-3.11.json'),
            'fixture_reread': str(reports / 'fixture-reread-3.11.json'),
            'repository_reread': str(reports / 'repository-reread-3.11.json'),
            'source_transaction': str(reports / 'source-transaction-3.11.json'),
        },
    }
    _write(manifest, output / 'legacy_manifest.json')
    return manifest


def _write(payload, destination):
    path = Path(destination)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + '\n',
                    encoding='utf-8')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True)
    parser.add_argument('--sqlite-cli')
    args = parser.parse_args(argv)
    manifest = build_legacy_repository(args.output, args.sqlite_cli)
    print(f"legacy repository ready at {Path(args.output).resolve()}")
    print(f"certified fixture sha256={manifest['certified_fixture']['sha256']}")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

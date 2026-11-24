"""Phases 2-5: freeze, cutover, post-cutover capture and executable rollback.

The cutover never mutates the only copy of the legacy data.  ``freeze`` takes
an atomic snapshot of the work directory into a timestamped backup before the
new interpreter is allowed to touch anything, and writes the rollback recipe
that ``rollback`` executes later.  The actual switch is a launcher/pointer
operation: state and source bytes are preserved bit for bit, only the recorded
interpreter changes.
"""

import argparse
import json
import shutil
import sqlite3
import sys
from contextlib import closing
from datetime import datetime, timezone
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
MARKER_NAME = 'RUNTIME'
SWITCH_RECORD_NAME = 'switch_record.json'
ROLLBACK_RECORD_NAME = 'rollback.json'


def _now():
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _write(payload, destination):
    path = Path(destination)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + '\n',
                    encoding='utf-8')


def _load_manifest(workdir):
    return json.loads((Path(workdir) / 'legacy_manifest.json').read_text(encoding='utf-8'))


def _checkpoint_wal(path):
    """Force WAL contents into the main database so the snapshot is complete."""
    with closing(sqlite3.connect(path)) as conn:
        conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')


def freeze(workdir, old_python):
    workdir = Path(workdir).resolve()
    manifest = _load_manifest(workdir)
    source = Path(manifest['source_database'])
    repository = Path(manifest['state_repository'])
    _checkpoint_wal(source)
    _checkpoint_wal(repository)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')
    backup_root = workdir / 'backups'
    backup_root.mkdir(parents=True, exist_ok=True)
    # Same-second re-entry (the driver freezes once, then cutover re-freezes)
    # must not collide; keep every freeze recoverable.
    suffix = 0
    while True:
        candidate = backup_root / (f'pre-cutover-{stamp}' if suffix == 0
                                   else f'pre-cutover-{stamp}-{suffix}')
        if not candidate.exists():
            backup = candidate
            break
        suffix += 1
    # Only legacy data belongs in a restore snapshot; switch metadata, markers
    # and generated reports are excluded so a freeze never snapshots itself.
    shutil.copytree(workdir, backup, ignore=shutil.ignore_patterns(
        'backups', 'reports', MARKER_NAME, SWITCH_RECORD_NAME, ROLLBACK_RECORD_NAME))

    before = reread_repository(repository)
    assert before['passed'], 'pre-cutover repository re-read failed'
    record = {
        'schema_version': SCHEMA_VERSION,
        'frozen_at_utc': _now(),
        'workdir': str(workdir),
        'backup_directory': str(backup),
        'old_python': old_python,
        'manifest': str(workdir / 'legacy_manifest.json'),
        'state_repository': str(repository),
        'source_database': str(source),
        'pre_freeze_repository_reread': before,
        'frozen_digests': {
            'state_repository': _sha256(repository),
            'source_database': _sha256(source),
            'certified_fixture': manifest['certified_fixture']['sha256'],
        },
    }
    rollback = {
        'schema_version': SCHEMA_VERSION,
        'created_at_utc': _now(),
        'backup_directory': str(backup),
        'workdir': str(workdir),
        'restore': {
            'state_repository': str(repository.relative_to(workdir)),
            'source_database': str(source.relative_to(workdir)),
            'legacy_fixture': str(Path(manifest['certified_fixture']['path']).relative_to(workdir)),
        },
        'old_python': old_python,
        'instructions': [
            f'{old_python} -I {PROJECT_ROOT / "upgrade" / "switch.py"} rollback '
            f'--workdir {workdir}',
            f'then restart the service with: {old_python} -I -c '
            f'"import sys; sys.path.insert(0, {str(PROJECT_ROOT)!r}); '
            f"from keelsplice.__main__ import main; main()\" "
            f'--repository {repository} --source microscope={source}',
        ],
    }
    _write(record, backup / 'freeze_record.json')
    _write(rollback, workdir / ROLLBACK_RECORD_NAME)
    _write(record, workdir / 'backups' / 'last_freeze.json')
    return record, rollback


def cutover(workdir, new_python, sqlite_cli):
    workdir = Path(workdir).resolve()
    manifest = _load_manifest(workdir)
    source = Path(manifest['source_database'])
    repository = Path(manifest['state_repository'])
    freeze_record, _ = freeze(workdir, manifest['built_by_executable'])
    old_digests = freeze_record['frozen_digests']

    # The marker is the only thing that changes in the work directory: it points
    # service start commands at the new interpreter.  Database bytes are touched
    # only by the new runtime opening them read/write through the normal app.
    marker = workdir / MARKER_NAME
    marker.write_text(
        f'# Keelsplice active runtime - written by upgrade.switch cutover\n'
        f'python={new_python}\n'
        f'switched_at_utc={_now()}\n'
        f'state_repository={repository}\n'
        f'source={source}\n', encoding='utf-8')

    with LoopbackServer(new_python, repository, {'microscope': source}) as server:
        # Preservation: every legacy recipe/capture/certificate still answers.
        catalog = server.request('/sources/microscope/catalog')
        assert catalog['schema_digest'] == manifest['schema_digest']
        for expected in manifest['expected_recipe_versions']:
            version = server.request(
                f"/recipes/{expected['name']}/versions/{expected['version']}")
            assert version['active'] == expected['active']
        legacy_capture = manifest['captures'][0]
        status = server.request(f"/captures/{legacy_capture['id']}")
        assert status['state'] == 'certified'
        assert status['summary'] == legacy_capture['summary']
        data, etag = server.request(f"/captures/{legacy_capture['id']}/database")
        assert etag == manifest['certified_fixture']['sha256']
        reread = reread_fixture(data, legacy_capture['certificate'], sqlite_cli)
        assert reread['passed']
        # Retired versions must still block new captures.
        server.request('/captures', {'name': 'swatch', 'version': 2}, 409)

        # New capture on the replica under the new runtime.
        fresh = server.request('/captures', {'name': 'swatch', 'version': 1}, 201)
        fresh_id = fresh['id']
        assert fresh['roots'] == manifest['expected_roots']
        built = server.request(f'/captures/{fresh_id}/build', {})
        assert built['state'] == 'built'
        certified = server.request(f'/captures/{fresh_id}/certify', {})
        fresh_certificate = certified['certificate']
        fresh_bytes, fresh_etag = server.request(f'/captures/{fresh_id}/database')
        assert fresh_etag == fresh_certificate['sha256']
        fresh_reread = reread_fixture(fresh_bytes, fresh_certificate, sqlite_cli)
        assert fresh_reread['passed']
        wire = run_probe(repository, source, 'switched', 'post-cutover', sqlite_cli)

    post_repository = reread_repository(repository)
    assert post_repository['passed']
    transaction = probe_source_transactions(source)
    assert transaction['passed']
    new_digests = {
        'state_repository': _sha256(repository),
        'source_database': _sha256(source),
        'certified_fixture': _sha256(Path(manifest['certified_fixture']['path'])),
    }
    reports = workdir / 'reports'
    _write(runtime_collect(), reports / 'runtime-3.14.json')
    _write(wire, reports / 'http-form-3.14-switched.json')
    _write(reread, reports / 'fixture-reread-legacy-under-3.14.json')
    _write(fresh_reread, reports / 'fixture-reread-new-capture-3.14.json')
    _write(post_repository, reports / 'repository-reread-3.14.json')
    _write(transaction, reports / 'source-transaction-3.14.json')

    record = {
        'schema_version': SCHEMA_VERSION,
        'switched_at_utc': _now(),
        'workdir': str(workdir),
        'new_python': new_python,
        'old_python': manifest['built_by_executable'],
        'freeze_record': str(workdir / 'backups' / 'last_freeze.json'),
        'digests': {'before_cutover': old_digests, 'after_cutover': new_digests,
                    'source_bytes_unchanged': old_digests['source_database'] == new_digests['source_database'],
                    'legacy_fixture_bytes_unchanged':
                        old_digests['certified_fixture'] == new_digests['certified_fixture']},
        'preserved': {
            'recipe_versions': post_repository['recipe_count'],
            'retired_versions': post_repository['retired_versions'],
            'capture_states': post_repository['capture_states'],
            'artifact_certificate_mismatches': post_repository['artifact_certificate_mismatches'],
        },
        'new_capture': {'id': fresh_id, 'state': 'certified',
                        'certificate': fresh_certificate,
                        'fixture_sha256': fresh_etag, 'bytes': len(fresh_bytes)},
        'reports': {
            'runtime': str(reports / 'runtime-3.14.json'),
            'http_form_switched': str(reports / 'http-form-3.14-switched.json'),
            'legacy_fixture_reread': str(reports / 'fixture-reread-legacy-under-3.14.json'),
            'new_fixture_reread': str(reports / 'fixture-reread-new-capture-3.14.json'),
            'repository_reread': str(reports / 'repository-reread-3.14.json'),
            'source_transaction': str(reports / 'source-transaction-3.14.json'),
        },
        'rollback': str(workdir / ROLLBACK_RECORD_NAME),
    }
    _write(record, workdir / SWITCH_RECORD_NAME)
    return record


def rollback(workdir):
    workdir = Path(workdir).resolve()
    recipe = json.loads((workdir / ROLLBACK_RECORD_NAME).read_text(encoding='utf-8'))
    backup = Path(recipe['backup_directory'])
    if not backup.is_dir():
        raise SystemExit(f'rollback backup missing: {backup}')
    restored = []
    for relative in recipe['restore'].values():
        target = workdir / relative
        source = backup / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        restored.append(str(relative))
    _checkpoint_wal(workdir / recipe['restore']['state_repository'])
    marker = workdir / MARKER_NAME
    marker.write_text(
        f'# Keelsplice active runtime - rolled back\n'
        f'python={recipe["old_python"]}\n'
        f'rolled_back_at_utc={_now()}\n', encoding='utf-8')
    verification = reread_repository(workdir / recipe['restore']['state_repository'])
    record = {
        'schema_version': SCHEMA_VERSION,
        'rolled_back_at_utc': _now(),
        'backup_directory': str(backup),
        'restored_files': restored,
        'old_python': recipe['old_python'],
        'repository_reread': verification,
        'passed': verification['passed'],
    }
    _write(record, workdir / 'backups' / f'rollback-{datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")}.json')
    if not verification['passed']:
        raise SystemExit('rollback completed but repository re-read failed')
    return record


def _sha256(path):
    import hashlib
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        for chunk in iter(lambda: handle.read(1 << 16), b''):
            digest.update(chunk)
    return digest.hexdigest()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    freeze_cmd = commands.add_parser('freeze')
    freeze_cmd.add_argument('--workdir', required=True)
    freeze_cmd.add_argument('--old-python', required=True)
    cut = commands.add_parser('cutover')
    cut.add_argument('--workdir', required=True)
    cut.add_argument('--new-python', required=True)
    cut.add_argument('--sqlite-cli')
    back = commands.add_parser('rollback')
    back.add_argument('--workdir', required=True)
    args = parser.parse_args(argv)

    if args.command == 'freeze':
        record, rollback_recipe = freeze(args.workdir, args.old_python)
        print(f"frozen -> {record['backup_directory']}")
        print(f"rollback recipe -> {Path(args.workdir).resolve() / ROLLBACK_RECORD_NAME}")
    elif args.command == 'cutover':
        record = cutover(args.workdir, args.new_python, args.sqlite_cli)
        print(f"cutover complete; new capture {record['new_capture']['id']} certified")
        print(f"switch record -> {Path(args.workdir).resolve() / SWITCH_RECORD_NAME}")
    else:
        record = rollback(args.workdir)
        print(f"rollback complete; restored {len(record['restored_files'])} files using {record['old_python']}")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

"""Switchover manifests, read-only replicas, and rollback attribution.

A restart drill keeps the primary repository authoritative while a standby
copy is only allowed to re-verify fixtures that were already certified.  The
manifest records the persistent location and record signature the operator
expects when writes resume, plus the rollback window.  A stale or misplaced
manifest is rejected before the repository opens for writes.
"""

import argparse
import json
import os
import sqlite3
import sys
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.request import urlopen

from .codec import Fault, digest, dumps
from .storage import SWITCHOVER_FORMAT, repository_signature


def _instant(value):
    return value.astimezone(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')


def _read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        raise Fault('handover_manifest', 'Cannot read the switchover manifest', 409) from exc


def export_replica(repository, destination, *, drill_id=None, snapshot_at=None):
    """Write a consistent, provenance-stamped copy of a primary repository."""
    if repository.read_only:
        raise Fault('standby_read_only', 'A standby repository cannot export a replica', 409)
    source = Path(repository.path)
    target = Path(destination).resolve()
    if target.exists():
        raise Fault('replica_exists', 'Refusing to overwrite an existing replica file', 409)
    if not target.parent.is_dir():
        raise Fault('replica_location', 'Replica directory does not exist', 409)
    if target == source or target in repository.sources.values():
        raise ValueError('A replica must not be the source or repository file')
    legacy = sqlite3.connect(target)
    try:
        with closing(sqlite3.connect(source)) as origin:
            origin.backup(legacy)
        signature = repository_signature(legacy)
    finally:
        legacy.close()
    replica = {
        'role': 'standby',
        'snapshot_at': _instant(snapshot_at or datetime.now(timezone.utc)),
        'source_repository': str(source),
        'source_content_signature': signature,
    }
    if drill_id is not None:
        replica['drill_id'] = drill_id
    with closing(sqlite3.connect(target)) as conn:
        with conn:
            conn.execute('CREATE TABLE IF NOT EXISTS repository_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
            conn.execute('INSERT OR REPLACE INTO repository_meta(key,value) VALUES(?,?)',
                         ('replica', dumps(replica)))
    if repository.content_signature() != signature:
        raise Fault('handover_content', 'Primary changed while the replica snapshot was taken', 409)
    return {'replica': replica, 'repository': str(target),
            'sha256': digest(target.read_bytes()), 'bytes': target.stat().st_size}


def plan(repository, standby, export, *, drill_id, rollback_minutes=30, now=None):
    """Build the manifest consumed by the next primary start.

    ``snapshot`` describes what the standby actually observed, while
    ``content_signature`` pins the primary's persistent records at plan time.
    """
    if repository.read_only or not standby.read_only:
        raise Fault('standby_read_only', 'A switchover plan needs one primary and one standby repository', 409)
    if not drill_id or not isinstance(drill_id, str):
        raise Fault('handover_manifest', 'A drill identifier is required', 409)
    if type(rollback_minutes) is not int or not 1 <= rollback_minutes <= 24 * 60:
        raise Fault('handover_manifest', 'Rollback window must be 1 to 1440 minutes', 409)
    stat = os.stat(repository.path)
    moment = now or datetime.now(timezone.utc)
    window = {'opens_at': _instant(moment),
              'closes_at': _instant(moment + timedelta(minutes=rollback_minutes)),
              'minutes': rollback_minutes}
    return {
        'format': SWITCHOVER_FORMAT,
        'drill_id': drill_id,
        'prepared_at': _instant(moment),
        'repository': str(repository.path),
        'device': stat.st_dev,
        'inode': stat.st_ino,
        'content_signature': repository.content_signature(),
        'rollback_window': window,
        'snapshot': {
            'taken_at': standby.replica['snapshot_at'],
            'standby_repository': str(standby.path),
            'standby_content_signature': standby.replica['source_content_signature'],
            'standby_sha256': export['sha256'],
            'recipes': standby.recipe_index(),
            'captures': standby.capture_index(),
        },
    }


def write_manifest(path, manifest):
    target = Path(path)
    if target.exists():
        raise Fault('handover_manifest', 'Refusing to overwrite an existing manifest', 409)
    target.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + '\n',
                      encoding='utf-8')
    return target


def rollback_report(manifest, instances):
    """Name every instance that sealed or sealed/advanced captures after the snapshot."""
    snapshot = manifest.get('snapshot') or {}
    before = {capture['id']: capture for capture in snapshot.get('captures', [])}
    details = []
    for instance in instances:
        captures = instance.get('captures') or []
        added = [capture['id'] for capture in captures if capture['id'] not in before]
        advanced = [capture['id'] for capture in captures
                    if capture['id'] in before and capture['state'] != before[capture['id']]['state']]
        details.append({
            'role': instance.get('role'),
            'authoritative': bool(instance.get('authoritative')),
            'repository': instance.get('repository'),
            'added_captures': added,
            'added_count': len(added),
            'advanced_captures': advanced,
        })
    named = [f'{item["role"]}（{item["repository"]}）新增 {item["added_count"]} 个捕获'
             for item in details if item['added_count']]
    return {
        'drill_id': manifest.get('drill_id'),
        'rollback_window': manifest.get('rollback_window'),
        'snapshot_captures': len(before),
        'instances': details,
        'added_total': sum(item['added_count'] for item in details),
        'summary': ('；'.join(named) + '。') if named else '两个实例自快照以来都没有新增捕获。',
    }


def _fetch_instance(url):
    with urlopen(url.rstrip('/') + '/instance', timeout=5) as response:
        return json.loads(response.read().decode('utf-8'))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    report = commands.add_parser('report', help='name instances that sealed captures after a snapshot')
    report.add_argument('--manifest', required=True)
    report.add_argument('--instance', action='append', required=True, metavar='URL')
    args = parser.parse_args(argv)
    manifest = _read_json(args.manifest)
    if manifest.get('format') != SWITCHOVER_FORMAT:
        parser.error('Unknown switchover manifest format')
    print(json.dumps(rollback_report(manifest, [_fetch_instance(url) for url in args.instance]),
                     ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == '__main__':
    sys.exit(main())

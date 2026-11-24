"""Run a two-process switchover drill with a verify-only standby.

The primary keeps its repository authoritative and can still seal a new capture
while the standby is running.  The standby is a separate repository snapshot and
process: it may only re-verify fixtures that were already certified, and it never
presents the old snapshot's recipe activity as the primary's current state.

Everything runs as two real local processes under a temporary directory, so the
same commands can be re-run on a workstation:

    python3 -m examples.switchover_drill
"""

import argparse
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import time
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from keelsplice import switchover
from keelsplice.storage import Repository

from .walkthrough import seed


class Instance:
    """A real ``python3 -m keelsplice`` process on a loopback port."""

    def __init__(self, root, name, repository, source, *, role='primary', manifest=None):
        self.root = root
        self.name = name
        self.role = role
        self.manifest = manifest
        self.log = root / f'{name}.log'
        self.process = None
        self.base = None
        self.repository = repository
        self.source = source

    def start(self, expected=True):
        port_file = self.root / f'{self.name}.port'
        if port_file.exists():
            port_file.unlink()
        command = [sys.executable, '-m', 'keelsplice',
                   '--repository', str(self.repository),
                   '--source', f'microscope={self.source}',
                   '--port', '0', '--role', self.role]
        if self.manifest is not None:
            command += ['--switchover', str(self.manifest)]
        env = os.environ | {'KEELSPLICE_PORT_FILE': str(port_file)}
        with self.log.open('wb') as log:
            self.process = subprocess.Popen(
                command, cwd=Path(__file__).resolve().parent.parent,
                stdout=log, stderr=subprocess.STDOUT, env=env,
            )
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if port_file.exists() and port_file.stat().st_size:
                port = int(port_file.read_text().strip())
                self.base = f'http://127.0.0.1:{port}'
                self._await_ready()
                return self
            if self.process.poll() is not None:
                break
            time.sleep(0.02)
        output = self.log.read_text(errors='replace') if self.log.exists() else ''
        self.stop()
        if expected:
            raise AssertionError(f'{self.name} did not start:\n{output}')
        self.failure = output
        return None

    def _await_ready(self):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                self.json('GET', '/instance')
                return
            except OSError:
                time.sleep(0.02)
        raise AssertionError(f'{self.name} never answered on {self.base}')

    def stop(self):
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        self.process = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.stop()

    def json(self, method, route, body=None, status=200):
        headers = {'Content-Type': 'application/json'}
        data = json.dumps(body).encode('utf-8') if body is not None else None
        try:
            with urlopen(Request(self.base + route, data=data, headers=headers,
                                 method=method), timeout=5) as response:
                assert response.status == status, (route, response.status, response.read()[:400])
                return json.loads(response.read())
        except HTTPError as exc:
            payload = exc.read()
            assert exc.code == status, (route, exc.code, payload[:400])
            return json.loads(payload)

    def page(self, path, payload=None, status=200):
        data = None if payload is None else urlencode(payload, doseq=True).encode('ascii')
        headers = {} if data is None else {'Origin': self.base}
        try:
            with urlopen(Request(self.base + path, data=data, headers=headers), timeout=5) as response:
                assert response.status == status, (path, response.status, response.read()[:400])
                return response.headers, response.read().decode('utf-8')
        except HTTPError as exc:
            payload = exc.read()
            assert exc.code == status, (path, exc.code, payload[:400])
            return exc.headers, payload.decode('utf-8')


def now():
    return datetime.now(timezone.utc)


def recipe_and_certified_capture(primary):
    catalog = primary.json('GET', '/sources/microscope/catalog')
    edge = next(edge for edge in catalog['schema']['edges']
                if edge['child'] == 'assay' and edge['parent'] == 'substrate')
    body = {'source': 'microscope', 'roots': [{'table': 'substrate', 'key': ['east', 'MICA']}],
            'follow_children': [{'child': 'assay', 'id': edge['id']}], 'row_limit': 20}
    primary.json('POST', '/recipes/drill/versions', body, 201)
    capture = primary.json('POST', '/captures', {'name': 'drill', 'version': 1}, 201)
    primary.json('POST', f'/captures/{capture["id"]}/build', {})
    certified = primary.json('POST', f'/captures/{capture["id"]}/certify', {})
    assert certified['state'] == 'certified' and certified['certificate']['sha256']
    return body, capture, certified['certificate']


def _repository(path, source, *, role='primary'):
    return Repository(path, {'microscope': source}, role=role)


def sealed_snapshot(primary, body):
    """Seal one more capture after the standby snapshot was taken."""
    capture = primary.json('POST', '/captures', {'name': 'drill', 'version': 1}, 201)
    assert capture['state'] == 'sealed'
    return capture


def run_report(manifest_path, bases):
    command = [sys.executable, '-m', 'keelsplice.switchover', 'report',
               '--manifest', str(manifest_path)]
    for base in bases:
        command += ['--instance', base]
    completed = subprocess.run(command, cwd=Path(__file__).resolve().parent.parent,
                               capture_output=True, text=True, timeout=30)
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


def run(root):
    source = root / 'microscope.sqlite'
    seed(source)
    primary_path = root / 'primary-repo' / 'state.sqlite'
    standby_path = root / 'standby-repo' / 'state.sqlite'
    primary_path.parent.mkdir()
    standby_path.parent.mkdir()

    # 1. Primary alone: establish a recipe and one previously certified fixture.
    primary = Instance(root, 'primary-1', primary_path, source).start()
    try:
        body, certified_capture, certificate = recipe_and_certified_capture(primary)
        metadata = primary.json('GET', '/instance')
        assert metadata['role'] == 'primary' and metadata['authoritative'] is True
        assert metadata['content_signature']
    finally:
        primary.stop()

    # 2. Snapshot the primary repository while it is stopped, stamped with provenance.
    primary_repository = _repository(primary_path, source)
    export = switchover.export_replica(primary_repository, standby_path,
                                       drill_id='drill-1', snapshot_at=now() - timedelta(seconds=5))
    assert export['replica']['source_content_signature'] == metadata['content_signature']

    # 3. Two processes: primary keeps sealing captures; standby only re-verifies.
    primary = Instance(root, 'primary-2', primary_path, source).start()
    standby = Instance(root, 'standby', standby_path, source, role='standby').start()
    try:
        standby_meta = standby.json('GET', '/instance')
        assert standby_meta['role'] == 'standby' and standby_meta['authoritative'] is False
        assert standby_meta['replica']['source_content_signature'] == metadata['content_signature']

        # The standby shows the snapshot's activity with explicit provenance.
        _, recipes_page = standby.page('/workbench/recipes/drill/1')
        assert '备用实例视图' in recipes_page and '快照' in recipes_page
        standby_recipe = standby.json('GET', '/recipes/drill/versions/1')
        assert standby_recipe['active'] is True  # only as of the snapshot

        # Standby re-verifies the certified fixture without writing any state.
        standby_before = standby.json('GET', '/instance')['content_signature']
        reverified = standby.json('POST', f'/captures/{certified_capture["id"]}/reverify', {})
        assert reverified['verified'] is True
        assert reverified['sha256'] == certificate['sha256']
        assert standby.json('GET', '/instance')['content_signature'] == standby_before
        _, reverify_page = standby.page(f'/workbench/captures/{certified_capture["id"]}/reverify', {})
        assert certificate['sha256'] in reverify_page and '重新计算摘要' in reverify_page

        # Standby refuses every write path, both JSON and browser.
        assert standby.json('POST', '/captures', {'name': 'drill', 'version': 1}, 409)['error'] == 'standby_read_only'
        assert standby.json('POST', '/preview', {'name': 'drill', 'version': 1}, 409)['error'] == 'standby_read_only'
        assert standby.json('POST', '/recipes/drill/versions', body, 409)['error'] == 'standby_read_only'
        assert standby.json('POST', '/recipes/drill/versions/1/retire', {}, 409)['error'] == 'standby_read_only'
        assert standby.json('POST', f'/captures/{certified_capture["id"]}/build', {}, 409)['error'] == 'standby_read_only'
        assert standby.json('POST', f'/captures/{certified_capture["id"]}/certify', {}, 409)['error'] == 'standby_read_only'
        assert standby.json('GET', f'/captures/{certified_capture["id"]}/database', status=409)['error'] == 'standby_read_only'
        _, refusal = standby.page('/workbench/recipes/new', status=409)
        assert '备用实例' in refusal
        _, download_refusal = standby.page(f'/workbench/captures/{certified_capture["id"]}/database', status=409)
        assert '备用实例' in download_refusal
        _, sealed_refusal = standby.page('/workbench/captures/' + '0' * 32 + '/build', {}, status=409)
        assert '备用实例' in sealed_refusal
        _, capture_refusal = standby.page('/workbench/recipes/drill/1/capture', {}, status=409)
        assert '备用实例' in capture_refusal

        # The primary is still sealing relational captures while the standby runs.
        appended = sealed_snapshot(primary, body)
        assert primary.json('GET', '/instance')['content_signature'] != metadata['content_signature']
        assert primary.json('GET', '/captures/' + certified_capture['id'])['state'] == 'certified'

        # The standby never absorbs the new capture or re-labels old activity as live.
        _, standby_captures = standby.page('/workbench/captures')
        assert appended['id'] not in standby_captures and '备用实例视图' in standby_captures
        assert len(standby.json('GET', '/instance')['captures']) == 1
        assert len(primary.json('GET', '/instance')['captures']) == 2
        _, standby_recipe_page = standby.page('/workbench/recipes/drill/1')
        assert '快照时投用' in standby_recipe_page and '投用中' not in standby_recipe_page

        # Old artifact stays deliverable before and after promotion.
        data, etag = download(primary, certified_capture['id'])
        assert hashlib.sha256(data).hexdigest() == etag == certificate['sha256']
        digest_before_switch = hashlib.sha256(data).hexdigest()

        # 4. Prepare the handover manifest, persist it, and restart the primary
        #    between two processes using that manifest.
        standby_repository = _repository(standby_path, source, role='standby')
        manifest = switchover.plan(primary_repository, standby_repository, export,
                                   drill_id='drill-1', rollback_minutes=30)
        manifest_path = switchover.write_manifest(root / 'switchover.json', manifest)

        # Operational rollback report, through the real CLI and both live instances.
        report = run_report(manifest_path, [primary.base, standby.base])
        assert report['instances'][0]['added_count'] == 1, report
        assert report['instances'][1]['added_count'] == 0, report
        assert appended['id'] in report['instances'][0]['added_captures']
    finally:
        primary.stop()
        standby.stop()

    primary_repository = _repository(primary_path, source)

    # The standby's store location must match the manifest before the primary writes.
    primary = Instance(root, 'primary-3', primary_path, source, manifest=manifest_path).start()
    try:
        switched = primary.json('GET', '/instance')
        assert switched['role'] == 'primary'
        assert switched['switchover']['drill_id'] == 'drill-1'
        # Continuation writes check the persistent record location first.
        assert switched['content_signature'] == primary_repository.content_signature()
        continued = primary.json('POST', '/captures', {'name': 'drill', 'version': 1}, 201)
        assert continued['state'] == 'sealed'
        after = primary.json('GET', '/instance')
        assert after['content_signature'] != manifest['content_signature']
        data, etag = download(primary, certified_capture['id'])
        assert hashlib.sha256(data).hexdigest() == etag == certificate['sha256']
        assert hashlib.sha256(data).hexdigest() == digest_before_switch == certificate['sha256']
        delivered = root / 'delivered.sqlite'
        delivered.write_bytes(data)
        with closing(sqlite3.connect(delivered)) as conn:
            assert conn.execute('PRAGMA foreign_key_check').fetchall() == []
            assert conn.execute("SELECT length_um FROM assay WHERE mark='S-01'").fetchone() == (3.25,)
    finally:
        primary.stop()

    # 5. Failure paths: stale records, a misplaced persistent store, and a closed window.
    refusal = Instance(root, 'primary-stale', primary_path, source, manifest=manifest_path).start(expected=False)
    assert refusal is None
    stale_output = (root / 'primary-stale.log').read_text(errors='replace')
    assert 'handover_content' in stale_output

    relocated = dict(manifest)
    relocated['content_signature'] = primary_repository.content_signature()
    relocated_path = switchover.write_manifest(root / 'relocated.json', relocated)
    other_path = root / 'other-repo' / 'state.sqlite'
    other_path.parent.mkdir()
    with closing(sqlite3.connect(primary_path)) as origin, closing(sqlite3.connect(other_path)) as copy:
        origin.backup(copy)
    refusal = Instance(root, 'primary-relocated', other_path, source,
                       manifest=relocated_path).start(expected=False)
    assert refusal is None
    assert 'handover_location' in (root / 'primary-relocated.log').read_text(errors='replace')
    other_path.unlink()

    closed = dict(manifest)
    closed['rollback_window'] = {
        'opens_at': (now() - timedelta(hours=2)).isoformat(),
        'closes_at': (now() - timedelta(hours=1)).isoformat(),
        'minutes': 60,
    }
    # The content signature must still match, so re-sign the manifest after the
    # drill writes, then close the window.
    closed['content_signature'] = primary_repository.content_signature()
    stat = os.stat(primary_path)
    closed['device'], closed['inode'] = stat.st_dev, stat.st_ino
    closed_path = switchover.write_manifest(root / 'closed.json', closed)
    refusal = Instance(root, 'primary-closed', primary_path, source, manifest=closed_path).start(expected=False)
    assert refusal is None
    assert 'handover_window' in (root / 'primary-closed.log').read_text(errors='replace')

    # 6. Rollback report names which instance gained captures after the snapshot.
    primary_after = primary_repository.instance_info()
    standby_after = _repository(standby_path, source, role='standby').instance_info()
    report = switchover.rollback_report(manifest, [primary_after, standby_after])
    assert report['added_total'] >= 1, report
    assert report['instances'][1]['added_count'] == 0
    assert report['instances'][1]['authoritative'] is False
    assert 'primary' in report['summary']

    print('切换演练通过：主实例持续封存，备用实例只读复核已认证子库，'
          '清单核对持久位置与摘要，旧成果切换前后摘要一致，失败回退可归因新增捕获')


def download(instance, capture_id):
    with urlopen(instance.base + f'/captures/{capture_id}/database', timeout=5) as response:
        return response.read(), response.headers['ETag'].strip('"')


def main():
    parser = argparse.ArgumentParser(description='两进程切换演练。')
    parser.parse_args()
    with TemporaryDirectory(prefix='keelsplice-switchover-') as directory:
        run(Path(directory))


if __name__ == '__main__':
    main()

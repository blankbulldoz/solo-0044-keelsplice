"""Build a re-runnable WAL point-in-time sampling archive for offsite retention.

The scenario stages one uncommitted write and two committed updates against the
microscope source, and seals an independent workbench capture at each boundary.
It writes the smallest source-state material a successor needs:

* one shared main database file (commits live in the WAL, so these bytes never
  change);
* the WAL segment beside each capture;
* the capture record the workbench actually sealed;
* for the pending point, the writer's own snapshot, which is the only faithful
  view of pages SQLite still holds in the page cache.

``manifest.json`` records what should be visible at each point, so the bundled
``verify.py`` can decide every claim from bytes and typed values rather than
from timestamps or from an equal schema digest.

Run::

    python3 -m examples.retention_archive archive-directory

Then, from anywhere::

    python3 archive-directory/verify.py archive-directory
"""

import argparse
import hashlib
import json
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from threading import Thread
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from keelsplice import retention_verify
from keelsplice.api import create_server
from keelsplice.codec import fingerprint
from keelsplice.wal import parse as parse_wal

from .walkthrough import seed

FORMAT = 'keelsplice-retention/1'
RECIPE_NAME = 'retention-swatch'
SOURCE_ALIAS = 'microscope'
POLL_SECONDS = 0.01
READY_TIMEOUT = 60

# One statement touches both measured rows, so the assay root page is dirty.
# A blob update then pushes pages out of the tiny cache, which leaves valid
# but uncommitted frames in the WAL for the pending point to archive.
STAGED = [
    "UPDATE assay SET "
    "length_um = CASE mark WHEN 'S-01' THEN 12.0 ELSE 6.0 END, "
    "region = CASE mark WHEN 'S-02' THEN 'West' ELSE region END, "
    "code = CASE mark WHEN 'S-02' THEN 'Basalt' ELSE code END "
    "WHERE mark IN ('S-01', 'S-02')",
    "UPDATE substrate SET texture = zeroblob(30000) "
    "WHERE region = 'East' AND code = 'Mica'",
]
COMMIT_ONE = "UPDATE assay SET length_um = 4.75 WHERE mark = 'S-01'"
COMMIT_TWO = "UPDATE assay SET region = 'west', code = 'basalt', length_um = 8.5 WHERE mark = 'S-02'"


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def real(value):
    return {'real': float(value).hex()}


def probe_values(length, doubled, region=None, code=None, parent=None):
    columns = {}
    if region is not None:
        columns['region'] = region
        columns['code'] = code
    columns['length_um'] = real(length)
    columns['doubled'] = real(doubled)
    return {'columns': columns, 'parent': parent}


def scenario_claims(edge_id):
    """The scenario's claims about what each capture and writer image must show."""
    east = ['substrate', ['East', 'Mica']]
    west = ['substrate', ['West', 'Basalt']]
    baseline = {
        'assay-S-01': probe_values(3.25, 6.5, parent=east),
        'assay-S-02': probe_values(5.5, 11.0, region='EAST', code='mica', parent=east),
    }
    after_one = {
        'assay-S-01': probe_values(4.75, 9.5, parent=east),
        'assay-S-02': probe_values(5.5, 11.0, region='EAST', code='mica', parent=east),
    }
    after_two = {
        'assay-S-01': probe_values(4.75, 9.5, parent=east),
        'assay-S-02': probe_values(8.5, 17.0, region='west', code='basalt', parent=west),
    }
    staged = {
        'assay-S-01': probe_values(12.0, 24.0, parent=east),
        'assay-S-02': probe_values(6.0, 12.0, region='West', code='Basalt', parent=west),
    }
    probes = [
        {'id': 'assay-S-01', 'table': 'assay', 'key': ['S-01'],
         'columns': ['length_um', 'doubled'],
         'parent_edge': {'child': 'assay', 'id': edge_id}},
        {'id': 'assay-S-02', 'table': 'assay', 'key': ['S-02'],
         'columns': ['region', 'code', 'length_um', 'doubled'],
         'parent_edge': {'child': 'assay', 'id': edge_id}},
    ]
    return {'baseline': baseline, 'after_one': after_one, 'after_two': after_two,
            'staged': staged, 'probes': probes}


def call(base, route, body=None, status=200):
    headers = {'Content-Type': 'application/json'}
    payload = None if body is None else json.dumps(body).encode('utf-8')
    try:
        response = urlopen(Request(base + route, payload, headers), timeout=10)
    except HTTPError as exc:
        response = exc
    with response:
        data = response.read()
        if response.status != status:
            raise AssertionError((route, response.status, data[:400]))
        return json.loads(data)


def captured_record(repository, capture_id):
    connection = sqlite3.connect(repository)
    try:
        row = connection.execute(
            'SELECT id, name, version, state, sealed FROM captures WHERE id=?', (capture_id,)
        ).fetchone()
    finally:
        connection.close()
    if row is None:
        raise AssertionError(f'capture {capture_id} is missing from the repository')
    return {'id': row[0], 'name': row[1], 'version': row[2], 'state': row[3],
            'sealed': json.loads(row[4])}


def wal_snapshot(source):
    path = Path(str(source) + '-wal')
    return path.read_bytes() if path.exists() else b''


def trim_segment(segment):
    """Drop frames past the last commit; a committed point needs nothing after it.

    Points that hold only writer material (no commit frame) keep every valid
    frame, because that tail is the evidence that the writer never committed.
    """
    info = parse_wal(segment)
    if info['state'] != 'wal':
        return segment
    if info['last_commit'] is None:
        limit = info['valid_frames']
    else:
        limit = info['last_commit'] + 1
    return segment[:32 + limit * info['frame_size']]


def wait_for(path, timeout=READY_TIMEOUT):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if Path(path).exists():
            return True
        time.sleep(POLL_SECONDS)
    return False


def open_writer(root, source, statements, workspace, tag, mode='pending', image=None):
    ready = workspace / f'{tag}.ready'
    release = workspace / f'{tag}.release'
    command = [sys.executable, '-m', 'keelsplice.retention_writer',
               '--source', str(source), '--mode', mode,
               '--statements', json.dumps(statements),
               '--ready', str(ready), '--release', str(release)]
    if image is not None:
        command += ['--image', str(image)]
    process = subprocess.Popen(command, cwd=str(root))
    if not wait_for(ready):
        process.kill()
        process.wait()
        raise AssertionError(f'retention writer {tag} did not announce readiness')
    return process, release


def close_writer(process, release, tag):
    release.write_text('release', encoding='ascii')
    code = process.wait(timeout=60)
    if code != 0:
        raise AssertionError(f'retention writer {tag} exited with {code}')


def record_point(destination, point_id, wal_bytes, capture, extra=None):
    directory = destination / 'points' / point_id
    directory.mkdir(parents=True)
    wal_file = directory / 'wal.bin'
    wal_file.write_bytes(wal_bytes)
    capture_file = directory / 'capture.json'
    capture_file.write_bytes(retention_verify.dumps(capture).encode('ascii'))
    info = parse_wal(wal_bytes)
    point = {
        'id': point_id,
        'main': {'file': 'source/main.sqlite', 'sha256': None},
        'wal': {'file': f'points/{point_id}/wal.bin', 'sha256': sha256(wal_bytes),
                'state': info['state'], 'valid_frames': info['valid_frames'],
                'frame_count': info['valid_frames'],
                'commit_frames': info['commit_frames'],
                'last_commit': info['last_commit'],
                'has_uncommitted_tail': info['has_uncommitted_tail'],
                'salt': info['salt']},
        'capture': {'file': f'points/{point_id}/capture.json',
                    'sha256': sha256(capture_file.read_bytes()),
                    'sealed_digest': fingerprint(capture['sealed']),
                    'id': capture['id']},
        'schema_digest': capture['sealed']['schema_digest'],
    }
    point.update(extra or {})
    return point


def build(destination):
    destination = Path(destination).resolve()
    if destination.exists() and any(destination.iterdir()):
        raise SystemExit(f'Destination {destination} is not empty')
    destination.mkdir(parents=True, exist_ok=True)
    root = Path(__file__).resolve().parent.parent

    with tempfile.TemporaryDirectory(prefix='keelsplice-retention-') as directory:
        workspace = Path(directory)
        source = workspace / 'source.sqlite'
        seed(source)

        setup = sqlite3.connect(source, isolation_level=None)
        setup.execute('PRAGMA journal_mode=WAL')
        setup.execute('PRAGMA wal_autocheckpoint=0')
        setup.execute('PRAGMA synchronous=FULL')
        setup.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        setup.close()

        main_bytes = source.read_bytes()
        main_sha = sha256(main_bytes)
        repository = workspace / 'state.sqlite'
        points = {}
        writer_image = None

        # A pinned read snapshot prevents checkpoints, so every commit stays in
        # the WAL segment that the next capture archives.
        guard = sqlite3.connect(source.as_uri() + '?mode=ro', uri=True)
        guard.execute('BEGIN')
        guard.execute('SELECT count(*) FROM assay').fetchone()
        try:
            with create_server(('127.0.0.1', 0), repository, {SOURCE_ALIAS: source}) as server:
                thread = Thread(target=server.serve_forever, kwargs={'poll_interval': 0.05})
                thread.start()
                base = f'http://127.0.0.1:{server.server_port}'
                try:
                    catalog = call(base, f'/sources/{SOURCE_ALIAS}/catalog')
                    edge = next(item for item in catalog['schema']['edges']
                                if item['child'] == 'assay' and item['parent'] == 'substrate')
                    claims = scenario_claims(edge['id'])
                    recipe_body = {
                        'source': SOURCE_ALIAS,
                        'roots': [{'table': 'substrate', 'key': ['east', 'MICA']},
                                  {'table': 'substrate', 'key': ['West', 'Basalt']}],
                        'follow_children': [{'child': 'assay', 'id': edge['id']}],
                        'row_limit': 50,
                    }
                    registered = call(base, f'/recipes/{RECIPE_NAME}/versions', recipe_body, 201)
                    lookup = {'name': RECIPE_NAME, 'version': registered['version']}

                    def capture_point(point_id, extra=None):
                        sealed = call(base, '/captures', lookup, 201)
                        record = captured_record(repository, sealed['id'])
                        segment = trim_segment(wal_snapshot(source))
                        points[point_id] = record_point(
                            destination, point_id, segment, record, extra)
                        return points[point_id]

                    capture_point('P0')  # baseline; the WAL has no frame yet

                    image_path = workspace / 'writer.sqlite'
                    process, release = open_writer(root, source, STAGED, workspace, 'pending',
                                                   mode='pending', image=image_path)
                    writer_image = image_path.read_bytes()
                    capture_point('P1', {
                        'writer_image': {'file': 'points/P1/writer.sqlite',
                                         'sha256': sha256(writer_image)},
                        'writer_visible': claims['staged'],
                    })
                    close_writer(process, release, 'pending')

                    process, release = open_writer(root, source, [COMMIT_ONE], workspace,
                                                   'commit-one', mode='commit')
                    close_writer(process, release, 'commit-one')
                    capture_point('P2')

                    process, release = open_writer(root, source, [COMMIT_TWO], workspace,
                                                   'commit-two', mode='commit')
                    close_writer(process, release, 'commit-two')
                    capture_point('P3')
                finally:
                    server.shutdown()
                    thread.join()
        finally:
            guard.close()

        if source.read_bytes() != main_bytes:
            raise AssertionError('main database bytes changed; commits did not stay in the WAL')

        # The scenario is only meaningful if the bytes show what it claims.
        p0, p1 = points['P0']['wal'], points['P1']['wal']
        p2_wal, p3_wal = points['P2']['wal'], points['P3']['wal']
        if p0['state'] != 'empty':
            raise AssertionError('baseline point must predate every WAL frame')
        if p1['last_commit'] is not None or not p1['has_uncommitted_tail']:
            raise AssertionError('pending point needs valid frames and no commit frame')
        if not (p2_wal['last_commit'] is not None and p2_wal['last_commit'] == 0):
            raise AssertionError('first committed point needs exactly its first commit')
        if not (p3_wal['last_commit'] is not None and p3_wal['last_commit'] > p2_wal['last_commit']):
            raise AssertionError('second committed point needs a later commit frame')
        if p2_wal['has_uncommitted_tail'] or p3_wal['has_uncommitted_tail']:
            raise AssertionError('committed points must not carry a writer-only tail')

        (destination / 'source').mkdir(exist_ok=True)
        (destination / 'source' / 'main.sqlite').write_bytes(main_bytes)
        (destination / 'points' / 'P1' / 'writer.sqlite').write_bytes(writer_image)
        (destination / 'verify.py').write_bytes(
            (root / 'keelsplice' / 'retention_verify.py').read_bytes())

        expected = {'P0': claims['baseline'], 'P1': claims['baseline'],
                    'P2': claims['after_one'], 'P3': claims['after_two']}
        for point_id, point in points.items():
            point['main'] = {'file': 'source/main.sqlite', 'sha256': main_sha}
            point['visible'] = expected[point_id]

        pending_columns = {probe_id: values['columns']
                           for probe_id, values in claims['staged'].items()}

        p2, p3 = points['P2'], points['P3']
        if retention_verify.wal_prefix_matches(
                (destination / p2['wal']['file']).read_bytes(),
                (destination / p3['wal']['file']).read_bytes()):
            p3['extends'] = 'P2'

        manifest = {
            'format': FORMAT,
            'scenario': {
                'source_alias': SOURCE_ALIAS,
                'recipe': {'name': RECIPE_NAME, 'version': registered['version'],
                           'schema_digest': registered['schema_digest'],
                           'roots': registered['recipe']['roots'],
                           'follow_children': registered['recipe']['follow_children']},
                'staged_uncommitted': STAGED,
                'committed_updates': [COMMIT_ONE, COMMIT_TWO],
                'note': 'One uncommitted write, two committed updates, and an independent '
                        'workbench capture at each boundary.',
            },
            'evidence_policy': {
                'decided_by': 'main database bytes, WAL frame checksums and commit ordinals, '
                              'and typed SQLite values in the sealed capture and the replay',
                'not_evidence': [
                    'filesystem or wall-clock timestamps',
                    'an equal schema digest on its own',
                    'the presence of a WAL file without a commit frame',
                    'a writer snapshot as a reader view',
                ],
                'pending_rule': 'A reader image stops at the last commit frame. A valid frame '
                                'after it is writer material and never explains a capture.',
            },
            'probes': claims['probes'],
            'points': [points[key] for key in ('P0', 'P1', 'P2', 'P3')],
            'relations': [
                {'left': 'P0', 'right': 'P1', 'relation': 'same_visible',
                 'why': 'The pending writer staged S-01 and S-02, yet the capture read the '
                        'last committed image, so the sealed rows and witnesses are identical.'},
                {'left': 'P1', 'right': 'P2', 'relation': 'different_visible',
                 'why': 'The first background commit changed S-01 length_um and its generated '
                        'doubled value; the next capture read the committed image.'},
                {'left': 'P2', 'right': 'P3', 'relation': 'different_visible',
                 'why': 'The second background commit moved S-02 to another substrate parent; '
                        'the next capture followed that parent key target.'},
                {'left': 'P0', 'right': 'P3', 'relation': 'different_visible',
                 'why': 'Two commits separate the baseline and final captures even though '
                        'every point shares one schema digest.'},
            ],
            'pending_absent': pending_columns,
            'main': {'file': 'source/main.sqlite', 'sha256': main_sha},
            'generated': {
                'source': 'Python ' + sys.version.split()[0] + ', SQLite ' + sqlite3.sqlite_version,
                'command': 'python3 -m examples.retention_archive <destination>',
            },
        }

    (destination / 'manifest.json').write_bytes(retention_verify.dumps(manifest).encode('ascii'))
    (destination / 'README.md').write_text(render_readme(manifest), encoding='utf-8')

    report = retention_verify.verify_archive(destination)
    return {'destination': str(destination), 'checks': len(report['checks']),
            'points': report['points'], 'schema_digests': report['schema_digests']}


def render_readme(manifest):
    rows = []
    for point in manifest['points']:
        wal = point['wal']
        rows.append(f"| {point['id']} | {wal['state']} | {wal['valid_frames']} | "
                    f"{wal['last_commit']} | {wal['has_uncommitted_tail']} |")
    return f'''# Keelsplice WAL 时间点取样归档

本目录可在异地用标准库复跑，不需要 Keelsplice 源码，也不读取源库原文件。

```sh
python3 verify.py .
```

## 证据点

| 点 | WAL 状态 | 有效帧 | 最后提交帧 | 未提交尾部 |
| --- | --- | --- | --- | --- |
{chr(10).join(rows)}

每个点包含同一份主库字节 `source/main.sqlite`、该点的 WAL 段
`points/<点>/wal.bin`、当时封存的捕获记录 `points/<点>/capture.json`。
主库字节在所有点之间不变，已提交更新只存在于 WAL 中。

- `P0`：基线，WAL 尚无有效帧。
- `P1`：后台写入方保持未提交事务，工作台另开读事务封存。WAL 有效帧没有提交帧，
  读到的仍是最后一个已提交镜像；`points/P1/writer.sqlite` 是写入方自己的快照，
  只用于说明未提交值，不能当作读者镜像。
- `P2`：第一个后台更新提交后封存。
- `P3`：第二个后台更新提交后封存。

## 判据

`verify.py` 只按字节和带类型的 SQLite 值定案：

- WAL 头与每帧的 salt、累计校验和决定有效帧；解析在第一个无效帧停止。
- 读者镜像止于最后一个提交帧；其后的合法帧属于写入方，不进入任何捕获。
- 捕获中的每个入选行都必须与重放镜像在实存主键上逐列相等。
- 生成列由 SQLite 计算，归档核对 `doubled = length_um * 2`。
- 父键目标按声明外键和 SQLite 比较语义重新查找，包含 NOCASE 复合键。
- 同模式捕获按封存内容摘要比较：`P0`/`P1` 相同，`P1`/`P2`、`P2`/`P3` 不同。
- `P2` 的 WAL 有效帧必须是 `P3` 的前缀，最后提交帧序号只增不减。

时间戳不参与判定；相同模式摘要只确认结构，行内容另由可见值和封存内容摘要判定。

## 场景

- 未提交写入：
{chr(10).join('  - `' + statement + '`' for statement in manifest['scenario']['staged_uncommitted'])}
- 已提交更新：
{chr(10).join('  - `' + statement + '`' for statement in manifest['scenario']['committed_updates'])}
- 配方：`{manifest['scenario']['recipe']['name']}` 版本 {manifest['scenario']['recipe']['version']}

重建归档（需要 Keelsplice 源码，目标须为空目录）：

```sh
python3 -m examples.retention_archive <新的空目录>
```
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('destination', help='Empty directory to receive the archive')
    args = parser.parse_args()
    result = build(args.destination)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == '__main__':
    main()

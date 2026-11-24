"""Prepare and replay the microscope handoff batch for the two specialist crews.

``prepare`` drives the real JSON HTTP service, seals/builds/certifies one
capture per source state, writes the certified SQLite bytes to ``artifacts/``
and the comparison basis to ``control.json``.  ``verify`` re-reads the
delivered bytes through independent SQLite connections, recomputes every
classification, and rebuilds the whole batch in a temporary directory to prove
that ``control.json`` is reproducible.

Every state starts again from a freshly seeded copy of ``examples/microscope.sql``,
so the recorded mutation is the complete difference from the pristine sample.
"""

import argparse
import hashlib
import json
import shutil
import sqlite3
import sys
import time
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from examples.walkthrough import seed
from keelsplice.api import create_server

ROOT = Path(__file__).resolve().parent
OUT = ROOT / 'artifacts'
CONTROL = ROOT / 'control.json'
RECIPE = 'handoff'
ROOT_KEY = ['east', 'MICA']
ROW_LIMIT = 20
EDGE = {'child': 'assay', 'parent': 'substrate'}
KEYS = {
    'substrate': ('region', 'code'),
    'assay': ('mark',),
    'cassette': ('serial',),
}
MEASUREMENT = {'length_um', 'doubled'}
RELATION = {'region', 'code', 'cassette'}
STATE_ORDER = ['baseline', 'numeric-only', 'parent-only', 'both', 'frame-only', 'case-key']
STATES = {
    'baseline': {
        'mutations': [],
        'note': '原始显微镜样例；两班共用的对照。',
    },
    'numeric-only': {
        'mutations': ["UPDATE assay SET length_um=4.75 WHERE mark='S-01'"],
        'note': '只改测量基值及其 STORED 生成值；substrate 行逐字节不变，外键列不变。',
    },
    'parent-only': {
        'mutations': ["UPDATE substrate SET texture=x'1020' WHERE region='East' AND code='Mica'"],
        'note': '只改父对象 substrate 的值；assay 每行逐字节不变，长度与生成值不变。',
    },
    'both': {
        'mutations': [
            "UPDATE assay SET length_um=4.75 WHERE mark='S-01'",
            "UPDATE substrate SET texture=x'1020' WHERE region='East' AND code='Mica'",
        ],
        'note': '两种变化同时发生；相对 baseline 的比较同时列出 assay 与 substrate 的行修改。',
    },
    'frame-only': {
        'mutations': ["UPDATE frame SET note='changed' WHERE frame_id=104"],
        'note': '只改闭包外的 frame 行；产物与 baseline 逐字节相同。',
    },
    'case-key': {
        'mutations': ["UPDATE substrate SET region='EAST', code='MICA' WHERE region='East' AND code='Mica'"],
        'note': '父键仅改大小写；NOCASE 下仍连接同一对象，但带类型主键的拼写改变。',
    },
}
CERTIFIED = STATE_ORDER


def call(base, route, body=None, status=200):
    data = None if body is None else json.dumps(body).encode('utf-8')
    headers = {'Content-Type': 'application/json'} if data is not None else {}
    try:
        response = urlopen(Request(base + route, data, headers), timeout=5)
    except HTTPError as exc:
        response = exc
    with response:
        raw = response.read()
        assert response.status == status, (route, response.status, raw[:400])
        if response.headers.get_content_type() == 'application/vnd.sqlite3':
            return raw, response.headers['ETag'].strip('"')
        return json.loads(raw)


def reset(source, pristine):
    shutil.copyfile(pristine, source)


def mutate(source, statements):
    with closing(sqlite3.connect(source)) as conn, conn:
        for sql in statements:
            conn.execute(sql)


def facts(file):
    """Read delivered bytes only through an independent read-only connection."""
    with closing(sqlite3.connect(file.as_uri() + '?mode=ro', uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        columns = [dict(row) for row in conn.execute('PRAGMA main.table_xinfo(assay)')]
        schemas = [dict(row) for row in conn.execute('PRAGMA main.table_list')]
        substrate = [dict(row) for row in conn.execute(
            'SELECT region, code, hex(texture) AS texture_hex FROM substrate ORDER BY region, code')]
        assay = [dict(row) for row in conn.execute(
            'SELECT mark, region, code, cassette, length_um, doubled FROM assay ORDER BY mark')]
        cassette = [dict(row) for row in conn.execute(
            'SELECT serial, prior, label FROM cassette ORDER BY serial')]
        return {
            'integrity_check': [row[0] for row in conn.execute('PRAGMA main.integrity_check')],
            'foreign_key_check': [list(row) for row in conn.execute('PRAGMA main.foreign_key_check')],
            'tables': [{'name': row['name'], 'without_rowid': bool(row['wr'])} for row in schemas
                       if row['schema'] == 'main' and not row['name'].startswith('sqlite_')],
            'assay_columns': [{'name': row['name'], 'hidden': row['hidden']} for row in columns],
            'generated': [row['name'] for row in columns if row['hidden'] != 0],
            'substrate': substrate,
            'assay': assay,
            'cassette': cassette,
            # The declared parent key is NOCASE; this join mirrors the
            # workbench's own parent lookup rather than a binary comparison.
            'joined_assay_rows': conn.execute(
                'SELECT count(*) FROM assay a JOIN substrate s '
                'ON s.region = a.region AND s.code = a.code').fetchone()[0],
        }


def diff(before, after, key):
    """Typed-key diff with the concrete columns that changed per row."""
    def index(rows):
        return {tuple(row[name] for name in key): row for row in rows}

    left, right = index(before), index(after)
    modified = []
    for identity in sorted(left.keys() & right.keys()):
        columns = sorted(name for name in left[identity]
                         if left[identity][name] != right[identity][name])
        if columns:
            modified.append({'key': list(identity), 'columns': columns})
    return {
        'added': [list(identity) for identity in sorted(right.keys() - left.keys())],
        'removed': [list(identity) for identity in sorted(left.keys() - right.keys())],
        'modified': modified,
    }


def classify(before, after):
    assay = diff(before['assay'], after['assay'], KEYS['assay'])
    substrate = diff(before['substrate'], after['substrate'], KEYS['substrate'])
    cassette = diff(before['cassette'], after['cassette'], KEYS['cassette'])
    assay_changed = {name for row in assay['modified'] for name in row['columns']}
    measurement = bool(assay_changed & MEASUREMENT)
    relation = bool(assay_changed & RELATION) or bool(assay['added'] or assay['removed'])
    relation = relation or bool(cassette['added'] or cassette['removed'] or cassette['modified'])
    parent_values = bool(substrate['modified'])
    parent_identity = bool(substrate['added'] or substrate['removed'])
    parent = parent_values or parent_identity
    return {
        'assay': assay,
        'substrate': substrate,
        'cassette': cassette,
        'measurement_changed': measurement,
        'relation_changed': relation,
        'parent_values_changed': parent_values,
        'parent_identity_changed': parent_identity,
        'both_changes': measurement and parent,
        'numeric_crew_admissible': measurement and not parent and not relation,
        'relational_crew_admissible': parent_values and not parent_identity
                                      and not measurement and not relation,
    }


def pair_name(left, right):
    return f'{left}->{right}'


def build_control(destination, captures, compare_views, rejected, sealed_download,
                  baseline_stable, control_path):
    labels = list(CERTIFIED)
    pairs = {}
    for left in labels:
        for right in labels:
            if left == right:
                continue
            result = classify(captures[left]['facts'], captures[right]['facts'])
            result['compare_api'] = compare_views[pair_name(left, right)]
            pairs[pair_name(left, right)] = result
    dispatch = {
        'numeric': {
            'captures': ['baseline', 'numeric-only'],
            'pair': pair_name('baseline', 'numeric-only'),
            'why': 'baseline→numeric-only 只改 assay.length_um 与生成列 doubled；'
                   'substrate、cassette 以及 assay 的外键列逐字节不变。',
        },
        'relational': {
            'captures': ['baseline', 'parent-only'],
            'pair': pair_name('baseline', 'parent-only'),
            'why': 'baseline→parent-only 只改父对象 substrate.texture；'
                   'assay 每行（含 length_um 与 doubled）逐字节不变。',
        },
    }
    for crew, entry in dispatch.items():
        result = pairs[entry['pair']]
        assert result[f'{crew}_crew_admissible'], (crew, entry['pair'], result)

    # The 'both' capture exists so the crews can see why it is withheld: its
    # assay row modification is indistinguishable from the numeric pair's,
    # while the parent changes at the same time.
    numeric_view = pairs[pair_name('baseline', 'numeric-only')]['compare_api']
    both_view = pairs[pair_name('baseline', 'both')]['compare_api']
    same_row_modification = both_view['assay'] == numeric_view['assay']
    assert same_row_modification, (numeric_view['assay'], both_view['assay'])

    excluded = {
        'both': {
            'artifact': 'both.sqlite',
            'reason': '两种变化同时发生。baseline→both 的比较同时列出 assay S-01 与 substrate 的修改；'
                      'assay 的修改与数值班那一对完全相同，只看 S-01 的行修改无法判断父对象是否也变了。',
            'conflicting_pairs': [pair_name('baseline', 'both'),
                                  pair_name('numeric-only', 'parent-only')],
            'same_assay_row_modification_as_numeric_pair': same_row_modification,
        },
        'frame-only': {
            'artifact': 'frame-only.sqlite',
            'reason': '改动在闭包之外，产物与 baseline 逐字节相同；不能证明任一条件。',
            'conflicting_pairs': [pair_name('baseline', 'frame-only')],
        },
        'case-key': {
            'artifact': 'case-key.sqlite',
            'reason': '父键只改大小写。NOCASE 下仍连接同一父行，但带类型主键由 '
                      '["East","Mica"] 变为 ["EAST","MICA"]；比较报告新增/移除而非父对象修改，'
                      '无法作为稳定的父对象变化证据。',
            'conflicting_pairs': [pair_name('baseline', 'case-key'),
                                  pair_name('numeric-only', 'case-key'),
                                  pair_name('parent-only', 'case-key')],
        },
    }
    control = {
        'schema_version': 1,
        'sample': 'examples/microscope.sql',
        'seed': 'examples.walkthrough.seed',
        'root': {'table': 'substrate', 'key': ROOT_KEY},
        'follow_children': [EDGE],
        'row_limit': ROW_LIMIT,
        'generated_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        'runtime': {'python': sys.version.split()[0], 'sqlite': sqlite3.sqlite_version},
        'note': 'capture_id 与 generated_at 每次运行不同；其余字段用于复跑对照。',
        'captures': captures,
        'pairs': pairs,
        'dispatch': dispatch,
        'excluded': excluded,
        'rejected': rejected,
        'sealed_download': sealed_download,
        'baseline_stable_after_drift': baseline_stable,
    }
    control_path.write_text(json.dumps(control, ensure_ascii=False, indent=1, sort_keys=True) + '\n')
    return control


def prepare(destination=OUT, control_path=CONTROL):
    destination.mkdir(parents=True, exist_ok=True)
    for stale in destination.glob('*.sqlite'):
        stale.unlink()
    captures, compare_views = {}, {}
    with TemporaryDirectory(prefix='handoff-batch-') as directory:
        work = Path(directory)
        pristine = work / 'pristine.sqlite'
        source = work / 'source.sqlite'
        seed(pristine)
        reset(source, pristine)
        server = create_server(('127.0.0.1', 0), work / 'repo.sqlite', {'microscope': source})
        thread = Thread(target=server.serve_forever)
        thread.start()
        base = f'http://127.0.0.1:{server.server_port}'
        try:
            catalog = call(base, '/sources/microscope/catalog')
            edge = next(edge for edge in catalog['schema']['edges']
                        if edge['child'] == EDGE['child'] and edge['parent'] == EDGE['parent'])
            assert edge['parent_cols'] == ['region', 'code']
            body = {'source': 'microscope', 'roots': [{'table': 'substrate', 'key': ROOT_KEY}],
                    'follow_children': [{'child': EDGE['child'], 'id': edge['id']}],
                    'row_limit': ROW_LIMIT}
            version = call(base, f'/recipes/{RECIPE}/versions', body, 201)['version']
            lookup = {'name': RECIPE, 'version': version}
            for label in STATE_ORDER:
                reset(source, pristine)
                mutate(source, STATES[label]['mutations'])
                capture = call(base, '/captures', lookup, 201)
                call(base, f'/captures/{capture["id"]}/build', {})
                certificate = call(base, f'/captures/{capture["id"]}/certify', {})['certificate']
                data, etag = call(base, f'/captures/{capture["id"]}/database')
                assert hashlib.sha256(data).hexdigest() == etag == certificate['sha256']
                file = destination / f'{label}.sqlite'
                file.write_bytes(data)
                captures[label] = {
                    'label': label,
                    'note': STATES[label]['note'],
                    'mutations': STATES[label]['mutations'],
                    'capture_id': capture['id'],
                    'schema_digest': capture['schema_digest'],
                    'summary': capture['summary'],
                    'roots': capture['roots'],
                    'certificate': certificate,
                    'artifact': {'file': file.name, 'sha256': etag, 'bytes': len(data)},
                    'facts': facts(file),
                }

            for left in CERTIFIED:
                for right in CERTIFIED:
                    if left == right:
                        continue
                    view = call(base, '/compare', {
                        'left': captures[left]['capture_id'],
                        'right': captures[right]['capture_id']})['changes']
                    compare_views[pair_name(left, right)] = view

            # Sealed but never built: no bytes may be handed off.
            reset(source, pristine)
            sealed = call(base, '/captures', lookup, 201)
            try:
                call(base, f'/captures/{sealed["id"]}/database', None, 409)
                sealed_download = {'status': 409, 'error': 'fixture_not_certified'}
            except AssertionError as exc:
                sealed_download = {'unexpected': str(exc)}
            sealed_download['capture_id'] = sealed['id']
            sealed_download['state'] = sealed['state']
            sealed_download['summary'] = sealed['summary']

            # Parent key rewritten away: the recipe root can no longer resolve.
            reset(source, pristine)
            mutate(source, ["UPDATE substrate SET region='North', code='Slate' "
                            "WHERE region='East' AND code='Mica'"])
            try:
                call(base, '/captures', lookup, 422)
                rejected_rewrite = {'status': 422, 'error': 'missing_root'}
            except AssertionError as exc:
                rejected_rewrite = {'unexpected': str(exc)}
            rejected_rewrite['mutation'] = (
                "UPDATE substrate SET region='North', code='Slate' "
                "WHERE region='East' AND code='Mica'")

            # Schema drift: the old recipe refuses to seal; a new version is
            # bound to the new digest and cannot be compared with the old one.
            reset(source, pristine)
            mutate(source, ['ALTER TABLE frame ADD COLUMN caption TEXT'])
            try:
                call(base, '/captures', lookup, 409)
                rejected_drift = {'status': 409, 'error': 'schema_conflict'}
            except AssertionError as exc:
                rejected_drift = {'unexpected': str(exc)}
            rejected_drift['mutation'] = 'ALTER TABLE frame ADD COLUMN caption TEXT'
            drifted_version = call(base, f'/recipes/{RECIPE}/versions', body, 201)['version']
            drifted = call(base, f'/captures', {'name': RECIPE, 'version': drifted_version}, 201)
            call(base, f'/captures/{drifted["id"]}/build', {})
            drifted_certificate = call(base, f'/captures/{drifted["id"]}/certify', {})['certificate']
            try:
                call(base, '/compare', {'left': captures['baseline']['capture_id'],
                                        'right': drifted['id']}, 409)
                rejected_drift['compare'] = {'status': 409, 'error': 'schema_conflict'}
            except AssertionError as exc:
                rejected_drift['compare'] = {'unexpected': str(exc)}
            rejected_drift['new_version'] = drifted_version
            rejected_drift['new_schema_digest'] = drifted['schema_digest']
            rejected_drift['new_capture_digest'] = drifted_certificate['capture_digest']
            rejected_drift['not_delivered'] = (
                '新模式的捕获不写入 artifacts：模式摘要不同，不能与已认证材料直接比较。')

            # The old certified capture must remain downloadable after drift.
            data, etag = call(base, f'/captures/{captures["baseline"]["capture_id"]}/database')
            baseline_stable = (etag == captures['baseline']['artifact']['sha256']
                               and len(data) == captures['baseline']['artifact']['bytes'])
        finally:
            server.shutdown()
            thread.join()
    control = build_control(destination, captures, compare_views,
                            {'parent-key-rewrite': rejected_rewrite,
                             'schema-drift': rejected_drift},
                            sealed_download, baseline_stable, control_path)
    print(f'wrote {control_path} and {len(captures)} certified artifacts')
    return control


def stable_view(control):
    """Fields that must be identical across two independent preparations.

    Only the random capture identifiers are dropped; digests, classifications,
    row facts, and rejection outcomes must reproduce exactly.
    """
    captures = {}
    for label, record in control['captures'].items():
        captures[label] = {key: value for key, value in record.items() if key != 'capture_id'}
    return {
        'captures': captures,
        'pairs': control['pairs'],
        'dispatch': control['dispatch'],
        'excluded': control['excluded'],
        'rejected': control['rejected'],
        'sealed_download': {key: value for key, value in control['sealed_download'].items()
                            if key != 'capture_id'},
        'baseline_stable_after_drift': control['baseline_stable_after_drift'],
    }


def verify(destination=OUT, control_file=CONTROL, rebuild=True):
    control = json.loads(control_file.read_text())
    failures = []
    for label in CERTIFIED:
        record = control['captures'][label]
        file = destination / record['artifact']['file']
        problems = []
        if not file.is_file():
            problems.append('artifact file is missing')
        else:
            data = file.read_bytes()
            if hashlib.sha256(data).hexdigest() != record['artifact']['sha256']:
                problems.append('artifact bytes differ from the recorded certificate')
            if len(data) != record['artifact']['bytes']:
                problems.append('artifact size differs from the recorded certificate')
            observed = facts(file)
            if observed != record['facts']:
                problems.append('independent rows differ from the recorded facts')
            if observed['integrity_check'] != ['ok'] or observed['foreign_key_check']:
                problems.append('artifact failed integrity or foreign-key check')
            if record['certificate']['sha256'] != record['artifact']['sha256']:
                problems.append('certificate was not bound to these artifact bytes')
        if problems:
            failures.append((label, problems))
            print(f'{label}: FAIL - {"; ".join(problems)}')
        else:
            print(f'{label}: ok sha256={record["artifact"]["sha256"][:16]} '
                  f'capture={record["certificate"]["capture_digest"][:16]}')

    labels = list(CERTIFIED)
    for left in labels:
        for right in labels:
            if left == right:
                continue
            name = pair_name(left, right)
            expected = classify(control['captures'][left]['facts'],
                                control['captures'][right]['facts'])
            expected['compare_api'] = control['pairs'][name]['compare_api']
            if expected != control['pairs'][name]:
                failures.append((name, ['pair classification does not follow the facts']))
                print(f'{name}: FAIL - pair classification drifted')
    for crew, entry in control['dispatch'].items():
        if not control['pairs'][entry['pair']][f'{crew}_crew_admissible']:
            failures.append((crew, ['dispatch pair is not admissible']))
            print(f'{crew}: FAIL - dispatched pair is not admissible')
    for label, record in control['excluded'].items():
        view = control['pairs'][record['conflicting_pairs'][0]]
        if view['numeric_crew_admissible'] or view['relational_crew_admissible']:
            failures.append((label, ['excluded capture is admissible for a crew']))
            print(f'{label}: FAIL - excluded capture is admissible for a crew')
    if not control['excluded']['both']['same_assay_row_modification_as_numeric_pair']:
        failures.append(('both', ['assay modification is no longer identical to the numeric pair']))
        print('both: FAIL - assay modification split from the numeric pair')

    if rebuild:
        with TemporaryDirectory(prefix='handoff-verify-') as directory:
            work = Path(directory)
            fresh = prepare(work / 'artifacts', work / 'control.json')
            if stable_view(fresh) != stable_view(control):
                failures.append(('rebuild', ['fresh preparation differs from the committed control']))
                print('rebuild: FAIL - fresh preparation differs from the committed control')
            else:
                print('rebuild: ok - a fresh batch reproduces every recorded digest and classification')
    if failures:
        print(f'{len(failures)} failure(s)')
    else:
        print('handoff batch verified')
    return failures


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('prepare', 'verify'))
    parser.add_argument('--no-rebuild', action='store_true',
                        help='verify only the committed artifacts, without a fresh preparation')
    args = parser.parse_args()
    if args.action == 'prepare':
        prepare()
    else:
        raise SystemExit(1 if verify(rebuild=not args.no_rebuild) else 0)


if __name__ == '__main__':
    main()

"""Offline replay of the night-shift handover drill material.

Nothing here writes to the delivered repository. Every claim is recomputed from
the labelled source snapshots with the production closure code, and every sealed
capture is completed on a temporary copy of the repository.
"""

import json
import shutil
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path

from keelsplice import bundle
from keelsplice import capture as selection
from keelsplice.codec import fingerprint, loads
from keelsplice.service import Workbench
from keelsplice.storage import Repository

from .handover_common import (
    AFTER_RETARGET, AFTER_VALUE, CANDIDATE, EXPECTED, EXPECTED_VERDICTS, PUBLIC,
    RECOVER, REQUESTED, REREAD, SNAPSHOTS, DrillError, appearance, calls,
    jdumps, read_json, require, sealed_from_snapshot, sha_file, summarize,
    write_json,
)

VERDICT_FILE = 'verdicts.json'
LOST = 'relay-lost'
RETRY = 'relay-retry'


def load_manifest(material):
    manifest = read_json(material / 'manifest.json')
    require(manifest['drill'] == 'keelsplice-night-handover-v1', 'unexpected drill manifest')
    return manifest


def load_expected(material):
    local = material.parent / 'expected-verdicts.json'
    expected = read_json(local if local.is_file() else EXPECTED)
    require(expected['drill'] == 'keelsplice-night-handover-v1', 'unexpected expectation file')
    return expected


def verify_files(material, manifest):
    checked = []
    for relative, digest in sorted(manifest['material_files'].items()):
        path = material / relative
        require(path.is_file(), f'missing material file {relative}')
        require(sha_file(path) == digest, f'checksum mismatch for {relative}')
        checked.append(relative)
    require('journal.jsonl' in checked, 'the manifest must cover the reconstructed journal')
    require('manifest.json' not in checked, 'the manifest must not hash itself')
    return checked


def stored_records(repo):
    require(repo.is_file(), 'the delivered repository is missing')
    with closing(sqlite3.connect(repo.as_uri() + '?mode=ro', uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        captures = {row['id']: dict(row) for row in conn.execute(
            'SELECT id,name,version,state,sealed FROM captures ORDER BY rowid')}
        recipes = [dict(row) for row in conn.execute(
            'SELECT name,version,active,schema_digest,recipe FROM recipes ORDER BY rowid')]
    for row in captures.values():
        row['sealed'] = loads(row['sealed'])
    for row in recipes:
        row['recipe'] = loads(row['recipe'])
        row['active'] = bool(row['active'])
    return captures, recipes


def verify_recipes(manifest, recipes):
    """Check the delivered recipe rows match the manifest and the retire claim."""
    stored = {(row['name'], row['version']): row for row in recipes}
    expected = {(item['name'], item['version']): item for item in manifest['recipes']}
    require(set(stored) == set(expected),
            f'recipe set differs: repository {sorted(stored)} manifest {sorted(expected)}')
    for key, item in expected.items():
        require(stored[key]['active'] == item['active'],
                f'recipe {key} active flag differs from the manifest')
        require(stored[key]['recipe'] == item['recipe'],
                f'recipe {key} payload differs from the manifest')
    retirement = manifest['retirement']
    retired = stored[(retirement['recipe'], retirement['version'])]
    require(retired['active'] is False, 'the attic recipe is not retired in the repository')
    require(all(row['active'] for key, row in stored.items() if key !=
                (retirement['recipe'], retirement['version'])),
            'a recipe other than attic is retired unexpectedly')
    return {'recipes': [{'name': key[0], 'version': key[1],
                         'active': row['active']} for key, row in sorted(stored.items())]}


def diagnose(material, manifest, records):
    diagnoses = {}
    for entry in manifest['captures']:
        require(entry['id'] in records,
                f"manifest capture {entry['id']} is absent from the repository")
        stored = records[entry['id']]['sealed']
        recomputed = sealed_from_snapshot(material / 'sources' / SNAPSHOTS[entry['snapshot']],
                                          entry['recipe'])
        item = {
            'label': entry['label'], 'capture_id': entry['id'],
            'state': records[entry['id']]['state'], 'snapshot': entry['snapshot'],
            'sealed_digest': fingerprint(stored), 'manifest_digest': entry['capture_digest'],
            'recomputed_digest': fingerprint(recomputed),
            'byte_equal': jdumps(stored) == jdumps(recomputed),
            'appearance': appearance(stored), 'summary': summarize(stored),
            'roots': stored['result']['roots'], 'recipe': stored['recipe'],
        }
        require(item['sealed_digest'] == item['manifest_digest'],
                f'{entry["label"]}: sealed payload does not match the manifest')
        require(item['recomputed_digest'] == item['manifest_digest'],
                f'{entry["label"]}: cannot be recomputed from snapshot {entry["snapshot"]}')
        require(item['byte_equal'],
                f'{entry["label"]}: recomputation differs from the sealed payload')
        diagnoses[entry['label']] = item
    return diagnoses


def artifact_digest(material, label, diagnoses):
    sealed = calls(material / 'state' / 'repository.sqlite',
                   'SELECT sealed FROM captures WHERE id=?',
                   (diagnoses[label]['capture_id'],))[0]['sealed']
    return bundle.digest_bytes(bundle.build(loads(sealed)))


def separation(material, diagnoses):
    """Group captures by visible appearance and show how each group separates.

    An appearance group is everything a page shows without opening rows. Inside
    one group the contents can still differ; inside one content digest the only
    remaining handle is the stable identity and the journal.
    """
    groups = {}
    for label, item in diagnoses.items():
        groups.setdefault(jdumps(item['appearance']), []).append(label)
    result = {}
    for token, labels in groups.items():
        if len(labels) < 2:
            continue
        per_capture = {}
        for label in labels:
            item = diagnoses[label]
            per_capture[label] = {
                'capture_id': item['capture_id'], 'capture_digest': item['sealed_digest'],
                'artifact_sha256': artifact_digest(material, label, diagnoses),
                'roots': item['roots'], 'assay_marks': _marks_from_repo(material, item),
            }
        content = {value['capture_digest'] for value in per_capture.values()}
        artifacts = {value['artifact_sha256'] for value in per_capture.values()}
        separable = []
        if len(content) > 1:
            separable.append('capture_digest')
        if len(artifacts) > 1:
            separable.append('artifact_bytes')
        if len({jdumps(value['assay_marks']) for value in per_capture.values()}) > 1:
            separable.append('trace_membership')
        clusters = {}
        for label, value in per_capture.items():
            clusters.setdefault(value['capture_digest'], []).append(label)
        identical = [
            {'capture_digest': digest, 'labels': sorted(members),
             'separable_by': ['capture_id_and_journal'],
             'ids': {label: per_capture[label]['capture_id'] for label in sorted(members)},
             'artifact_sha256': per_capture[sorted(members)[0]]['artifact_sha256']}
            for digest, members in clusters.items() if len(members) > 1]
        result[token] = {
            'labels': labels, 'appearance': json.loads(token),
            'per_capture': per_capture, 'distinct_content_digests': len(content),
            'same_artifact_bytes': len(artifacts) == 1,
            'separable_by': separable, 'identical_content_clusters': identical}
    return result


def _marks_from_repo(material, item):
    sealed = loads(calls(material / 'state' / 'repository.sqlite',
                         'SELECT sealed FROM captures WHERE id=?',
                         (item['capture_id'],))[0]['sealed'])
    return sorted(json.loads(key)[0] for key in sealed['result']['rows']['assay'])


def require_lost_group(groups, diagnoses):
    group = next((item for item in groups.values() if {LOST, RETRY} <= set(item['labels'])), None)
    require(group is not None, 'the lost record and blind retry did not form a visible group')
    clusters = group['identical_content_clusters']
    require(len(clusters) == 1 and set(clusters[0]['labels']) == {LOST, RETRY},
            'the lost record and blind retry must be the one identical-content cluster')
    require(clusters[0]['separable_by'] == ['capture_id_and_journal'],
            f'unexpected separation basis: {clusters[0]["separable_by"]}')
    require(diagnoses[LOST]['capture_id'] != diagnoses[RETRY]['capture_id'],
            'the lost record and the retry share an identifier')
    return {'group_labels': group['labels'],
            'distinct_content_digests': group['distinct_content_digests'],
            'cluster': clusters[0],
            'sibling_labels': sorted(set(group['labels']) - {LOST, RETRY})}


def complete_on_copy(material, manifest, source_snapshot):
    """Build, certify and download every sealed capture on a throwaway copy."""
    with tempfile.TemporaryDirectory(prefix='keelsplice-replay-') as temporary:
        repo = Path(temporary) / 'repository.sqlite'
        shutil.copy2(material / 'state' / 'repository.sqlite', repo)
        workbench = Workbench(Repository(repo, {'microscope': source_snapshot}))
        completed = {}
        for entry in manifest['captures']:
            label = entry['label']
            if workbench.status(entry['id'])['state'] == 'sealed':
                workbench.build(entry['id'])
            certified = workbench.certify(entry['id'])
            data, etag = workbench.download(entry['id'])
            require(bundle.digest_bytes(data) == etag, f'{label}: download digest mismatch')
            require(etag == certified['certificate']['sha256'],
                    f'{label}: certificate does not bind the delivered bytes')
            with tempfile.TemporaryDirectory(prefix='keelsplice-check-') as check:
                file = Path(check) / 'fixture.sqlite'
                file.write_bytes(data)
                with closing(sqlite3.connect(file)) as conn:
                    require(conn.execute('PRAGMA foreign_key_check').fetchall() == [],
                            f'{label}: rebuilt fixture has foreign key violations')
                    counts = {}
                    for (table,) in conn.execute(
                            "SELECT name FROM sqlite_master WHERE type='table' "
                            "AND name NOT LIKE 'sqlite_%'"):
                        counts[table] = conn.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]
            require(counts == entry['summary']['counts'],
                    f'{label}: delivered bytes do not hold the sealed row counts')
            completed[label] = {'state': certified['state'], 'counts': counts,
                                'capture_digest': certified['certificate']['capture_digest'],
                                'artifact_sha256': etag,
                                'bytes': certified['certificate']['bytes']}

        retired = None
        try:
            workbench.capture({'name': 'attic', 'version': 1})
        except Exception as exc:  # the retirement refusal is the expected outcome
            retired = {'recipe': 'attic', 'version': 1,
                       'error': getattr(exc, 'code', type(exc).__name__),
                       'status': getattr(exc, 'status', None)}
        require(retired is not None and retired['error'] == 'retired_recipe',
                f'the retired attic version sealed a new capture: {retired}')

        # Sealing relay v1 against the later snapshot must reproduce the recorded
        # post-retarget content, proving the recipe still tracks the moved target.
        resealed = workbench.capture({'name': 'relay', 'version': 1})
        resealed_sealed = loads(calls(repo, 'SELECT sealed FROM captures WHERE id=?',
                                      (resealed['id'],))[0]['sealed'])
        expected_post = manifest['unsealed_work']['relay-retargeted-closure']['summary']
        require(comparable_summary(resealed_sealed) == comparable_summary(expected_post),
                're-sealing relay v1 against the post-retarget snapshot did not reproduce '
                'the recorded post-retarget rehearsal')
        return completed, {'attic_refusal': retired,
                           'relay_reseal': {'capture_id': resealed['id'],
                                            'capture_digest': fingerprint(resealed_sealed),
                                            'summary': summarize(resealed_sealed),
                                            'matches_recorded_preview': True}}


def comparable_summary(value):
    """Drop the root list, which a preview page reports separately."""
    summary = summarize(value) if 'result' in value else dict(value)
    summary.pop('roots', None)
    return summary


def check_snapshot_deltas(material, manifest):
    """Verify the two claimed source changes are exactly the stated ones."""
    keys = {'substrate': ('region', 'code'), 'cassette': ('serial',),
            'assay': ('mark',), 'frame': ('frame_id',)}

    def snapshot_rows(key):
        path = material / 'sources' / SNAPSHOTS[key]
        with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as conn:
            conn.row_factory = sqlite3.Row
            tables = {}
            for name, columns in keys.items():
                tables[name] = {tuple(row[column] for column in columns): dict(row)
                                for row in conn.execute(f'SELECT * FROM "{name}"')}
        return tables

    def diff(left, right):
        changes = []
        for table in sorted(set(left) | set(right)):
            before, after = left.get(table, {}), right.get(table, {})
            for token in sorted(set(before) | set(after)):
                if before.get(token) != after.get(token):
                    changes.append({'table': table, 'key': list(token),
                                    'before': before.get(token), 'after': after.get(token)})
        return changes

    public, value, retarget = (snapshot_rows(key) for key in SNAPSHOTS)
    value_changes = diff(public, value)
    retarget_changes = diff(value, retarget)
    value_digests = [snapshot['sha256'] for snapshot in manifest['snapshots']]
    require(len(set(value_digests)) == 3, 'the three source snapshots must differ')
    require([(item['table'], item['key']) for item in value_changes] == [('assay', ['S-01'])]
            and value_changes[0]['before']['length_um'] != value_changes[0]['after']['length_um']
            and value_changes[0]['before']['region'] == value_changes[0]['after']['region']
            and value_changes[0]['before']['code'] == value_changes[0]['after']['code'],
            f'the first change must be a value-only edit of assay S-01: {value_changes}')
    moved = [item for item in retarget_changes if item['table'] == 'assay' and
             (item['before'] or {}).get('region') != (item['after'] or {}).get('region')]
    require(len(moved) == 1 and moved[0]['key'] == ['S-02'],
            f'the second change must move only the S-02 relationship target: {retarget_changes}')
    public_counts = {table: len(rows) for table, rows in public.items()}
    value_counts = {table: len(rows) for table, rows in value.items()}
    retarget_counts = {table: len(rows) for table, rows in retarget.items()}
    require(public_counts == value_counts,
            'the value-only change must not alter table row counts')
    # Moving a child between parents keeps every table's total size; what changes
    # is the relay closure, which is what the rehearsal page reports.
    require(public_counts == retarget_counts,
            'the retarget moves a row between parents, so table totals stay equal')
    relay_recipe = manifest['captures'][0]['recipe']
    closures = {key: summarize(sealed_from_snapshot(material / 'sources' / SNAPSHOTS[key],
                                                    relay_recipe))
                for key in SNAPSHOTS}
    require(closures[PUBLIC]['counts'] == closures[AFTER_VALUE]['counts'],
            'the value-only change must keep the relay closure counts')
    require(closures[AFTER_VALUE]['counts'] != closures[AFTER_RETARGET]['counts'],
            'the retarget must change the relay closure counts')
    return {'value_change': value_changes,
            'retarget_changes': retarget_changes,
            'table_totals': {'public': public_counts, 'after_value': value_counts,
                             'after_retarget': retarget_counts},
            'relay_closure': {key: value['counts'] for key, value in closures.items()}}


def check_journal(material, manifest, diagnoses):
    lines = [line for line in (material / 'journal.jsonl').read_text(encoding='utf-8').splitlines()
             if line.strip()]
    events = [json.loads(line) for line in lines]
    require([event['seq'] for event in events] == list(range(1, len(events) + 1)),
            'the journal sequence is not contiguous')
    by_action = {}
    for event in events:
        by_action.setdefault(event['action'], []).append(event)
    lost = read_json(material / 'observations' / 'dropped-response.json')
    retry = by_action['capture-retry'][0]
    lost_event = by_action['read-lost-status'][0]
    listed = set(lost['reconciled']['listed_ids'])
    checks = {
        'lost_page_response_never_observed':
            lost['client_observation']['response_status'] is None and
            not manifest['lost_response']['page_response_observed'],
        'lost_request_is_in_the_journal':
            any(event['action'] == 'capture-lost-page' and
                event.get('response') is None and
                event['request']['raw_sha256'] == lost['request']['raw_sha256']
                for event in events),
        'lost_record_exists_server_side':
            lost['server_side_evidence']['repository_row_present'] and
            lost['server_side_evidence']['state'] == 'sealed',
        'journal_agrees_with_the_repository': (
            lost_event['response']['body']['state'] == diagnoses[LOST]['state'] == 'sealed' and
            lost_event['response']['body']['roots'] == diagnoses[LOST]['appearance']['roots'] and
            all(lost_event['response']['body']['summary'][key] ==
                diagnoses[LOST]['appearance'][key]
                for key in ('counts', 'rows', 'witnesses'))),
        'lost_capture_is_not_the_blind_retry':
            lost['capture_id'] == lost_event['response']['body']['id'] and
            lost['capture_id'] != retry['response']['body']['id'],
        'both_visible_in_browser_list':
            {lost['capture_id'], retry['response']['body']['id']} <= listed,
        'retry_has_an_observed_response':
            retry['response']['observed'] is True and retry['response']['status'] == 201,
        'retirement_refusals_are_recorded':
            manifest['retirement']['refusals'] == ['retired_recipe', 'retired_recipe'],
        'retired_capture_remains_sealed_and_usable':
            diagnoses['attic-legacy']['state'] == 'sealed',
    }
    require(all(checks.values()), f'incomplete journal evidence: {checks}')
    previews = read_json(material / 'observations' / 'previews.json')
    require(previews['equal_counts'] is True,
            'the recorded rehearsals must show equal row counts')
    require(previews['counts']['first'] != previews['counts']['third'],
            'the retarget rehearsal must change the row counts')
    page = (material / 'observations' / 'captures-page.html').read_text(encoding='utf-8')
    require(lost['capture_id'] in page and retry['response']['body']['id'] in page,
            'the saved capture page does not list both competing records')
    return {'events': len(events), 'actions': sorted(by_action), 'checks': checks,
            'lost_seq': lost_event['seq'], 'retry_seq': retry['seq'],
            'previews': previews['counts'], 'equal_counts': previews['equal_counts']}


def trace_evidence(material, manifest, diagnoses):
    """Recompute relation and value evidence for every captured row.

    This is the evidence a successor can use when two captures look the same:
    relation witnesses can still agree while stored values differ, and two
    attempts can agree on both while only their identity differs.
    """
    evidence = {}
    for entry in manifest['captures']:
        sealed = loads(calls(material / 'state' / 'repository.sqlite',
                             'SELECT sealed FROM captures WHERE id=?',
                             (entry['id'],))[0]['sealed'])
        traces, values = {}, {}
        for table, tokens in sealed['result']['rows'].items():
            for token in sorted(tokens):
                key = json.loads(token)
                traces[f'{table}:{token}'] = selection.trace(sealed, {'table': table, 'key': key})
                values[f'{table}:{token}'] = fingerprint(tokens[token])
        require(traces == entry.get('trace'),
                f'{entry["label"]}: recorded trace evidence does not recompute')
        require(values == entry.get('row_values'),
                f'{entry["label"]}: recorded row-value evidence does not recompute')
        evidence[entry['label']] = {'trace': traces, 'row_values': values}
    lost, retry = evidence[LOST], evidence[RETRY]
    early = evidence['relay-early']
    # The retargeted sample was never sealed; recompute it from the later snapshot.
    resealed = sealed_from_snapshot(material / 'sources' / SNAPSHOTS[AFTER_RETARGET],
                                    manifest['captures'][0]['recipe'])
    post_traces, post_values = {}, {}
    for table, tokens in resealed['result']['rows'].items():
        for token in sorted(tokens):
            key = json.loads(token)
            post_traces[f'{table}:{token}'] = selection.trace(
                resealed, {'table': table, 'key': key})
            post_values[f'{table}:{token}'] = fingerprint(tokens[token])
    post = {'trace': post_traces, 'row_values': post_values}
    require(lost['trace'] == retry['trace'] and lost['row_values'] == retry['row_values'],
            'the lost record and the retry must match on trace and values')
    require(early['trace'] == lost['trace'],
            'a value-only change must keep relation witnesses identical')
    require(early['row_values'] != lost['row_values'],
            'a value-only change must show up as different stored row values')
    require(post['trace'] != lost['trace'],
            'the retargeted capture must differ by relation witnesses')
    return {
        'lost_vs_retry': {'trace_identical': True, 'values_identical': True,
                          'only_difference': 'capture_id (and the journal event that '
                                             'produced it)'},
        'early_vs_lost': {'trace_identical': True, 'values_identical': False,
                          'difference': sorted(set(early['row_values'].items()) ^
                                               set(lost['row_values'].items()))[:2]},
        'lost_vs_post': {'trace_identical': post['trace'] == lost['trace'],
                         'values_identical': post['row_values'] == lost['row_values']},
    }


def tamper_self_test(material):
    """Prove the checks discriminate by breaking one delivered fact at a time."""
    attempts = []

    def attempt(name, mutate_material):
        with tempfile.TemporaryDirectory(prefix='keelsplice-tamper-') as temporary:
            copy = Path(temporary) / 'material'
            shutil.copytree(material, copy)
            mutate_material(copy)
            try:
                manifest = load_manifest(copy)
                verify_files(copy, manifest)
                records, _ = stored_records(copy / 'state' / 'repository.sqlite')
                diagnoses = diagnose(copy, manifest, records)
                check_journal(copy, manifest, diagnoses)
                separation(copy, diagnoses)
            except DrillError as exc:
                attempts.append({'mutation': name, 'caught': True, 'message': str(exc)[:160]})
                return
        attempts.append({'mutation': name, 'caught': False, 'message': 'no failure raised'})

    def edit_json(path, edit):
        def mutate(copy):
            target = copy / path
            value = json.loads(target.read_text(encoding='utf-8'))
            edit(value)
            target.write_text(json.dumps(value, ensure_ascii=False, indent=2,
                                         sort_keys=True) + '\n', encoding='utf-8')
            if path != 'manifest.json':
                refresh_manifest_entry(copy, path)
        return mutate

    def rewrite_snapshot(copy):
        target = copy / 'sources' / '01-after-value.sqlite'
        with closing(sqlite3.connect(target)) as conn, conn:
            conn.execute("UPDATE assay SET length_um=9.99 WHERE mark='S-01'")
        refresh_manifest_entry(copy, 'sources/01-after-value.sqlite')

    def rewrite_journal(copy):
        target = copy / 'journal.jsonl'
        lines = target.read_text(encoding='utf-8').splitlines()
        events = [json.loads(line) for line in lines if line]
        for event in events:
            if event['action'] == 'read-lost-status':
                event['response']['body']['state'] = 'built'
        target.write_text(''.join(jdumps(event) + '\n' for event in events), encoding='utf-8')
        refresh_manifest_entry(copy, 'journal.jsonl')

    def rewrite_sealed(copy, label, column, value):
        path = copy / 'state' / 'repository.sqlite'
        manifest = load_manifest(copy)
        capture_id = next(entry['id'] for entry in manifest['captures'] if entry['label'] == label)
        with closing(sqlite3.connect(path)) as conn:
            sealed = loads(conn.execute('SELECT sealed FROM captures WHERE id=?',
                                        (capture_id,)).fetchone()[0])
            token = next(iter(sealed['result']['rows']['assay']))
            sealed['result']['rows']['assay'][token][column] = value
            conn.execute('UPDATE captures SET sealed=? WHERE id=?',
                         (jdumps(sealed), capture_id))
            conn.commit()
        refresh_manifest_entry(copy, 'state/repository.sqlite')

    attempt('a stored value in a labelled snapshot is changed', rewrite_snapshot)
    attempt('a sealed capture digest is edited in the manifest',
            edit_json('manifest.json',
                      lambda value: value['captures'][0].__setitem__('capture_digest', '0' * 64)))
    attempt('a sealed payload is edited inside the repository',
            lambda copy: rewrite_sealed(copy, 'relay-lost', 4, {'real': (5.0).hex()}))
    attempt('the lost record is relabelled as the blind retry',
            edit_json('observations/dropped-response.json',
                      lambda value: value.__setitem__('capture_id',
                                                      value['reconciled']['duplicate_id'])))
    attempt('the journal forgets the lost record was sealed', rewrite_journal)
    return attempts


def refresh_manifest_entry(material, relative):
    manifest = read_json(material / 'manifest.json')
    manifest['material_files'][relative] = sha_file(material / relative)
    write_json(material / 'manifest.json', manifest)


def verdicts(manifest, diagnoses, completed):
    entries = []
    for label in manifest['labels']:
        item = diagnoses[label]
        verdict = EXPECTED_VERDICTS[label]
        reason, cannot = {
            RECOVER: (
                f"Sealed record {item['capture_id']} recomputes byte-for-byte from snapshot "
                f"{item['snapshot']}, and build/certify/download complete on a repository copy.",
                'Cannot prove the operator still wanted this exact sample; the record only '
                'proves the sample as sealed.'),
            CANDIDATE: (
                'The sealed record recomputes from its snapshot, but a blind retry left a '
                'second record with identical content. Nothing ties either attempt to the '
                'operator intent, so this proves a candidate sample, not a unique continuation.',
                'Cannot say which identifier is the intended continuation; both have the '
                'same rows, witnesses and artifact bytes.'),
        }[verdict]
        entries.append({'label': label, 'kind': 'capture', 'verdict': verdict,
                        'capture_id': item['capture_id'], 'state': item['state'],
                        'snapshot': item['snapshot'], 'capture_digest': item['sealed_digest'],
                        'appearance': item['appearance'], 'asserts': _assertion(item, completed),
                        'artifact_sha256': completed.get(label, {}).get('artifact_sha256'),
                        'proves': reason, 'cannot_prove': cannot})
    for subject, reason in REQUESTED.items():
        entries.append({'label': subject, 'kind': 'requested-work', 'verdict': REREAD,
                        'appearance': (manifest['unsealed_work'][subject]['summary']
                                       if subject in manifest.get('unsealed_work', {}) else None),
                        'proves': 'There is no persisted record of this work.',
                        'cannot_prove': reason})
    return {'recover': [item for item in entries if item['verdict'] == RECOVER],
            'candidate': [item for item in entries if item['verdict'] == CANDIDATE],
            'reread': [item for item in entries if item['verdict'] == REREAD],
            'entries': entries}


def _assertion(item, completed):
    record = completed.get(item['label'])
    if record is None:
        return {'kind': 'candidate-only'}
    return {'kind': 'sealed-record', 'state': record['state'],
            'artifact_sha256': record['artifact_sha256'],
            'counts': record['counts'],
            'recomputed_from': item['snapshot']}


def replay(material, report_path=None):
    material = Path(material).resolve()
    manifest = load_manifest(material)
    expected = load_expected(material)
    checks = []

    def step(name, produce):
        try:
            detail = produce()
        except DrillError as exc:
            checks.append({'check': name, 'ok': False, 'detail': str(exc)})
            raise
        checks.append({'check': name, 'ok': True, 'detail': detail})
        return detail

    try:
        files = step('manifest checksums cover every delivered file',
                     lambda: verify_files(material, manifest))
        records, recipes = step('repository rows match the manifest',
                                lambda: stored_records(material / 'state' / 'repository.sqlite'))
        recipe_state = step('recipe versions and the retirement flag match the manifest',
                            lambda: verify_recipes(manifest, recipes))
        diagnoses = step('every sealed capture recomputes from its labelled snapshot',
                         lambda: diagnose(material, manifest, records))
        groups = step('identical-looking captures are grouped and separable',
                      lambda: separation(material, diagnoses))
        lost_group = step('lost record and blind retry are content-identical but separable',
                          lambda: require_lost_group(groups, diagnoses))
        completed = step('all sealed captures build, certify and download on a copy',
                         lambda: complete_on_copy(material, manifest,
                                                  material / 'sources' / SNAPSHOTS[AFTER_RETARGET]))
        journal = step('journal and observations reconstruct the shift',
                       lambda: check_journal(material, manifest, diagnoses))
        deltas = step('labelled snapshots contain exactly the claimed changes',
                      lambda: check_snapshot_deltas(material, manifest))
        traces = step('relation and value evidence distinguish same-looking captures',
                      lambda: trace_evidence(material, manifest, diagnoses))
        report_verdicts = step('derived verdicts match the recorded expectation',
                               lambda: _match_expectation(
                                   verdicts(manifest, diagnoses, completed[0]), expected))
        tampering = step('negative controls: broken facts are rejected',
                         lambda: tamper_self_test(material))
        require(all(item['caught'] for item in tampering),
                f'a negative control went undetected: {tampering}')
    except DrillError:
        report = {'ok': False, 'material': str(material), 'checks': checks,
                  'verdicts': None}
        if report_path is not None:
            write_json(report_path, report)
        print_report(report)
        return report

    report = {'ok': True, 'material': display_path(material), 'checks': checks,
              'verdicts': report_verdicts, 'separation_groups': groups,
              'completion': completed[0], 'post_retirement': completed[1],
              'journal_events': journal['events'], 'lost_group': lost_group,
              'snapshot_deltas': deltas, 'recipe_state': recipe_state,
              'trace_evidence': traces, 'negative_controls': tampering}
    write_json(Path(report_path) if report_path else material.parent / VERDICT_FILE, report)
    print_report(report)
    return report


def display_path(material):
    """Report a repository-relative path when possible so the report travels."""
    anchor = Path(__file__).resolve().parent.parent
    try:
        return str(material.relative_to(anchor))
    except ValueError:
        return str(material)


def _match_expectation(derived, expected):
    actual = {item['label']: item['verdict'] for item in derived['entries']}
    wanted = {label: value['verdict'] for label, value in expected['captures'].items()} | {
        label: value['verdict'] for label, value in expected['requested_work'].items()}
    require(actual == wanted, f'derived verdicts {actual} differ from the expectation {wanted}')
    require(derived['recover'] and derived['candidate'] and derived['reread'],
            'each verdict bucket must contain at least one item')
    return derived


def print_report(report):
    print('replay ok:', report['ok'])
    for check in report['checks']:
        print(f"  [{'ok' if check['ok'] else 'FAIL'}] {check['check']}")
    if not report['ok']:
        return
    for bucket in ('recover', 'candidate', 'reread'):
        print(f'  {bucket}:')
        for item in report['verdicts'][bucket]:
            print(f"    - {item['label']} ({item['kind']}) "
                  f"{item.get('capture_id') or ''}")


if __name__ == '__main__':
    raise SystemExit('run through examples.handover_night')

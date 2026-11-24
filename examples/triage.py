"""Reproduce and separate three explanations for a post-handoff parent swap.

The reported symptom: after a preview, someone seals a capture, later opens the
certified file, and the parent of one selected row differs from the parent
recorded during review.  Two pages agree on total rows and the schema summary.
A colleague suggests another version of the same recipe name may have been used.

This script builds a controlled source pair and drives the real JSON service so
each explanation is produced by a different, independently observable path:

A. the source changed between the preview read and the sealing read;
B. a capture used another version of the same recipe name (or the alias behind
   a version was rebound) against a twin source with an identical schema;
C. the recipient was handed a different certified artifact than the capture
   named during review.

Every run uses disposable files.  ``--output`` copies the two downloaded
fixtures, the state repository and ``observations.json`` into the given
directory; ``--expect`` re-checks a committed observation file.

The same decision table can be applied afterwards with only the repository and
the delivered file; see ``apply_decision`` and ``DIAGNOSIS.md``.
"""

import argparse
import hashlib
import json
import re
import sqlite3
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from keelsplice import bundle
from keelsplice.api import create_server
from keelsplice.codec import Fault

from .walkthrough import seed

ROOT = {'table': 'assay', 'key': ['S-01']}
DRIFT_SQL = "UPDATE assay SET region='West', code='Basalt' WHERE mark='S-01';"
TWIN_SQL = """
PRAGMA foreign_keys=ON;
INSERT INTO substrate VALUES ('South','Slate',x'7f');
UPDATE assay SET region='South', code='Slate' WHERE mark='S-01';
"""
SAME_COUNTS = {'assay': 1, 'cassette': 2, 'frame': 0, 'substrate': 1}
OCCURRENCE_ROOT = {'table': 'cassette', 'key': [11]}


def request(base, route, body=None, status=200):
    headers = {'Content-Type': 'application/json'}
    data = None if body is None else json.dumps(body).encode('utf-8')
    try:
        response = urlopen(Request(base + route, data, headers), timeout=5)
    except HTTPError as exc:
        response = exc
    with response:
        payload = response.read()
        assert response.status == status, (route, response.status, payload[:500])
        if response.headers.get_content_type() == 'application/vnd.sqlite3':
            return payload, response.headers['ETag'].strip('"')
        return json.loads(payload)


def apply(path, sql):
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.executescript(sql)
    with closing(sqlite3.connect(path)) as conn:
        conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')


def source_fingerprint(path):
    """Out-of-band evidence: the repository never records this value."""
    with closing(sqlite3.connect(path)) as conn:
        return hashlib.sha256('\n'.join(conn.iterdump()).encode('utf-8')).hexdigest()


def sealed_of(repository, capture_id):
    with closing(sqlite3.connect(repository)) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute('SELECT sealed FROM captures WHERE id=?', (capture_id,)).fetchone()
    assert row is not None, capture_id
    return json.loads(row['sealed'])


def certify_bytes(data, sealed):
    try:
        return {'ok': True, 'counts': bundle.certify(data, sealed)['counts']}
    except Fault as exc:
        return {'ok': False, 'error': exc.code, 'message': str(exc)}


def parent_target(base, capture_id, parent='substrate'):
    traced = request(base, f'/captures/{capture_id}/trace', ROOT)
    arcs = [arc for arc in traced['dependencies'] if arc['target'][0] == parent]
    assert len(arcs) == 1, traced
    return arcs[0]['target'][1], arcs[0]['edge']


def file_rows(path, data):
    path.write_bytes(data)
    with closing(sqlite3.connect(path)) as conn:
        assert conn.execute('PRAGMA foreign_key_check').fetchall() == []
        rows = conn.execute(
            'SELECT a.mark, s.region, s.code FROM assay a '
            'LEFT JOIN substrate s ON s.region=a.region AND s.code=a.code'
        ).fetchall()
    return rows


def reencode(data, target):
    """Same logical content, different file bytes (as a consumer tool might save)."""
    target.write_bytes(data)
    with closing(sqlite3.connect(target)) as conn:
        conn.execute('VACUUM INTO ?', (str(target.with_suffix('.vacuum.sqlite')),))
    return target.with_suffix('.vacuum.sqlite').read_bytes()


def register(base, name, source, roots, **extra):
    return request(base, f'/recipes/{name}/versions',
                   {'source': source, 'roots': roots, 'row_limit': 20} | extra, 201)


def seal(base, name, version):
    return request(base, '/captures', {'name': name, 'version': version}, 201)


def finish(base, repository, capture):
    request(base, f"/captures/{capture['id']}/build", {})
    certified = request(base, f"/captures/{capture['id']}/certify", {})
    data, etag = request(base, f"/captures/{capture['id']}/database")
    certificate = certified['certificate']
    assert etag == certificate['sha256'] == hashlib.sha256(data).hexdigest()
    return {'capture': certified, 'certificate': certificate, 'data': data,
            'sealed': sealed_of(repository, capture['id'])}


def load_repository(repository):
    with closing(sqlite3.connect(repository)) as conn:
        conn.row_factory = sqlite3.Row
        recipes = {(row['name'], row['version']): json.loads(row['recipe'])
                   for row in conn.execute('SELECT name, version, recipe FROM recipes')}
        captures = []
        for row in conn.execute('SELECT id, name, version, state, sealed, artifact, certificate FROM captures'):
            captures.append({
                'id': row['id'], 'name': row['name'], 'version': row['version'],
                'state': row['state'], 'sealed': json.loads(row['sealed']),
                'artifact': row['artifact'],
                'certificate': json.loads(row['certificate']) if row['certificate'] else None,
            })
    return recipes, captures


def sealed_parent(sealed, child, key, parent):
    target = [child, key]
    arcs = [arc for arc in sealed['result']['arcs']
            if arc['origin'] == target and arc['direction'] == 'parent' and arc['target'][0] == parent]
    return arcs[0]['target'][1] if len(arcs) == 1 else None


def content_ok(data, sealed):
    try:
        bundle.certify(data, sealed)
        return True
    except Fault:
        return False


def identify(data, captures):
    """Return (captures whose stored artifact equals the bytes, captures whose content matches)."""
    by_artifact = [c for c in captures if c['artifact'] == data]
    by_content = [c for c in captures
                  if c['state'] == 'certified' and content_ok(data, c['sealed'])]
    return by_artifact, by_content


def apply_decision(repository, sealing_id, reviewed_version, reviewed_parent=None, delivered=None,
                   child='assay', key=('S-01',), parent='substrate', reviewed_capture=None,
                   fingerprint_before=None, fingerprint_after=None):
    """Decide A/B/C using only the repository and the opened bytes.

    ``sealing_id`` names the capture the sealing action produced.  ``delivered``
    is the file the recipient opened; without it A and B can still be judged.
    ``reviewed_parent`` is what review recorded; if ``reviewed_capture`` names a
    stored capture, its sealed parent is used and cross-checked instead.  No live
    source access is assumed; optional fingerprints are out-of-band evidence, not
    repository records.
    """
    recipes, captures = load_repository(repository)
    sealing = next(c for c in captures if c['id'] == sealing_id)
    reviewed_recipe = recipes.get((sealing['name'], reviewed_version))
    sealing_recipe = recipes.get((sealing['name'], sealing['version']))
    by_artifact, by_content = identify(delivered, captures) if delivered is not None else ([], [])
    delivered_sha = hashlib.sha256(delivered).hexdigest() if delivered is not None else None
    sealing_parent = sealed_parent(sealing['sealed'], child, list(key), parent)
    matched = {item['id'] for item in by_artifact}
    contained = {item['id'] for item in by_content}

    if reviewed_capture is not None:
        stored = next(c for c in captures if c['id'] == reviewed_capture)
        stored_parent = sealed_parent(stored['sealed'], child, list(key), parent)
        assert stored_parent is not None, 'the reviewed capture does not include the row'
        if reviewed_parent is not None:
            assert stored_parent == reviewed_parent, (stored_parent, reviewed_parent)
        reviewed_parent = stored_parent

    # C asks whether the opened file could carry a parent other than the sealed one.
    if delivered is None:
        c = {'verdict': 'undetermined', 'observation': 'no delivered file supplied'}
    elif sealing['id'] in matched:
        c = {'verdict': 'excluded', 'byte_bound_to_certificate': True,
             'observation': 'opened bytes match the sealing capture certificate and content'}
        if len(matched) > 1:
            c['byte_ambiguous_between'] = sorted(matched)
            c['note'] = ('several stored captures carry identical bytes; the certificate cannot name one of them, '
                         'but none of them has a different parent')
    elif sealing['id'] in contained:
        c = {'verdict': 'excluded', 'byte_bound_to_certificate': False,
             'observation': 'content equals the sealing capture but bytes differ from its certificate; the file '
                            'was re-saved or copied, ask the recipient for the delivered SHA-256'}
    elif matched:
        c = {'verdict': 'confirmed', 'actual_captures': sorted(matched),
             'actual_version': by_artifact[0]['version'],
             'observation': 'opened bytes carry another certified capture certificate'}
    elif contained:
        c = {'verdict': 'confirmed', 'actual_captures': sorted(contained),
             'actual_version': by_content[0]['version'],
             'observation': 'bytes differ from every certificate but content equals another capture'}
    else:
        c = {'verdict': 'unresolved', 'observation': 'bytes match no stored capture; altered or foreign file'}

    # B asks whether the sealing action recorded a different version of the same name.
    same_version = sealing['version'] == reviewed_version
    same_binding = same_version and reviewed_recipe is not None and sealing_recipe == reviewed_recipe
    if same_version:
        b = {'verdict': 'excluded', 'observation': 'capture row records the reviewed version'}
    elif reviewed_recipe is not None and sealing_recipe == reviewed_recipe:
        b = {'verdict': 'confirmed_but_equivalent', 'sealed_version': sealing['version'],
             'sealed_source': sealing_recipe['source'],
             'observation': 'another version was used, but it normalizes to the same source, roots and '
                            'reverse edges; it can only matter through the source file itself'}
    else:
        b = {'verdict': 'confirmed', 'sealed_version': sealing['version'],
             'sealed_source': sealing_recipe['source'] if sealing_recipe else None,
             'observation': 'capture row records a version other than the reviewed one'}

    # A asks whether the same source binding yielded different data between reads.
    if reviewed_parent is None:
        a = {'verdict': 'undetermined', 'observation': 'no reviewed parent supplied'}
    elif not same_binding:
        a = {'verdict': 'excluded', 'observation': 'sealing used a different version or source binding'}
    elif sealing_parent == reviewed_parent:
        a = {'verdict': 'excluded', 'observation': 'sealing capture retained the reviewed parent'}
    elif fingerprint_before is not None and fingerprint_after is not None and fingerprint_before != fingerprint_after:
        a = {'verdict': 'confirmed', 'sealing_parent': sealing_parent,
             'observation': 'same binding, sealed parent differs, out-of-band source fingerprint changed'}
    else:
        a = {'verdict': 'consistent_unconfirmed', 'sealing_parent': sealing_parent,
             'observation': 'same binding and sealed parent differs; source bytes are not recorded, so drift '
                            'or an alias rebinding cannot be confirmed from history alone'}

    return {
        'delivered_sha256': delivered_sha,
        'delivered_subject': (sorted(matched) or sorted(contained) or None) if delivered is not None else None,
        'sealing_capture': {'id': sealing['id'], 'name': sealing['name'],
                            'version': sealing['version'], 'sealed_parent': sealing_parent},
        'reviewed_version': reviewed_version,
        'reviewed_parent': reviewed_parent,
        'verdicts': {'A': a, 'B': b, 'C': c},
    }


def run(base, root, repository, primary, twin):
    catalog = request(base, '/sources/primary/catalog')
    twin_catalog = request(base, '/sources/twin/catalog')
    assert catalog['schema_digest'] == twin_catalog['schema_digest']

    v1 = register(base, 'parent-shift', 'primary', [ROOT])
    v2 = register(base, 'parent-shift', 'twin', [ROOT])
    v3 = register(base, 'parent-shift', 'primary', [ROOT])
    assert v1['schema_digest'] == v2['schema_digest'] == v3['schema_digest']
    assert v1['recipe'] == v3['recipe'] != v2['recipe']
    assert v2['recipe']['source'] == 'twin'

    # The reviewed capture, sealed against an unchanged primary source.
    before = source_fingerprint(primary)
    preview_before = request(base, '/preview', {'name': 'parent-shift', 'version': 1})
    assert preview_before['summary']['counts'] == SAME_COUNTS
    reviewed = finish(base, repository, seal(base, 'parent-shift', 1))
    reviewed_parent, reviewed_edge = parent_target(base, reviewed['capture']['id'])
    assert reviewed_parent == ['East', 'Mica']
    # Boundary: resealing while nothing changed reproduces the reviewed file.
    clone = finish(base, repository, seal(base, 'parent-shift', 1))
    assert clone['certificate'] == reviewed['certificate']
    assert clone['data'] == reviewed['data']
    assert source_fingerprint(primary) == before

    # A. Source changed between the preview read and the sealing read.
    apply(primary, DRIFT_SQL)
    after = source_fingerprint(primary)
    preview_after = request(base, '/preview', {'name': 'parent-shift', 'version': 1})
    drifted = finish(base, repository, seal(base, 'parent-shift', 1))
    drifted_parent, drifted_edge = parent_target(base, drifted['capture']['id'])
    changes = request(base, '/compare', {
        'left': reviewed['capture']['id'], 'right': drifted['capture']['id']})['changes']
    facts_a = {
        'source_fingerprint_changed': before != after,
        'preview_totals_equal': preview_before['summary'] == preview_after['summary'],
        'preview_roots_equal': preview_before['roots'] == preview_after['roots'],
        'preview_schema_equal': preview_before['schema_digest'] == preview_after['schema_digest'],
        'capture_totals_equal': reviewed['capture']['summary'] == drifted['capture']['summary'],
        'capture_schema_equal': reviewed['capture']['schema_digest'] == drifted['capture']['schema_digest'],
        'edge_identity_equal': reviewed_edge == drifted_edge,
        'parents': [reviewed_parent, drifted_parent],
        'certificate_sha256_differ': reviewed['certificate']['sha256'] != drifted['certificate']['sha256'],
        'compare_assay': changes['assay'],
        'compare_substrate': changes['substrate'],
    }
    assert preview_after['summary'] == preview_before['summary']
    assert preview_after['roots'] == preview_before['roots']
    assert drifted['capture']['summary'] == reviewed['capture']['summary']
    assert drifted['capture']['schema_digest'] == reviewed['capture']['schema_digest']
    assert changes['assay']['modified'] == [['S-01']]

    # B. Another version of the same recipe name, bound to a twin source.
    preview_twin = request(base, '/preview', {'name': 'parent-shift', 'version': 2})
    sibling = finish(base, repository, seal(base, 'parent-shift', 2))
    sibling_parent, _ = parent_target(base, sibling['capture']['id'])
    twin_source = request(base, '/recipes/parent-shift/versions/2')['recipe']['source']
    facts_b = {
        'capture_version': sibling['capture']['version'],
        'reviewed_capture_version': reviewed['capture']['version'],
        'recipe_source': twin_source,
        'totals_equal': sibling['capture']['summary'] == reviewed['capture']['summary'],
        'preview_totals_equal': preview_twin['summary'] == preview_before['summary'],
        'parents': [reviewed_parent, sibling_parent],
        'certificate_sha256_differ': sibling['certificate']['sha256'] != reviewed['certificate']['sha256'],
    }
    assert preview_twin['summary']['counts'] == SAME_COUNTS
    assert sibling['capture']['version'] == 2
    assert sibling_parent == ['South', 'Slate']
    assert sibling['capture']['summary'] == reviewed['capture']['summary']

    # Control: a different closure size would have shown up in the totals pages.
    register(base, 'wider', 'primary', [ROOT, {'table': 'assay', 'key': ['S-02']}])
    wider = finish(base, repository, seal(base, 'wider', 1))
    wider_parent, _ = parent_target(base, wider['capture']['id'])
    facts_control = {
        'reviewed_rows': reviewed['capture']['summary']['rows'],
        'wider_rows': wider['capture']['summary']['rows'],
        'totals_equal': wider['capture']['summary']['rows'] == reviewed['capture']['summary']['rows'],
        'schema_equal': wider['capture']['schema_digest'] == reviewed['capture']['schema_digest'],
        'parents_for_focus_row': [reviewed_parent, wider_parent],
    }
    assert wider['capture']['summary']['rows'] != reviewed['capture']['summary']['rows']
    assert wider['capture']['schema_digest'] == reviewed['capture']['schema_digest']

    # C. The recipient opens a certified file that belongs to another capture.
    received = hashlib.sha256(drifted['data']).hexdigest()
    facts_c = {
        'received_sha_matches_reviewed_certificate': received == reviewed['certificate']['sha256'],
        'received_sha_matches_drifted_certificate': received == drifted['certificate']['sha256'],
        'capture_ids_differ': reviewed['capture']['id'] != drifted['capture']['id'],
        'certify_as_reviewed': certify_bytes(drifted['data'], reviewed['sealed']),
        'certify_as_drifted': certify_bytes(drifted['data'], drifted['sealed']),
        'certify_as_sibling': certify_bytes(drifted['data'], sibling['sealed']),
        'rows_in_reviewed_file': file_rows(root / 'reviewed.sqlite', reviewed['data']),
        'rows_in_received_file': file_rows(root / 'drifted.sqlite', drifted['data']),
    }
    assert facts_c['certify_as_reviewed']['error'] == 'artifact_content'
    assert facts_c['certify_as_drifted']['ok']
    assert facts_c['certify_as_sibling']['error'] == 'artifact_content'
    assert facts_c['rows_in_reviewed_file'] == [('S-01', 'East', 'Mica')]
    assert facts_c['rows_in_received_file'] == [('S-01', 'West', 'Basalt')]

    facts_history = {
        'reviewed': {
            'id': reviewed['capture']['id'],
            'version': reviewed['capture']['version'],
            'schema_digest': reviewed['capture']['schema_digest'],
            'summary': reviewed['capture']['summary'],
            'certificate': reviewed['certificate'],
        },
        'drifted': {
            'id': drifted['capture']['id'],
            'version': drifted['capture']['version'],
            'schema_digest': drifted['capture']['schema_digest'],
            'summary': drifted['capture']['summary'],
            'certificate': drifted['certificate'],
        },
        'resolved_same_state': {
            'same_version': clone['capture']['version'] == reviewed['capture']['version'],
            'certificate_equal': clone['certificate'] == reviewed['certificate'],
            'bytes_equal': clone['data'] == reviewed['data'],
        },
    }

    # C boundary: identical delivered bytes can still mean different evidence.
    register(base, 'occurrence', 'primary', [OCCURRENCE_ROOT])
    register(base, 'occurrence', 'primary', [OCCURRENCE_ROOT, OCCURRENCE_ROOT])
    once = finish(base, repository, seal(base, 'occurrence', 1))
    twice = finish(base, repository, seal(base, 'occurrence', 2))
    facts_c['identical_bytes_different_roots'] = {
        'bytes_equal': once['data'] == twice['data'],
        'sha_equal': once['certificate']['sha256'] == twice['certificate']['sha256'],
        'capture_digest_equal': once['certificate']['capture_digest'] == twice['certificate']['capture_digest'],
        'roots': [once['capture']['roots'], twice['capture']['roots']],
    }
    assert once['data'] == twice['data']
    assert once['certificate']['sha256'] == twice['certificate']['sha256']
    assert once['certificate']['capture_digest'] != twice['certificate']['capture_digest']

    decision = {
        'A': apply_decision(repository, drifted['capture']['id'], 1, reviewed_parent,
                            drifted['data'], fingerprint_before=before, fingerprint_after=after),
        'B': apply_decision(repository, sibling['capture']['id'], 1, reviewed_parent,
                            sibling['data'], fingerprint_before=before, fingerprint_after=after),
        'C': apply_decision(repository, reviewed['capture']['id'], 1, reviewed_parent,
                            drifted['data'], fingerprint_before=before, fingerprint_after=after),
        'A_without_fingerprints': apply_decision(repository, drifted['capture']['id'], 1, reviewed_parent,
                                                 drifted['data']),
    }
    assert decision['A']['verdicts']['A']['verdict'] == 'confirmed'
    assert decision['A']['verdicts']['B']['verdict'] == 'excluded'
    assert decision['A']['verdicts']['C']['verdict'] == 'excluded'
    assert decision['B']['verdicts']['B']['verdict'] == 'confirmed'
    assert decision['B']['verdicts']['A']['verdict'] == 'excluded'
    assert decision['C']['verdicts']['C']['verdict'] == 'confirmed'
    assert decision['C']['verdicts']['A']['verdict'] == 'excluded'
    assert decision['C']['verdicts']['B']['verdict'] == 'excluded'
    assert decision['A_without_fingerprints']['verdicts']['A']['verdict'] == 'consistent_unconfirmed'

    # A version that normalizes identically is B-"confirmed" but equivalent; only version differs.
    equivalent = finish(base, repository, seal(base, 'parent-shift', 3))
    assert equivalent['data'] == drifted['data']
    equivalent_decision = apply_decision(repository, equivalent['capture']['id'], 1, reviewed_parent,
                                         equivalent['data'])
    assert equivalent_decision['verdicts']['B']['verdict'] == 'confirmed_but_equivalent'
    assert equivalent_decision['verdicts']['A']['verdict'] == 'excluded'
    assert equivalent_decision['verdicts']['C']['verdict'] == 'excluded'
    assert decision['B']['verdicts']['B']['verdict'] == 'confirmed'
    facts_equivalent = {
        'version': equivalent['capture']['version'],
        'recipe_equals_reviewed': request(base, '/recipes/parent-shift/versions/3')['recipe']
                                   == request(base, '/recipes/parent-shift/versions/1')['recipe'],
        'bytes_equal_drifted': equivalent['data'] == drifted['data'],
        'decision': equivalent_decision['verdicts'],
    }

    # Delivered bytes can be byte-identical for two captures; the certificate cannot pick one.
    ambiguous = apply_decision(repository, once['capture']['id'], once['capture']['version'],
                               None, once['data'], child='cassette', key=(11,), parent='cassette',
                               reviewed_capture=twice['capture']['id'])
    assert ambiguous['verdicts']['C']['byte_ambiguous_between']
    assert len(ambiguous['verdicts']['C']['byte_ambiguous_between']) == 2
    assert ambiguous['verdicts']['C']['verdict'] == 'excluded'

    # A consumer may re-save the same content; the certificate names bytes, not content.
    reencoded = reencode(reviewed['data'], root / 'reviewed-copy.sqlite')
    assert reencoded != reviewed['data']
    assert bundle.certify(reencoded, reviewed['sealed'])['counts'] == reviewed['certificate']['counts']
    reencoded_decision = apply_decision(repository, reviewed['capture']['id'], 1, reviewed_parent, reencoded)
    assert reencoded_decision['verdicts']['C']['verdict'] == 'excluded'
    assert reencoded_decision['verdicts']['C']['byte_bound_to_certificate'] is False
    decisions_reencoded = {
        'delivered_sha_matches_certificate': hashlib.sha256(reencoded).hexdigest() == reviewed['certificate']['sha256'],
        'content_certifies': True,
        'C': reencoded_decision['verdicts']['C'],
    }

    # A same-version change that drops the parent link would also change the
    # totals, so the reported equal row counts already exclude this shape.
    apply(primary, "UPDATE assay SET region=NULL, code=NULL WHERE mark='S-01';")
    count_drift = finish(base, repository, seal(base, 'parent-shift', 1))
    facts_control['count_changing_drift'] = {
        'reviewed_rows': reviewed['capture']['summary']['rows'],
        'drifted_rows': count_drift['capture']['summary']['rows'],
        'drifted_counts': count_drift['capture']['summary']['counts'],
        'schema_equal': count_drift['capture']['schema_digest'] == reviewed['capture']['schema_digest'],
    }
    assert count_drift['capture']['summary']['rows'] != reviewed['capture']['summary']['rows']
    assert count_drift['capture']['schema_digest'] == reviewed['capture']['schema_digest']

    return {'same_schema_digest': True, 'areas': {'A': facts_a, 'B': facts_b, 'C': facts_c,
                                                  'control': facts_control,
                                                  'equivalent_version': facts_equivalent},
            'history': facts_history, 'decisions': decision,
            'reencoded_delivery': decisions_reencoded}


def alias_remap(root):
    """A recipe stores the alias, not the file; a restart may rebind the alias."""
    first_file, second_file = root / 'alias-a.sqlite', root / 'alias-b.sqlite'
    seed(first_file)
    seed(second_file)
    apply(second_file, TWIN_SQL)
    repository = root / 'alias-repository.sqlite'
    observed = []
    for path in (first_file, second_file):
        with create_server(('127.0.0.1', 0), repository, {'lab': path}) as server:
            thread = Thread(target=server.serve_forever, kwargs={'poll_interval': 0.05})
            thread.start()
            try:
                base = f'http://127.0.0.1:{server.server_port}'
                if not observed:
                    version = register(base, 'shared', 'lab', [ROOT])
                    assert version['version'] == 1
                else:
                    assert request(base, '/captures/' + observed[0]['capture']['id'])['state'] == 'certified'
                observed.append(finish(base, repository, seal(base, 'shared', 1)))
            finally:
                server.shutdown()
                thread.join()
    first, second = observed
    parents = [[row[1:] for row in file_rows(root / 'alias.sqlite', item['data'])]
               for item in (first, second)]
    facts = {
        'same_version': first['capture']['version'] == second['capture']['version'],
        'same_schema_digest': first['capture']['schema_digest'] == second['capture']['schema_digest'],
        'totals_equal': first['capture']['summary'] == second['capture']['summary'],
        'parents': parents,
        'first_still_certified': first['capture']['state'] == 'certified',
        'certificate_sha256_differ': first['certificate']['sha256'] != second['certificate']['sha256'],
    }
    assert facts['same_version'] and facts['totals_equal']
    assert parents == [[('East', 'Mica')], [('South', 'Slate')]]
    assert facts['certificate_sha256_differ']
    # With history alone this is indistinguishable from explanation A: same
    # version, same stored recipe, different sealed parent.
    decision = apply_decision(repository, second['capture']['id'], 1, ['East', 'Mica'], second['data'])
    facts['decision_without_source_fingerprints'] = decision['verdicts']
    assert decision['verdicts']['B']['verdict'] == 'excluded'
    assert decision['verdicts']['A']['verdict'] == 'consistent_unconfirmed'
    return facts


def check_expectation(text, expected_path):
    """Compare against a committed observation file, masking random capture ids."""
    mask = lambda document: re.sub(r'[0-9a-f]{32}', 'CAPTURE', document)
    expected = json.loads(Path(expected_path).read_text(encoding='utf-8'))
    actual = json.loads(text)
    if mask(json.dumps(actual, sort_keys=True)) != mask(json.dumps(expected, sort_keys=True)):
        raise SystemExit(f'观察结果与 {expected_path} 不一致')


def main():
    parser = argparse.ArgumentParser(description='区分封存后父关系不一致的三种解释。')
    parser.add_argument('--output', type=Path, help='把观察结果和下载文件写到这个目录')
    parser.add_argument('--expect', type=Path,
                        help='把本次观察与给定 observations.json 比对（忽略随机捕获编号）')
    parser.add_argument('--diagnose', type=Path, metavar='REPOSITORY',
                        help='不重建场景，直接按已有状态库和交付文件给出判断')
    parser.add_argument('--delivered', type=Path, help='接收方打开的文件（与 --diagnose 同用）')
    parser.add_argument('--sealing', help='封存动作产生的捕获编号（与 --diagnose 同用）')
    parser.add_argument('--reviewed-version', type=int, help='核对时看到的配方版本')
    parser.add_argument('--reviewed-parent', help='核对时看到的父键 JSON，例如 ["East","Mica"]')
    parser.add_argument('--reviewed-capture', help='核对时那次捕获的编号；给出时以它的封存父键为准')
    parser.add_argument('--child', default='assay')
    parser.add_argument('--key', default='["S-01"]', help='关注行的主键 JSON')
    parser.add_argument('--parent', default='substrate')
    parser.add_argument('--fingerprint-before', help='试算前源文件摘要（可选，外部证据）')
    parser.add_argument('--fingerprint-after', help='封存后源文件摘要（可选，外部证据）')
    args = parser.parse_args()
    if args.diagnose:
        if not (args.sealing and args.reviewed_version is not None
                and (args.reviewed_parent or args.reviewed_capture)):
            parser.error('--diagnose 需要 --sealing、--reviewed-version，以及 --reviewed-parent 或 --reviewed-capture')
        verdict = apply_decision(
            args.diagnose, args.sealing, args.reviewed_version,
            json.loads(args.reviewed_parent) if args.reviewed_parent else None,
            args.delivered.read_bytes() if args.delivered else None,
            child=args.child, key=tuple(json.loads(args.key)), parent=args.parent,
            reviewed_capture=args.reviewed_capture, fingerprint_before=args.fingerprint_before,
            fingerprint_after=args.fingerprint_after)
        print(json.dumps(verdict, ensure_ascii=False, indent=2, sort_keys=True))
        return
    with TemporaryDirectory(prefix='keelsplice-triage-') as directory:
        root = Path(directory)
        primary, twin = root / 'primary.sqlite', root / 'twin.sqlite'
        seed(primary)
        seed(twin)
        apply(twin, TWIN_SQL)
        repository = root / 'repository.sqlite'
        with create_server(('127.0.0.1', 0), repository, {'primary': primary, 'twin': twin}) as server:
            thread = Thread(target=server.serve_forever, kwargs={'poll_interval': 0.05})
            thread.start()
            try:
                facts = run(f'http://127.0.0.1:{server.server_port}', root, repository, primary, twin)
            finally:
                server.shutdown()
                thread.join()
        facts['alias_remap'] = alias_remap(root)
        text = json.dumps(facts, ensure_ascii=False, indent=2, sort_keys=True)
        print(text)
        if args.expect:
            check_expectation(text, args.expect)
        if args.output:
            args.output.mkdir(parents=True, exist_ok=True)
            (args.output / 'observations.json').write_text(text + '\n', encoding='utf-8')
            (args.output / 'reviewed.sqlite').write_bytes((root / 'reviewed.sqlite').read_bytes())
            (args.output / 'drifted.sqlite').write_bytes((root / 'drifted.sqlite').read_bytes())
            (args.output / 'repository.sqlite').write_bytes(repository.read_bytes())


if __name__ == '__main__':
    main()

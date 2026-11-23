"""Compare two candidate reverse-edge groups against the recipe, on one snapshot.

Four variants are evaluated inside a single source read transaction so their row
identities share one comparable read basis:

* ``baseline`` keeps the edges stored on the recipe version;
* ``group_a`` / ``group_b`` replace those edges with each candidate group;
* ``union`` adopts both groups together.

Each variant keeps its own per-table budget accounting. A variant that exceeds a
budget is reported as failed and excluded from the identity-set comparisons, so a
failed trial can never silently drive a decision.
"""

from . import capture
from .codec import Fault, fingerprint
from .recipe import fields, validate_edges

VARIANTS = ('baseline', 'group_a', 'group_b', 'union')
VARIANT_GROUPS = {'baseline': 'baseline', 'group_a': 'group_a', 'group_b': 'group_b', 'union': 'union'}


def request_shape(body):
    fields(body, ('name', 'version', 'group_a', 'group_b'), ('basis',))
    if type(body['version']) is not int or not 1 <= body['version'] < 2**63:
        raise Fault('invalid_version', 'Supply a positive recipe version')
    return body


def normalize_groups(schema, body):
    group_a = validate_edges(schema, body['group_a'])
    group_b = validate_edges(schema, body['group_b'])
    overlap = {(e['child'], e['id']) for e in group_a} & {(e['child'], e['id']) for e in group_b}
    if overlap:
        child, number = sorted(overlap)[0]
        raise Fault('overlapping_groups', f'Edge {child} #{number} appears in both candidate groups')
    return group_a, group_b


def variant_recipe(recipe, edges):
    return dict(recipe, follow_children=edges)


def edge_labels(edges, group_key):
    return {(edge['child'], edge['id']): group_key for edge in edges}


def combine_labels(*maps):
    labels = {}
    for mapping in maps:
        labels.update(mapping)
    return labels


def identity_payload(identities):
    """Stable JSON-friendly sorted list of [table, key] identities."""
    return [[table, list(values)] for table, values in
            sorted(identities, key=lambda item: (item[0], capture.dumps(list(item[1]))))]


def compare_identities(ok_scenarios):
    """Solo versus solo and union versus solo set findings for ok scenarios."""
    have = {name: scenario['identities'] for name, scenario in ok_scenarios.items()}
    findings = {}
    if {'group_a', 'group_b'} <= set(have):
        findings['solo_a_vs_solo_b'] = {
            'only_group_a': identity_payload(have['group_a'] - have['group_b']),
            'only_group_b': identity_payload(have['group_b'] - have['group_a']),
            'shared_identity': identity_payload(have['group_a'] & have['group_b']),
        }
    if 'union' in have:
        union = have['union']
        solo = set()
        for name in ('group_a', 'group_b'):
            if name in have:
                solo |= have[name]
        findings['union_vs_solos'] = {
            'reached_only_together': identity_payload(union - solo),
            'covered_by_a_solo': identity_payload(union & solo),
        }
    if 'baseline' in have:
        baseline = have['baseline']
        for name in ('group_a', 'group_b', 'union'):
            if name in have:
                findings[f'{name}_vs_baseline'] = {
                    'added_vs_baseline': identity_payload(have[name] - baseline),
                    'removed_vs_baseline': identity_payload(baseline - have[name]),
                }
    return findings


def content_evidence(basis):
    """Content-bearing evidence: schema binding plus hashed main and WAL files."""
    return {
        'source': basis['source'],
        'schema_digest': basis['schema_digest'],
        'files': [{'file': item['file'], 'bytes': item['bytes'], 'sha256': item['sha256']}
                  for item in basis['files'] if 'sha256' in item],
    }


def basis_status(previous, current):
    """Decide whether an earlier read basis can still ground a decision.

    Only content-bearing evidence (schema digest and main/WAL byte hashes) decides
    staleness. SHM journals are transient metadata and never count as source writes.
    """
    if previous is None:
        return {'comparison': 'single_snapshot', 'stale': False, 'reason': None,
                'submitted': None, 'current': current}
    expected = {'source', 'files', 'schema_digest', 'digest'}
    if not isinstance(previous, dict) or set(previous) != expected:
        raise Fault('invalid_basis', 'Submit the basis object returned by an earlier trial')
    if previous.get('source') != current['source'] or fingerprint(content_evidence(previous)) != previous['digest']:
        raise Fault('invalid_basis', 'Basis object is malformed or was modified')
    stale = content_evidence(previous) != content_evidence(current)
    return {
        'comparison': 'same_basis' if not stale else 'basis_changed',
        'stale': stale,
        'reason': None if not stale else '源库在两次试算之间发生过写入，旧读取依据不能用于本次决定。',
        'submitted': previous,
        'current': current,
    }


def variant_labels(name, recipe, group_a, group_b):
    """Map every edge the variant adopts to the candidate group it belongs to.

    Parent-closure arcs over an adopted edge inherit that edge's group; an edge
    adopted for neither candidate group (baseline only) is labeled ``baseline``.
    """
    if name == 'baseline':
        return edge_labels(recipe['follow_children'], 'baseline')
    if name == 'group_a':
        return edge_labels(group_a, 'group_a')
    if name == 'group_b':
        return edge_labels(group_b, 'group_b')
    return combine_labels(edge_labels(group_a, 'group_a'), edge_labels(group_b, 'group_b'))


def run_variants(conn, schema, recipe, group_a, group_b):
    """Evaluate every variant against the shared read transaction."""
    plans = {
        'baseline': recipe['follow_children'],
        'group_a': group_a,
        'group_b': group_b,
        'union': [dict(edge) for edge in group_a] + [dict(edge) for edge in group_b],
    }
    scenarios = {}
    for name in VARIANTS:
        edges = plans[name]
        labels = variant_labels(name, recipe, group_a, group_b)
        try:
            result = capture.collect(conn, schema, variant_recipe(recipe, edges))
        except Fault as exc:
            scenarios[name] = {'status': 'failed', 'error': {'code': exc.code, 'message': str(exc)},
                               'edges': edges, 'edge_group': VARIANT_GROUPS[name]}
            continue
        scenarios[name] = {
            'status': 'ok',
            'edges': edges,
            'edge_group': VARIANT_GROUPS[name],
            'summary': capture.summarize(result),
            'roots': result['roots'],
            'identities': capture.identity_set(result),
            'row_identities': capture.row_identities(result),
            'attribution': capture.attribution(result, labels),
        }
    return scenarios


def payload(schema_digest, basis, status, scenarios):
    ok = {name: scenario for name, scenario in scenarios.items() if scenario['status'] == 'ok'}
    rendered = {}
    for name, scenario in scenarios.items():
        item = {key: scenario[key] for key in ('status', 'edges', 'edge_group')}
        if scenario['status'] == 'ok':
            item.update(summary=scenario['summary'], roots=scenario['roots'],
                        row_identities=scenario['row_identities'], attribution=scenario['attribution'])
        else:
            item['error'] = scenario['error']
        rendered[name] = item
    return {
        'schema_digest': schema_digest,
        'basis': basis,
        'basis_status': status,
        'scenarios': rendered,
        'findings': compare_identities(ok),
        'decidable': status['comparison'] != 'basis_changed' and len(ok) >= 2,
    }

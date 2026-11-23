"""Audit a candidate source file against a live WAL database before onboarding.

The operator is handed several filesystem views of the same running database:
directory copies, an alias path that follows the original file, and a separate
database that merely shares the schema.  The evidence here distinguishes three
different guarantees that look identical at the filename level:

* ``same_object``        - the path resolves to the very same file (device/inode),
* ``same_database_copy`` - a different file, yet a read-only snapshot still shows
                           the committed representative rows,
* ``main_file_only``     - the main database file matches byte-for-byte, but the
                           committed WAL-only rows are not visible,
* ``divergent_database`` - same shape, different committed content.

A baseline records the evidence observed at confirmation time.  Re-checking it
after the source has moved on states explicitly which earlier conclusions still
hold and which have drifted.
"""

import hashlib
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

from .catalog import inspect, resolve
from .codec import Fault, decode, digest, dumps, fingerprint, quote

CHUNK = 64 * 1024
SIDECARS = ('-wal', '-shm')

# Verdict buckets, ordered from strongest to weakest guarantee.
SAME_OBJECT = 'same_object'
SAME_DATABASE_COPY = 'same_database_copy'
MAIN_FILE_ONLY = 'main_file_only'
DIVERGENT_DATABASE = 'divergent_database'
UNREADABLE = 'unreadable'

VERDICTS = (SAME_OBJECT, SAME_DATABASE_COPY, MAIN_FILE_ONLY, DIVERGENT_DATABASE, UNREADABLE)
# Verdict buckets that are safe to hand to a new recipe.
ONBOARDABLE = (SAME_OBJECT, SAME_DATABASE_COPY)


def file_hash(path):
    """Stable digest of the bytes currently stored in one file."""
    hasher = hashlib.sha256()
    with open(path, 'rb') as handle:
        for chunk in iter(lambda: handle.read(CHUNK), b''):
            hasher.update(chunk)
    return hasher.hexdigest()


def identity_token(path):
    stat = path.stat()
    return {'device': stat.st_dev, 'inode': stat.st_ino}


def sidecars(path):
    # SQLite resolves a symlink to its target before locating -wal/-shm files, so
    # sidecar names must be derived from the resolved path, not the alias spelling.
    resolved = Path(path).resolve()
    return {suffix.lstrip('-'): Path(str(resolved) + suffix).is_file() for suffix in SIDECARS}


def normalize_probes(probes):
    if not isinstance(probes, list) or not 1 <= len(probes) <= 64:
        raise Fault('invalid_probe', 'Specify 1 to 64 representative row probes')
    normalized = []
    for probe in probes:
        if not isinstance(probe, dict) or set(probe) != {'table', 'key'}:
            raise Fault('invalid_probe', 'Each probe needs a table and a typed key')
        key = probe['key']
        if not isinstance(key, list) or not key or None in key:
            raise Fault('invalid_probe', 'Each probe needs a complete non-NULL key tuple')
        normalized.append({'table': probe['table'], 'key': [decode(value) for value in key]})
    return normalized


@contextmanager
def staged_read_only(path):
    """Stage the main file and its pre-existing sidecars, then open read-only.

    SQLite materializes ``-wal``/``-shm`` files whenever it opens a WAL database,
    even in read-only mode.  Probing inside the operator's directory would therefore
    create evidence that was not there before and fake drift on every re-check.
    Copying the candidate into a private staging area keeps the original directory
    untouched while preserving whatever WAL frame was actually handed over.
    """
    candidate = Path(path).resolve()
    with TemporaryDirectory(prefix='keelsplice-onboard-') as directory:
        staged = Path(directory) / candidate.name
        try:
            os.link(candidate, staged)
        except OSError:
            staged.write_bytes(candidate.read_bytes())
        for suffix in SIDECARS:
            sidecar = Path(str(candidate) + suffix)
            if sidecar.is_file():
                target = Path(str(staged) + suffix)
                try:
                    os.link(sidecar, target)
                except OSError:
                    target.write_bytes(sidecar.read_bytes())
        conn = sqlite3.connect(staged.as_uri() + '?mode=ro', uri=True, timeout=2,
                               isolation_level=None)
        try:
            yield conn
        finally:
            conn.close()


def lookup_row(conn, table, pk, values):
    # Binding against the declared key columns keeps SQLite collation and type affinity.
    condition = ' AND '.join(f't.{quote(column)} = ?' for column in pk)
    row = conn.execute(f'SELECT t.* FROM {quote(table)} t WHERE {condition} LIMIT 1',
                       values).fetchone()
    return row is not None


def probe_database(path, probes):
    """Return independent read-only evidence about schema and committed rows."""
    with staged_read_only(path) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA query_only=ON')
        conn.execute('PRAGMA trusted_schema=OFF')
        conn.execute('BEGIN')
        schema = inspect(conn)
        results = []
        for probe in probes:
            table = resolve(schema['tables'], probe['table'])
            pk = schema['tables'][table]['pk']
            values = probe['key']
            if len(values) != len(pk):
                raise Fault('invalid_probe', f'{table} needs {len(pk)} key values')
            found = lookup_row(conn, table, pk, values)
            results.append({'table': table, 'key': values, 'found': found})
        digest_value = fingerprint(schema)
    return {'schema_digest': digest_value, 'probes': results}


def assess_candidate(path, reference_identity, reference_main_hash, reference_database, probes):
    """Classify one candidate path using only freshly observed filesystem and SQL evidence."""
    candidate = Path(path)
    evidence = {'label': path, 'path': str(candidate), 'resolved_path': str(candidate.resolve())}
    if not candidate.is_file():
        evidence.update(verdict=UNREADABLE, same_object=False, main_file_match=None,
                        schema_digest_match=None, representative_rows_visible=None,
                        sidecars=sidecars(candidate) if candidate.parent.exists() else {},
                        reason='path is not a regular file')
        return evidence
    identity = identity_token(candidate)
    main_hash = file_hash(candidate)
    # Snapshot sidecars before opening: a read-only SQLite open materializes
    # -wal/-shm files itself, which must not be mistaken for pre-existing evidence.
    present_sidecars = sidecars(candidate)
    evidence.update(identity=identity, main_file_sha256=main_hash, sidecars=present_sidecars)
    same_object = identity == reference_identity
    evidence['same_object'] = same_object
    evidence['main_file_match'] = main_hash == reference_main_hash
    try:
        observed = probe_database(candidate, probes)
    except (sqlite3.Error, OSError, Fault) as exc:
        evidence.update(verdict=UNREADABLE, schema_digest_match=None,
                        representative_rows_visible=None, reason=f'read-only open failed: {exc}')
        return evidence
    evidence['schema_digest'] = observed['schema_digest']
    evidence['schema_digest_match'] = observed['schema_digest'] == reference_database['schema_digest']
    evidence['probe_results'] = observed['probes']
    visible = all(item['found'] for item in observed['probes'])
    evidence['representative_rows_visible'] = visible
    if same_object:
        verdict, reason = SAME_OBJECT, 'device/inode identity resolves to the live file'
    elif visible:
        verdict, reason = SAME_DATABASE_COPY, (
            'separate file but the read-only snapshot contains every committed representative row')
    elif evidence['main_file_match']:
        verdict, reason = MAIN_FILE_ONLY, (
            'main database bytes match yet the committed representative rows are absent, '
            'so only the main file identity is proven')
    else:
        verdict, reason = DIVERGENT_DATABASE, (
            'main file differs and representative rows are missing despite a compatible schema')
    evidence.update(verdict=verdict, reason=reason)
    return evidence


def recommendation(verdict):
    if verdict == SAME_OBJECT:
        return 'register_alias'
    if verdict == SAME_DATABASE_COPY:
        return 'register_copy'
    return 'do_not_register'


def check(repository, body):
    """Produce the auditable onboarding baseline for the supplied candidate paths."""
    if not isinstance(body, dict) or set(body) != {'reference', 'candidates', 'probes'}:
        raise Fault('invalid_request', "Expected 'reference', 'candidates' and 'probes'", 400)
    reference_alias = body['reference']
    if not isinstance(reference_alias, str) or reference_alias not in repository.sources:
        raise Fault('unknown_source', 'Reference alias is not registered', 404)
    reference_path = repository.sources[reference_alias]
    candidates = body['candidates']
    if not isinstance(candidates, dict) or not candidates or len(candidates) > 32:
        raise Fault('invalid_request', 'Provide a non-empty label-to-path candidate map', 400)
    if any(not isinstance(label, str) or not isinstance(item, str) or not label or not item
           for label, item in candidates.items()):
        raise Fault('invalid_request', 'Each candidate needs a string label and path', 400)
    probes = normalize_probes(body['probes'])
    reference_identity = identity_token(reference_path)
    reference_main_hash = file_hash(reference_path)
    reference_database = probe_database(reference_path, probes)
    if not all(item['found'] for item in reference_database['probes']):
        raise Fault('reference_probe_missing', 'The registered source does not contain every probe row')
    findings = []
    for label, item in candidates.items():
        candidate_path = Path(item)
        evidence = assess_candidate(candidate_path, reference_identity, reference_main_hash,
                                    reference_database, probes)
        evidence['label'] = label
        evidence['onboardable'] = evidence['verdict'] in ONBOARDABLE
        evidence['recommendation'] = recommendation(evidence['verdict'])
        findings.append(evidence)
    baseline = {
        'reference': {
            'alias': reference_alias,
            'path': str(reference_path),
            'identity': reference_identity,
            'main_file_sha256': reference_main_hash,
            'schema_digest': reference_database['schema_digest'],
            'probe_results': reference_database['probes'],
        },
        'probes': [{'table': p['table'], 'key': p['key']} for p in probes],
        'candidates': findings,
    }
    baseline['baseline_fingerprint'] = digest(dumps({
        'reference_path': baseline['reference']['path'],
        'probes': baseline['probes'],
        'candidates': [{k: f[k] for k in ('label', 'resolved_path')} for f in findings],
    }).encode('ascii'))
    return baseline


def diff_evidence(before, after):
    keys = ('same_object', 'main_file_sha256', 'main_file_match', 'schema_digest_match',
            'representative_rows_visible', 'sidecars', 'identity')
    return {key: {'before': before.get(key), 'after': after.get(key)}
            for key in keys if before.get(key) != after.get(key)}


def recheck(repository, body):
    """Re-observe a saved baseline and state whether each earlier conclusion still holds."""
    if not isinstance(body, dict) or set(body) != {'baseline'}:
        raise Fault('invalid_request', "Expected a saved 'baseline' object", 400)
    baseline = body['baseline']
    if not isinstance(baseline, dict) or {'reference', 'probes', 'candidates'} - set(baseline):
        raise Fault('invalid_baseline', 'The supplied baseline is missing required fields')
    reference_alias = baseline['reference'].get('alias') if isinstance(baseline['reference'], dict) else None
    if not isinstance(reference_alias, str) or reference_alias not in repository.sources:
        raise Fault('unknown_source', 'Reference alias is not registered', 404)
    reference_path = repository.sources[reference_alias]
    probes = normalize_probes(baseline['probes'])
    reference_identity = identity_token(reference_path)
    reference_main_hash = file_hash(reference_path)
    reference_database = probe_database(reference_path, probes)
    current_paths = {item['label']: item['resolved_path'] for item in baseline['candidates']}
    findings = []
    for prior in baseline['candidates']:
        label = prior['label']
        path = current_paths[label]
        observed = assess_candidate(path, reference_identity, reference_main_hash,
                                    reference_database, probes)
        observed['label'] = label
        observed['onboardable'] = observed['verdict'] in ONBOARDABLE
        observed['recommendation'] = recommendation(observed['verdict'])
        changed = diff_evidence(prior, observed)
        observed['evidence_changes'] = changed
        observed['verdict_unchanged'] = prior['verdict'] == observed['verdict']
        observed['still_applies'] = observed['verdict_unchanged'] and not changed
        findings.append(observed)
    reference_changed = (
        baseline['reference']['identity'] != reference_identity
        or baseline['reference']['main_file_sha256'] != reference_main_hash
        or baseline['reference']['schema_digest'] != reference_database['schema_digest']
    )
    return {
        'baseline_fingerprint': baseline.get('baseline_fingerprint'),
        'reference': {
            'alias': reference_alias,
            'path': str(reference_path),
            'identity': reference_identity,
            'main_file_sha256': reference_main_hash,
            'schema_digest': reference_database['schema_digest'],
            'probe_results': reference_database['probes'],
            'changed_since_baseline': reference_changed,
        },
        'candidates': findings,
        'all_conclusions_hold': all(item['still_applies'] for item in findings),
    }

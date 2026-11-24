"""Shared helpers for the night-shift handover drill."""

import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path

from keelsplice import catalog
from keelsplice import capture as selection

HERE = Path(__file__).resolve().parent
SAMPLE = HERE / 'microscope.sql'
MATERIAL = HERE / 'handover-night' / 'material'
EXPECTED = HERE / 'handover-night' / 'expected-verdicts.json'

SNAPSHOTS = {
    '00-public-sample': '00-public-sample.sqlite',
    '01-after-value': '01-after-value.sqlite',
    '02-after-retarget': '02-after-retarget.sqlite',
}
PUBLIC = '00-public-sample'
AFTER_VALUE = '01-after-value'
AFTER_RETARGET = '02-after-retarget'

LABELS = ('attic-legacy', 'relay-early', 'relay-lost', 'relay-retry')
RELAY = 'relay'
ATTIC = 'attic'

RECOVER = 'recover'
CANDIDATE = 'candidate'
REREAD = 'reread'

EXPECTED_VERDICTS = {
    'relay-early': RECOVER,
    'relay-lost': RECOVER,
    'attic-legacy': RECOVER,
    'relay-retry': CANDIDATE,
}

# Work that has no persisted record and therefore cannot be recovered. These
# are requested-work entries, not captures; they are keyed by the work item.
REQUESTED = {
    'relay-retargeted-closure': (
        'The successor rehearsed relay v1 after S-02 moved to another substrate, but only a '
        'live preview exists. A preview is a read snapshot, not a record, and the source can '
        'move again; to obtain this sample the successor must read the source and seal it.'),
    'attic-fresh-sample': (
        'Recipe attic v1 is retired and refuses new seals. The old capture remains valid, but '
        'any new attic sample needs a fresh source read and a new recipe version.'),
}


class DrillError(RuntimeError):
    """A build or replay invariant did not hold."""


def require(condition, message):
    if not condition:
        raise DrillError(message)


def jdumps(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def sha_bytes(data):
    return hashlib.sha256(data).hexdigest()


def sha_file(path):
    return sha_bytes(Path(path).read_bytes())


def write_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2,
                                     sort_keys=True) + '\n', encoding='utf-8')


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def seed_source(path):
    with closing(sqlite3.connect(path)) as conn:
        conn.executescript(SAMPLE.read_text(encoding='utf-8'))


def mutate(path, statement, params=()):
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute(statement, params)


def snapshot(source, destination):
    """Persist one committed source state as a standalone replayable file."""
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(source)) as src, closing(sqlite3.connect(destination)) as dst:
        src.backup(dst)
    with closing(sqlite3.connect(destination)) as conn:
        conn.execute('PRAGMA journal_mode=DELETE')
        require([row[0] for row in conn.execute('PRAGMA integrity_check')] == ['ok'],
                f'snapshot {destination.name} failed integrity_check')


def readonly(path):
    return closing(sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True))


def calls(path, statement, params=()):
    with readonly(path) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(row) for row in conn.execute(statement, params)]


def sealed_from_snapshot(snapshot_path, recipe):
    """Recompute a sealed payload from a labelled source snapshot."""
    with readonly(snapshot_path) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA trusted_schema=OFF')
        disclosed = catalog.snapshot(conn)
        result = selection.collect(conn, disclosed['schema'], recipe)
    return {'schema': disclosed['schema'], 'schema_digest': disclosed['schema_digest'],
            'recipe': recipe, 'result': result}


def appearance(sealed):
    """The part of a capture a reader sees without opening rows."""
    result = sealed['result']
    counts = {table: len(rows) for table, rows in result['rows'].items()}
    return {'schema_digest': sealed['schema_digest'], 'counts': counts,
            'rows': sum(counts.values()), 'witnesses': len(result['arcs']),
            'roots': result['roots']}


def summarize(sealed):
    return selection.summarize(sealed['result']) | {'roots': sealed['result']['roots']}


def appearance_counts(payload):
    """Row counts as a reader sees them on a rehearsal page."""
    return payload['summary']['counts']


def assay_marks(sealed):
    return sorted(json.loads(token)[0] for token in sealed['result']['rows']['assay'])

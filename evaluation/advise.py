"""Per-capture advisory: measure the three review strategies on *this* capture.

No extrapolation and no fitted model: for the capture you point at, this script
executes each strategy against the real sealed capture and reports measured
median cost, storage premium, and a verdict derived from those measurements.

Strategies
----------
``sealed``   read the whole sealed capture per lookup (today's ``trace``)
``split``    keep the sealed capture but store evidence separately from payload
``index``    keep an independent derived evidence index

Usage
-----
    python3 -m evaluation.advise --repository state.sqlite --source s=src.sqlite --capture ID
    python3 -m evaluation.advise --fixture fanout:4500
    python3 -m evaluation.advise --fixture blob:1600

Exit code is 0 when the three paths agree on every sampled answer, else 2.
"""

from __future__ import annotations

import argparse
import gc
import json
import sqlite3
import statistics
import sys
import tempfile
import time
from contextlib import closing
from pathlib import Path

from keelsplice.codec import loads
from keelsplice.service import Workbench
from keelsplice.storage import Repository

from . import evidence_index, fixtures, split_sealed
from .harness import Reader, edge_ids

REPS = 11


def _median(fn, reps=REPS):
    fn()
    times, reads = [], []
    for _ in range(reps):
        gc.collect()  # keep earlier decoded captures from polluting the sample
        with Reader() as reader:
            fn()
        times.append(reader.elapsed)
        reads.append(reader.bytes)
    return {'median_s': statistics.median(times), 'median_read_bytes': statistics.median(reads)}


def _sealed_lookup(bench, capture_id, table, values):
    return bench.trace(capture_id, {'table': table, 'key': values})


def _index_lookup(path, schema, table, values):
    with closing(sqlite3.connect(path)) as conn:
        conn.row_factory = sqlite3.Row
        return evidence_index.lookup(conn, schema, {'table': table, 'key': values})


def _split_lookup(path, capture_id, table, values):
    with closing(sqlite3.connect(path)) as conn:
        return split_sealed.lookup(split_sealed.load(conn, capture_id), {'table': table, 'key': values})


def _sample_keys(sealed):
    """Leaf-first sample: a reviewed row is usually a narrow leaf, not the root."""
    rows = sealed['result']['rows']
    sample = []
    for table, tokens in rows.items():
        for token in tokens:
            address = (table, loads(token))
            sample.append(address)
    # Prefer addresses with the fewest direct causes: that is the "few rows"
    # review target the question is about.
    counts = {}
    for arc in sealed['result']['arcs']:
        counts[(arc['target'][0], json.dumps(arc['target'][1]))] = counts.get(
            (arc['target'][0], json.dumps(arc['target'][1])), 0) + 1
    sample.sort(key=lambda item: counts.get((item[0], json.dumps(item[1])), 0))
    return sample[:3]


def measure(sealed, bench, capture_id, workdir: Path):
    schema = sealed['schema']
    index_path = workdir / 'index.sqlite'
    split_path = workdir / 'split.sqlite'
    with closing(sqlite3.connect(index_path)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA journal_mode=OFF')
        conn.execute('PRAGMA synchronous=OFF')
        with Reader() as build:
            evidence_index.build(conn, sealed)
        index_bytes = evidence_index.storage_bytes(conn)
    with closing(sqlite3.connect(split_path)) as conn:
        conn.execute('PRAGMA journal_mode=OFF')
        conn.execute('PRAGMA synchronous=OFF')
        evidence_bytes, payload_bytes = split_sealed.store(conn, capture_id, sealed)

    samples = _sample_keys(sealed)
    equivalence = True
    per_key = []
    for table, values in samples:
        want = _sealed_lookup(bench, capture_id, table, values)
        got_index = _index_lookup(index_path, schema, table, values)
        got_split = _split_lookup(split_path, capture_id, table, values)
        equivalence = equivalence and want == got_index == got_split
        per_key.append({
            'key': [table, values],
            'causes': len(want['causes']),
            'sealed': _median(lambda: _sealed_lookup(bench, capture_id, table, values)),
            'split': _median(lambda: _split_lookup(split_path, capture_id, table, values)),
            'index': _median(lambda: _index_lookup(index_path, schema, table, values)),
        })

    with closing(sqlite3.connect(Path(bench.repository.path))) as conn:
        repo_bytes = conn.execute('PRAGMA page_count').fetchone()[0] * conn.execute('PRAGMA page_size').fetchone()[0]
    record = bench.repository.load_capture(capture_id)
    bench.build(capture_id)
    artifact = bench.repository.load_capture(capture_id, with_artifact=True)['artifact']

    return {
        'capture_id': capture_id,
        'rows': record['sealed']['result'] and sum(len(v) for v in record['sealed']['result']['rows'].values()),
        'witnesses': len(record['sealed']['result']['arcs']),
        'sealed_json_bytes': len(json.dumps(record['sealed'], ensure_ascii=True, sort_keys=True, separators=(',', ':')).encode()),
        'artifact_bytes': len(artifact),
        'repository_bytes': repo_bytes,
        'index_storage_bytes': index_bytes,
        'split_evidence_bytes': evidence_bytes,
        'split_payload_bytes': payload_bytes,
        'split_storage_bytes': split_path.stat().st_size,
        'index_build_s': build.elapsed,
        'equivalence': equivalence,
        'samples': per_key,
    }


def _wins(samples, name, baseline='sealed', margin=0.10):
    """Strictly cheaper than the baseline on every sample, with a margin.

    Ties are not wins: an alternative must cut the sampled read by at least
    ``margin`` to justify its storage and lifecycle cost.
    """
    for sample in samples:
        mine = sample[name]['median_read_bytes']
        base = sample[baseline]['median_read_bytes']
        if mine > base * (1 - margin):
            return False
        for other in ('sealed', 'split', 'index'):
            if other != name and sample[other]['median_read_bytes'] < mine:
                return False
    return True


def verdict(result):
    """Derive the recommendation from this capture's measured numbers.

    Two dimensions are reported separately and never averaged away:

    * per-lookup cost (bytes and time), measured on sampled keys;
    * storage cost, against the cost of keeping the sealed capture alone.

    ``split`` *replaces* the sealed store, so its premium is
    ``split_bytes - sealed_bytes``.  ``index`` *adds* to the sealed store
    (the sealed capture remains the authority for certify/download), so its
    premium is ``index_bytes`` in full.  Both an absolute and a relative
    figure are reported, because on a tiny capture the relative figure is
    dominated by SQLite's minimum page allocation.
    """
    samples = result['samples']
    sealed_bytes = result['sealed_json_bytes'] or 1
    delta = {
        'split': result['split_storage_bytes'] - sealed_bytes,
        'index': result['index_storage_bytes'],
    }
    premium = {name: value / sealed_bytes for name, value in delta.items()}
    cheapest = [name for name in ('split', 'index') if _wins(samples, name)]
    ceiling_bytes = 8 * 1024 * 1024
    affordable = [name for name in cheapest if delta[name] <= ceiling_bytes]
    if affordable:
        choice = min(affordable, key=lambda name: delta[name])
        reason = ('cuts every sampled read by at least 10% versus the whole-sealed path '
                  'and its added storage stays under 8 MiB')
    elif cheapest:
        choice = min(cheapest, key=lambda name: delta[name])
        reason = ('cuts every sampled read, but adds more than 8 MiB of storage; '
                  'treat as opt-in per capture')
    else:
        choice = 'sealed'
        reason = 'no alternative cuts every sampled read by 10%; keep reading the sealed capture'
    return {
        'choice': choice,
        'reason': reason,
        'cheapest_per_lookup': cheapest,
        'storage_delta_bytes': delta,
        'storage_premium': premium,
        'sealed_wins': not cheapest,
    }


def _fixture_from_arg(value):
    """Parse ``fanout:N``, ``wide:N``, ``microscope`` or ``blob:MIB``.

    ``blob:0.0625`` means 64 KiB; the unit is MiB so the sweep can touch small
    payloads without a second syntax.
    """
    kind, _, size = value.partition(':')
    if kind == 'microscope':
        return {'kind': 'microscope'}
    try:
        number = float(size or 0)
    except ValueError:
        raise SystemExit(f'fixture size must be a number, got {size!r}') from None
    if kind == 'fanout':
        return {'kind': 'fanout', 'probes': int(number) or 1000}
    if kind == 'blob':
        return {'kind': 'blob', 'rows': 3, 'payload_bytes': max(16 * 1024, int(number * 1024 * 1024))}
    if kind == 'wide':
        return {'kind': 'wide', 'roots': 40, 'per_root': int(number) or 100}
    raise SystemExit('fixture must be fanout:N, wide:N, microscope or blob:MIB')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repository')
    parser.add_argument('--source', action='append', default=[], metavar='ALIAS=FILE')
    parser.add_argument('--capture')
    parser.add_argument('--fixture')
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args(argv)
    with tempfile.TemporaryDirectory(prefix='keelsplice-advise-') as directory:
        workdir = Path(directory)
        if args.fixture:
            source = workdir / 'source.sqlite'
            fx = fixtures.seed(_fixture_from_arg(args.fixture), source)
            repo = Repository(workdir / 'repo.sqlite', {'s': source})
            bench = Workbench(repo)
            catalog = bench.catalog('s')
            bench.register('review', {'source': 's', 'roots': fx['roots'],
                                      'follow_children': edge_ids(catalog, fx['follow_children']),
                                      'row_limit': fx['row_limit']})
            capture_id = bench.capture({'name': 'review', 'version': 1})['id']
        else:
            if not (args.repository and args.capture):
                parser.error('give --fixture, or --repository with --capture')
            sources = dict(item.split('=', 1) for item in args.source)
            repo = Repository(args.repository, sources)
            bench = Workbench(repo)
            capture_id = args.capture
        result = measure(repo.load_capture(capture_id)['sealed'], bench, capture_id, workdir)
        result['verdict'] = verdict(result)
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print(f"capture {result['capture_id']}: {result['rows']} rows, {result['witnesses']} witnesses, "
                  f"sealed {result['sealed_json_bytes']} B, artifact {result['artifact_bytes']} B")
            print(f"  index storage {result['index_storage_bytes']} B "
                  f"({result['index_storage_bytes'] / result['sealed_json_bytes']:.0%} of sealed), "
                  f"build {result['index_build_s'] * 1e3:.1f} ms")
            print(f"  storage vs sealed: split {result['verdict']['storage_premium']['split']:+.0%}, "
                  f"index {result['verdict']['storage_premium']['index']:+.0%}")
            print(f"  split storage {result['split_storage_bytes']} B "
                  f"(evidence {result['split_evidence_bytes']} B + payload {result['split_payload_bytes']} B)")
            print('  sampled lookups (fewest causes first):')
            for sample in result['samples']:
                print(f"    {sample['key'][0]} {json.dumps(sample['key'][1], ensure_ascii=False)} "
                      f"causes={sample['causes']:<4} sealed={sample['sealed']['median_read_bytes']:>9}B/"
                      f"{sample['sealed']['median_s'] * 1e3:>7.2f}ms "
                      f"split={sample['split']['median_read_bytes']:>9}B/"
                      f"{sample['split']['median_s'] * 1e3:>7.2f}ms "
                      f"index={sample['index']['median_read_bytes']:>9}B/"
                      f"{sample['index']['median_s'] * 1e3:>7.2f}ms")
            v = result['verdict']
            print(f"  verdict: {v['choice']} — {v['reason']}")
            print(f"  all paths agree on every sampled answer: {result['equivalence']}")
    return 0 if result['equivalence'] else 2


if __name__ == '__main__':
    sys.exit(main())

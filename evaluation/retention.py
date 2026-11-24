"""Repository growth across repeated captures, builds and certifications.

Answers the storage half of the design question: what does keeping the sealed
capture (and, separately, a derived index) cost per capture, and how much of
the repository file is free-list churn rather than live data.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import tempfile
from contextlib import closing
from pathlib import Path

from keelsplice.service import Workbench
from keelsplice.storage import Repository

from . import fixtures
from .harness import edge_ids


def _repo_stats(path):
    with closing(sqlite3.connect(path)) as conn:
        page = conn.execute('PRAGMA page_size').fetchone()[0]
        pages = conn.execute('PRAGMA page_count').fetchone()[0]
        free = conn.execute('PRAGMA freelist_count').fetchone()[0]
        live = {row[0]: row[1] for row in conn.execute(
            'SELECT name, COALESCE(SUM(pgsize), 0) FROM dbstat GROUP BY name')}
    return {'file_bytes': pages * page, 'freelist_bytes': free * page, 'live': live}


def run_case(spec, root: Path, captures: int):
    source = root / 'source.sqlite'
    repo_path = root / 'repo.sqlite'
    fx = fixtures.seed(spec, source)
    bench = Workbench(Repository(repo_path, {'s': source}))
    catalog = bench.catalog('s')
    body = {'source': 's', 'roots': fx['roots'],
            'follow_children': edge_ids(catalog, fx['follow_children']),
            'row_limit': fx['row_limit']}
    bench.register('review', body)
    steps = []
    for number in range(1, captures + 1):
        capture = bench.capture({'name': 'review', 'version': 1})
        bench.build(capture['id'])
        bench.certify(capture['id'])
        stats = _repo_stats(repo_path)
        steps.append({'capture': number, **stats})
    with closing(sqlite3.connect(repo_path)) as conn:
        conn.execute('VACUUM')
    after_vacuum = _repo_stats(repo_path)
    return {'name': fx['name'], 'class': fx['class'],
            'sealed_json_bytes': len(json.dumps(bench.repository.load_capture(
                capture['id'])['sealed'], ensure_ascii=True, sort_keys=True, separators=(',', ':'))),
            'steps': steps, 'after_vacuum': after_vacuum}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--captures', type=int, default=5)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args(argv)
    specs = [{'kind': 'fanout', 'probes': 4500},
             {'kind': 'blob', 'rows': 3, 'payload_bytes': 1600 * 1024}]
    cases = []
    with tempfile.TemporaryDirectory(prefix='keelsplice-retention-') as directory:
        for number, spec in enumerate(specs):
            workdir = Path(directory) / f'{number:02d}'
            workdir.mkdir()
            case = run_case(spec, workdir, args.captures)
            cases.append(case)
            print(f"{case['name']}: sealed={case['sealed_json_bytes']}B")
            for step in case['steps']:
                print(f"  capture {step['capture']}: file={step['file_bytes']:>10} "
                      f"freelist={step['freelist_bytes']:>10} captures_table={step['live'].get('captures', 0):>10}")
            print(f"  after VACUUM: file={case['after_vacuum']['file_bytes']:>10}")
    if args.output:
        args.output.write_text(json.dumps({'generated_by': 'evaluation/retention.py',
                                           'captures_per_case': args.captures, 'cases': cases},
                                          ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        print(f'wrote {args.output}')
    return 0


if __name__ == '__main__':
    sys.exit(main())

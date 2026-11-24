"""Run the evidence-access evaluation suite and write machine-readable results."""

from __future__ import annotations

import argparse
import json
import platform
import sqlite3
import sys
import tempfile
from pathlib import Path

from . import fixtures
from .harness import run_case


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--suite', choices=('default', 'quick', 'sweep'), default='default')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args(argv)
    specs = {'default': fixtures.DEFAULT_SUITE, 'quick': fixtures.QUICK_SUITE,
             'sweep': fixtures.SWEEP_SUITE}[args.suite]
    results = []
    with tempfile.TemporaryDirectory(prefix='keelsplice-eval-') as directory:
        root = Path(directory)
        for number, spec in enumerate(specs):
            workdir = root / f'{number:02d}'
            workdir.mkdir()
            result = run_case(spec, workdir)
            results.append(result)
            _print_line(result)
    report = {
        'generated_by': 'evaluation/run.py',
        'platform': {'python': platform.python_version(), 'sqlite': sqlite3.sqlite_version,
                     'machine': platform.machine(), 'system': platform.system(),
                     'cpu_count': __import__('os').cpu_count()},
        'suite': args.suite,
        'cases': results,
    }
    if args.output:
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        print(f'wrote {args.output}')
    return 0


def _print_line(result):
    if 'rejected' in result:
        print(f'{result["name"]:<18} rejected: {result["rejected"]}')
        return
    full, split, index = result['lookup_full'], result['lookup_split'], result['lookup_index']
    detail_full, detail_index = result['detail_full'], result['detail_index']
    print(f'{result["name"]:<14} rows={result["rows"]:<5} arcs={result["witnesses"]:<6} '
          f'sealed={result["sealed_json_bytes"]:>9}B index={result["index_storage_bytes"]:>8}B '
          f'lookup[sealed/split/index]={full["median_read_bytes"]:>9.0f}/'
          f'{split["median_read_bytes"]:>9.0f}/{index["median_read_bytes"]:>7.0f}B '
          f'detail[sealed/index]={detail_full["median_read_bytes"]:>9.0f}/'
          f'{detail_index["median_read_bytes"]:>7.0f}B')


if __name__ == '__main__':
    sys.exit(main())

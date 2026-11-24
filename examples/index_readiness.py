"""Re-run the index-readiness check against two disposable source shapes.

Shape A is a NOCASE parent key reached by a matching partial child index, next to
namesake indexes that share column names but cannot serve the reads.  Shape B has
several roots that resolve to the same parent key.  For each shape the walkthrough
registers a recipe through the real HTTP service, asks for its readiness report,
and checks that the report separates a genuine plan change, a real change in
measured work, and the relationship closure -- and that the source file is
byte-identical afterwards, because nothing is ever installed in it.
"""

import json
import sqlite3
import sys
from contextlib import closing
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from keelsplice.api import create_server

WORKFLOWS = ('shape-a', 'shape-b')

SHAPES = {
    'shape-a': (Path(__file__).with_name('index_ready_a.sql'),
                {'source': 'a',
                 'roots': [{'table': 'seam', 'key': ['east', 'MICA']}],
                 'follow_children': [{'child': 'spline', 'id': 0}],
                 'row_limit': 5000}),
    'shape-b': (Path(__file__).with_name('index_ready_b.sql'),
                {'source': 'b',
                 'roots': [{'table': 'leaf_node', 'key': [tag]} for tag in ('LEAF-0', 'LEAF-1', 'LEAF-2')],
                 'follow_children': [],
                 'row_limit': 100}),
}


def seed(file, shape='shape-a'):
    sql, _ = SHAPES[shape]
    with closing(sqlite3.connect(file)) as conn:
        conn.executescript(sql.read_text())


def call(base, route, body=None, status=200):
    headers = {'Content-Type': 'application/json'}
    data = None if body is None else json.dumps(body).encode('utf-8')
    try:
        response = urlopen(Request(base + route, data, headers), timeout=30)
    except HTTPError as exc:
        response = exc
    with response:
        raw = response.read()
        assert response.status == status, (route, response.status, raw[:500])
        return json.loads(raw)


def read(report, kind, table):
    return next(item for item in report['reads'] if item['kind'] == kind and item['table'] == table)


def index(read, name):
    return next(item for item in read['indexes'] if item['name'] == name)


def check_shape_a(base, source):
    before = sha256(source.read_bytes()).hexdigest()
    _, body = SHAPES['shape-a']
    call(base, '/recipes/a/versions', body, 201)
    report = call(base, '/recipes/a/versions/1/readiness', {})
    assert sha256(source.read_bytes()).hexdigest() == before, 'the source file must not change'

    child = read(report, 'child', 'spline')
    match = index(child, 'spline_match_nocase')
    binary = index(child, 'spline_namesake_binary')
    reversed_ = index(child, 'spline_namesake_reversed')

    # The child fan-out never gets a keyed seek -- the engine's unary plus makes
    # the right side an expression -- so the matching partial index can only
    # reduce work, and it really does.
    assert child['natural']['shape'] == 'scan' and not child['natural']['keyed']
    assert match['verdict'] == 'less_work' and match['work_delta'] == 'less'
    assert match['chosen'] is True
    # The namesakes share the exact column names yet make the same read cost more.
    for namesake in (binary, reversed_):
        assert namesake['verdict'] == 'costs' and namesake['work_delta'] == 'more'
        assert set(namesake['columns']) == set(match['columns'])

    # The parent key lookup is a genuine seek the BINARY namesake cannot match.
    parent = read(report, 'parent', 'seam')
    assert parent['natural']['shape'] == 'seek'
    assert index(parent, 'seam_namesake_binary')['verdict'] == 'costs'

    # Several reasons for the same read are grouped, not duplicated.
    root = read(report, 'root', 'seam')
    assert len(root['via']) == 1

    # The closure is reported on its own and is not an index verdict.
    assert report['closure']['summary']['counts']['spline'] == 2000
    assert report['summary']['less_work'] >= 1 and report['summary']['costs'] >= 1
    return report


def check_shape_b(base, source):
    before = sha256(source.read_bytes()).hexdigest()
    _, body = SHAPES['shape-b']
    call(base, '/recipes/b/versions', body, 201)
    report = call(base, '/recipes/b/versions/1/readiness', {})
    assert sha256(source.read_bytes()).hexdigest() == before

    root = read(report, 'root', 'leaf_node')
    # Three roots that share one key collapse into a single described read.
    assert len(root['via']) == 3 and root['natural']['shape'] == 'seek'
    # The partial child index is refused for a primary-key lookup: SQLite will not
    # silently use it and drop rows.
    assert index(root, 'leaf_region_code')['verdict'] == 'unusable'
    # The primary key itself is what serves this read.
    assert index(root, 'sqlite_autoindex_leaf_node_1')['verdict'] == 'seeks'

    parent = read(report, 'parent', 'parent_node')
    assert parent['natural']['shape'] == 'seek'
    assert index(parent, 'parent_namesake_binary')['verdict'] == 'costs'

    # All roots hit one parent row, so the closure has a single parent.
    assert report['closure']['summary']['counts'] == {'leaf_node': 3, 'parent_node': 1}
    assert report['closure']['summary']['root_occurrences'] == 3
    return report


def walkthrough(base, sources):
    """Run both shapes through the live service; each check returns its report."""
    return {shape: (check_shape_a if shape == 'shape-a' else check_shape_b)(base, sources[shape[-1]])
            for shape in WORKFLOWS}


def main():
    selected = sys.argv[1] if len(sys.argv) == 2 else 'all'
    if selected not in (*WORKFLOWS, 'all'):
        raise SystemExit('Choose a shape: ' + ', '.join(WORKFLOWS))
    with TemporaryDirectory(prefix='keelsplice-index-ready-') as directory:
        root = Path(directory)
        sources = {}
        for shape in WORKFLOWS:
            sources[shape[-1]] = root / f'{shape}.sqlite'
            seed(sources[shape[-1]], shape)
        with create_server(('127.0.0.1', 0), root / 'repo.sqlite', sources) as server:
            thread = Thread(target=server.serve_forever, kwargs={'poll_interval': 0.05})
            thread.start()
            try:
                base = f'http://127.0.0.1:{server.server_port}'
                reports = walkthrough(base, sources)
                for shape in WORKFLOWS:
                    if selected in (shape, 'all'):
                        print(shape, json.dumps(reports[shape], sort_keys=True))
                if selected == 'all':
                    print('索引就绪检查通过：两种源形态、真实计划变化、实际工作量与关系闭包分开报告，源文件未被改动')
            finally:
                server.shutdown()
                thread.join()


if __name__ == '__main__':
    main()

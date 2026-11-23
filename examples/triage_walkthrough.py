"""Verify batch root triage and the all-or-nothing registration guarantee.

The checks exercise, against the actual microscope schema and a live server:

* one batch containing roots that can be kept, roots that need correction
  (truncated composite key on substrate, malformed BLOB encoding on the
  texture column family, a missing row) and roots that cannot be judged yet
  (a table absent from the catalog);
* an edge on a table whose foreign-key number is not declared;
* repairing the roots and registering the whole recipe against the real
  schema, proving no previously passing fragment was saved on its own;
* rebuilding the source DDL so the same edge number is reassigned to a
  different parent table, then re-checking and attempting the stale
  registration, which must be rejected without creating a version.
"""

import json
import sqlite3
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread

from keelsplice.api import create_server

from .walkthrough import call, seed

REBUILT_DDL = """
PRAGMA foreign_keys=ON;
CREATE TABLE substrate (
    region TEXT COLLATE NOCASE NOT NULL,
    code TEXT COLLATE NOCASE NOT NULL,
    texture BLOB NOT NULL,
    PRIMARY KEY (region, code)
) WITHOUT ROWID;
CREATE TABLE cassette (
    serial INTEGER PRIMARY KEY,
    prior INTEGER REFERENCES cassette,
    label TEXT NOT NULL
);
CREATE TABLE assay (
    mark TEXT PRIMARY KEY NOT NULL,
    region TEXT,
    code TEXT,
    cassette INTEGER NOT NULL,
    length_um REAL NOT NULL,
    doubled REAL GENERATED ALWAYS AS (length_um * 2) STORED,
    FOREIGN KEY (region, code) REFERENCES substrate,
    FOREIGN KEY (cassette) REFERENCES cassette
);
-- The rebuilt DDL reassigns foreign-key number 0: in the original schema the
-- composite substrate reference was key 0 and the inline cassette reference
-- was key 1. Here cassette is key 0 and substrate is key 1.
CREATE TABLE frame (
    frame_id INTEGER PRIMARY KEY,
    mark TEXT REFERENCES assay,
    note TEXT NOT NULL
);
CREATE INDEX assay_substrate_lookup ON assay(region,code) WHERE code IS NOT NULL;
CREATE INDEX frame_mark_lookup ON frame(mark);
BEGIN;
PRAGMA defer_foreign_keys=ON;
INSERT INTO substrate VALUES ('East','Mica',x'0000ff'), ('West','Basalt',x'fe');
INSERT INTO cassette VALUES (11,12,'opal'), (12,11,'agate'), (13,NULL,'garnet');
INSERT INTO assay(mark,region,code,cassette,length_um) VALUES
    ('S-01','east','MICA',11,3.25), ('S-02','EAST','mica',12,5.5),
    ('S-03','West',NULL,13,2.5), ('S-04','west','basalt',13,9.0);
INSERT INTO frame VALUES (101,'S-01','front'), (102,'S-02','detail'), (103,'S-02','side'), (104,NULL,'blank');
COMMIT;
"""


def index_results(report, kind):
    return {item['index']: item for item in report[kind]}


def rebuild_source(source):
    with closing(sqlite3.connect(source)) as conn:
        with conn:
            conn.execute('PRAGMA foreign_keys=OFF')
            conn.execute('DROP TABLE frame')
            conn.execute('DROP TABLE assay')
            conn.execute('DROP TABLE cassette')
            conn.execute('DROP TABLE substrate')
        conn.executescript(REBUILT_DDL)


def run(base, source):
    draft_roots = [
        {'table': 'substrate', 'key': ['east', 'MICA']},          # 0 keep, respelled to East/Mica
        {'table': 'substrate', 'key': ['east']},                  # 1 correct: truncated composite key
        {'table': 'substrate', 'key': ['East', {'blob': 'zzz'}]},# 2 correct: malformed BLOB on key member
        {'table': 'cassette', 'key': [999]},                      # 3 correct: row does not exist
        {'table': 'ghost_slide', 'key': [1]},                     # 4 defer: table not in catalog
    ]
    body = {'source': 'microscope', 'roots': draft_roots,
            'follow_children': [{'child': 'frame', 'id': 7}]}

    report = call(base, '/recipe-check', body)
    assert report['ready'] is False
    assert report['statuses'] == {'keep': 1, 'correct': 3, 'defer': 2}
    roots = index_results(report, 'roots')
    assert roots[0]['status'] == 'keep' and roots[0]['respelled'] is True
    assert roots[0]['actual'] == ['substrate', ['East', 'Mica']]
    assert roots[1]['status'] == 'correct' and roots[1]['code'] == 'invalid_key'
    assert roots[2]['status'] == 'correct' and roots[2]['code'] == 'invalid_value'
    assert roots[3]['status'] == 'correct' and roots[3]['code'] == 'missing_root'
    assert roots[4]['status'] == 'defer' and roots[4]['code'] == 'unknown_identifier'
    edges = index_results(report, 'edges')
    assert edges[0]['status'] == 'defer' and edges[0]['code'] == 'invalid_edge'
    assert edges[0]['available'] == [0]
    print('triage', json.dumps(report['statuses']),
          {i: item['status'] for i, item in roots.items()},
          {0: edges[0]['status']})

    # The failed whole draft must not leave any partial recipe behind.
    assert call(base, '/recipes/batch-swatch/versions/1', status=404)['error'] == 'unknown_recipe'

    # A still-invalid whole draft is rejected on the real registration path.
    rejected = call(base, '/recipes/batch-swatch/versions', body, 422)
    assert rejected['error'] in {'invalid_edge', 'invalid_key', 'invalid_value', 'unknown_identifier'}
    assert call(base, '/recipes/batch-swatch/versions/1', status=404)['error'] == 'unknown_recipe'

    # Roots-only draft reaches the root validation and is rejected for the truncated tuple.
    root_only = dict(body, follow_children=[])
    assert call(base, '/recipes/batch-swatch/versions', root_only, 422)['error'] == 'invalid_key'
    assert call(base, '/recipes/batch-swatch/versions/1', status=404)['error'] == 'unknown_recipe'

    # Repair every item using the actual same schema, then register the whole recipe.
    repaired = {'source': 'microscope',
                'roots': [draft_roots[0], {'table': 'cassette', 'key': [11]}],
                'follow_children': [{'child': 'assay', 'id': 0, 'parent': 'substrate'}],
                'row_limit': 20}
    clean = call(base, '/recipe-check', repaired)
    assert clean['ready'] is True and clean['statuses']['keep'] == 3
    version = call(base, '/recipes/batch-swatch/versions', repaired, 201)
    assert version['version'] == 1
    preview = call(base, '/preview', {'name': 'batch-swatch', 'version': 1})
    assert preview['roots'] == [['substrate', ['East', 'Mica']], ['cassette', [11]]]

    # Rebuild the source so assay key 0 now points at cassette instead of substrate.
    rebuild_source(source)
    catalog = call(base, '/sources/microscope/catalog')
    current = {edge['id']: edge['parent'] for edge in catalog['schema']['edges'] if edge['child'] == 'assay'}
    assert current == {0: 'cassette', 1: 'substrate'}

    recheck = call(base, '/recipe-check', repaired)
    again = index_results(recheck, 'edges')
    assert recheck['ready'] is False
    assert again[0]['status'] == 'defer' and again[0]['code'] == 'edge_reassigned'
    assert again[0]['parent'] == 'cassette' and again[0]['expected_parent'] == 'substrate'
    roots = index_results(recheck, 'roots')
    assert roots[0]['status'] == 'keep' and roots[1]['status'] == 'keep'

    # Registering with the stale parent expectation fails and creates no version.
    stale = call(base, '/recipes/batch-swatch/versions', repaired, 422)
    assert stale['error'] == 'edge_reassigned'
    assert call(base, '/recipes/batch-swatch/versions/2', status=404)['error'] == 'unknown_recipe'

    # Re-choosing against the live catalog and registering the whole recipe works.
    updated = dict(repaired, follow_children=[{'child': 'assay', 'id': 0, 'parent': 'cassette'}])
    second = call(base, '/recipes/batch-swatch/versions', updated, 201)
    assert second['version'] == 2
    sealed = call(base, '/captures', {'name': 'batch-swatch', 'version': 2}, 201)
    # Key 0 now walks cassette -> assay, so only assays attached to the captured
    # cassette (and its parent cascade) enter the closure.
    assert sealed['summary']['counts'] == {'substrate': 1, 'cassette': 2, 'assay': 2, 'frame': 0}
    print('reassignment', json.dumps(again[0]), 'sealed', sealed['summary']['counts'])


def main():
    with TemporaryDirectory(prefix='keelsplice-triage-') as directory:
        root = Path(directory)
        source = root / 'microscope.sqlite'
        seed(source)
        with create_server(('127.0.0.1', 0), root / 'repo.sqlite', {'microscope': source}) as server:
            thread = Thread(target=server.serve_forever, kwargs={'poll_interval': 0.05})
            thread.start()
            try:
                run(f'http://127.0.0.1:{server.server_port}', source)
            finally:
                server.shutdown()
                thread.join()
    print('批量核对与整份配方登记检查通过：三类判定、坏 BLOB、未确认关系、同编号改指与无部分保存')


if __name__ == '__main__':
    main()

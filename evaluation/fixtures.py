"""Deterministic source databases representing evidence-review input classes.

Two axes matter for the design question:

* witness fan-out: one capture with tens of thousands of relationship arcs but
  few rows per lookup result;
* payload bulk: very large BLOB rows with only a handful of arcs.

The fixtures keep the same declared-key / foreign-key semantics the workbench
already requires, so captures go through the real service rather than a
shortcut.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

MICROSCOPE_SCHEMA = """
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
    cassette INTEGER NOT NULL REFERENCES cassette,
    length_um REAL NOT NULL,
    doubled REAL GENERATED ALWAYS AS (length_um * 2) STORED,
    FOREIGN KEY (region, code) REFERENCES substrate
);
CREATE TABLE frame (
    frame_id INTEGER PRIMARY KEY,
    mark TEXT REFERENCES assay,
    note TEXT NOT NULL
);
CREATE INDEX assay_substrate_lookup ON assay(region,code) WHERE code IS NOT NULL;
CREATE INDEX frame_mark_lookup ON frame(mark);
"""

FANOUT_SCHEMA = """
CREATE TABLE substrate (
    region TEXT COLLATE NOCASE NOT NULL,
    code TEXT COLLATE NOCASE NOT NULL,
    note TEXT NOT NULL,
    PRIMARY KEY (region, code)
) WITHOUT ROWID;
CREATE TABLE dim (
    id INTEGER PRIMARY KEY,
    label TEXT NOT NULL
);
CREATE TABLE probe (
    id INTEGER PRIMARY KEY,
    region TEXT,
    code TEXT,
    link0 INTEGER REFERENCES dim(id),
    link1 INTEGER REFERENCES dim(id),
    link2 INTEGER REFERENCES dim(id),
    link3 INTEGER REFERENCES dim(id),
    link4 INTEGER REFERENCES dim(id),
    link5 INTEGER REFERENCES dim(id),
    link6 INTEGER REFERENCES dim(id),
    link7 INTEGER REFERENCES dim(id),
    FOREIGN KEY (region, code) REFERENCES substrate
);
"""

BLOB_SCHEMA = """
CREATE TABLE substrate (
    region TEXT COLLATE NOCASE NOT NULL,
    code TEXT COLLATE NOCASE NOT NULL,
    payload BLOB NOT NULL,
    PRIMARY KEY (region, code)
) WITHOUT ROWID;
CREATE TABLE assay (
    mark TEXT PRIMARY KEY NOT NULL,
    region TEXT,
    code TEXT,
    length_um REAL NOT NULL,
    FOREIGN KEY (region, code) REFERENCES substrate
);
CREATE TABLE frame (
    frame_id INTEGER PRIMARY KEY,
    mark TEXT REFERENCES assay,
    note TEXT NOT NULL
);
"""


def _execute(conn, script, statements=()):
    conn.executescript(script)
    conn.execute('BEGIN')
    for sql, params in statements:
        conn.execute(sql, params)
    conn.commit()


def microscope(path: Path) -> dict:
    """The shipped sample: a tiny capture used as the crossover control."""
    with closing(sqlite3.connect(path)) as conn:
        _execute(conn, MICROSCOPE_SCHEMA, (
            ("INSERT INTO substrate VALUES ('East','Mica',x'0000ff')", ()),
            ("INSERT INTO substrate VALUES ('West','Basalt',x'fe')", ()),
            ("INSERT INTO cassette VALUES (11,12,'opal')", ()),
            ("INSERT INTO cassette VALUES (12,11,'agate')", ()),
            ("INSERT INTO cassette VALUES (13,NULL,'garnet')", ()),
            ("INSERT INTO assay(mark,region,code,cassette,length_um) VALUES ('S-01','east','MICA',11,3.25)", ()),
            ("INSERT INTO assay(mark,region,code,cassette,length_um) VALUES ('S-02','EAST','mica',12,5.5)", ()),
            ("INSERT INTO assay(mark,region,code,cassette,length_um) VALUES ('S-03','West',NULL,13,2.5)", ()),
            ("INSERT INTO assay(mark,region,code,cassette,length_um) VALUES ('S-04','west','basalt',13,9.0)", ()),
            ("INSERT INTO frame VALUES (101,'S-01','front')", ()),
            ("INSERT INTO frame VALUES (102,'S-02','detail')", ()),
            ("INSERT INTO frame VALUES (103,'S-02','side')", ()),
            ("INSERT INTO frame VALUES (104,NULL,'blank')", ()),
        ))
    return {
        'name': 'microscope',
        'class': 'small control',
        'description': '5 selected rows, 4 witnesses; index overhead check',
        'roots': [{'table': 'substrate', 'key': ['east', 'MICA']}],
        'follow_children': [{'child': 'assay', 'parent': 'substrate'}],
        'row_limit': 100,
        'lookups': [('substrate', ['East', 'Mica']), ('assay', ['S-01']), ('cassette', [11])],
        'review': [('substrate', ['East', 'Mica']), ('assay', ['S-01']), ('assay', ['S-02'])],
        'missing': ('substrate', ['East', 'Nope']),
        'session': [('substrate', ['East', 'Mica']), ('assay', ['S-01']), ('assay', ['S-02'])],
    }


def fanout(path: Path, probes: int) -> dict:
    """One root fans out to many child rows; each child has eight FK columns.

    ``probes`` child rows and eight parent links per child yield roughly
    ``10 * probes`` witnesses while every single-row lookup result stays small.
    """
    dims = 8
    statements = [("INSERT INTO substrate VALUES ('east','mica','fanout root')", ())]
    statements += [(f'INSERT INTO dim VALUES (?,?)', (i, f'dim-{i}')) for i in range(1, dims + 1)]
    statements += [
        (
            'INSERT INTO probe(id,region,code,link0,link1,link2,link3,link4,link5,link6,link7) '
            'VALUES (?,?,?,?,?,?,?,?,?,?,?)',
            (i, 'east', 'mica', *(1 + (i + j) % dims for j in range(dims))),
        )
        for i in range(1, probes + 1)
    ]
    with closing(sqlite3.connect(path)) as conn:
        _execute(conn, FANOUT_SCHEMA, statements)
    return {
        'name': f'fanout-{probes}',
        'class': 'high fan-out',
        'description': f'{probes} child rows, ~{10 * probes} witnesses, ~{probes + dims + 1} rows',
        'roots': [{'table': 'substrate', 'key': ['east', 'mica']}],
        'follow_children': [{'child': 'probe', 'parent': 'substrate'}],
        'row_limit': min(5000, probes + dims + 2),
        'lookups': [('probe', [1]), ('probe', [probes // 2 or 1]), ('probe', [probes]), ('substrate', ['east', 'mica'])],
        'missing': ('probe', [probes + 1000]),
        'session': [('probe', [i]) for i in range(1, min(probes, 20) + 1)],
    }


def blob(path: Path, rows: int, payload_bytes: int) -> dict:
    """A few rows carrying large BLOBs and almost no relationship witnesses."""
    statements = []
    for i in range(1, rows + 1):
        payload = bytes(((i * 37 + j) % 256 for j in range(16))) * (payload_bytes // 16)
        statements.append(('INSERT INTO substrate VALUES (?,?,?)', (f'region-{i}', f'code-{i}', payload)))
        statements.append(('INSERT INTO assay(mark,region,code,length_um) VALUES (?,?,?,?)',
                           (f'B-{i:02d}', f'region-{i}', f'code-{i}', float(i))))
    statements.append(("INSERT INTO frame VALUES (1,'B-01','only frame')", ()))
    with closing(sqlite3.connect(path)) as conn:
        _execute(conn, BLOB_SCHEMA, statements)
    return {
        'name': f'blob-{rows}x{payload_bytes // 1024}k',
        'class': 'large BLOB rows, few edges',
        'description': f'{rows} BLOB rows of {payload_bytes // 1024} KiB; {2 * rows} witnesses',
        'roots': [{'table': 'substrate', 'key': [f'region-{i}', f'code-{i}']} for i in range(1, rows + 1)],
        'follow_children': [{'child': 'assay', 'parent': 'substrate'}],
        'row_limit': 100,
        'lookups': [('substrate', ['region-1', 'code-1']), ('assay', ['B-01']), ('assay', [f'B-{rows:02d}'])],
        'missing': ('substrate', ['region-1', 'missing']),
        'session': [('substrate', ['region-1', 'code-1']), ('assay', ['B-01']),
                    ('substrate', [f'region-{rows}', f'code-{rows}']), ('assay', [f'B-{rows:02d}'])],
    }


def wide(path: Path, roots: int, per_root: int) -> dict:
    """Many selected rows with one witness each, not ten.

    Contrasts with ``fanout``: the same row count produces a tenth of the
    witnesses, isolating identity cost from witness cost.
    """
    total = roots * per_root
    statements = [('INSERT INTO substrate VALUES (?,?,?)', (f'r{i}', f'c{i}', 'x')) for i in range(roots)]
    statements += [('INSERT INTO item VALUES (?,?,?,?)', (i, f'r{(i - 1) // per_root}', f'c{(i - 1) // per_root}', 'y'))
                   for i in range(1, total + 1)]
    with closing(sqlite3.connect(path)) as conn:
        _execute(conn, '''
            CREATE TABLE substrate (
                region TEXT COLLATE NOCASE NOT NULL,
                code TEXT COLLATE NOCASE NOT NULL,
                note TEXT NOT NULL,
                PRIMARY KEY (region, code)
            ) WITHOUT ROWID;
            CREATE TABLE item (
                item_id INTEGER PRIMARY KEY,
                region TEXT,
                code TEXT,
                note TEXT NOT NULL,
                FOREIGN KEY (region, code) REFERENCES substrate
            );
        ''', statements)
    return {
        'name': f'wide-{total}',
        'class': 'many rows, one witness each',
        'description': f'{roots} roots fan out to {total} items; {total} witnesses',
        'roots': [{'table': 'substrate', 'key': [f'r{i}', f'c{i}']} for i in range(roots)],
        'follow_children': [{'child': 'item', 'parent': 'substrate'}],
        'row_limit': 5000,
        'lookups': [('substrate', ['r0', 'c0']), ('item', [1]),
                    ('item', [total // 2 or 1]), ('item', [total])],
        'missing': ('item', [total + 1]),
        'session': [('substrate', [f'r{i}', f'c{i}']) for i in range(min(roots, 10))]
                   + [('item', [i]) for i in range(1, 11)],
    }


def seed(spec: dict, path: Path) -> dict:
    """Materialize a fixture spec and return it."""
    kind = spec['kind']
    if kind == 'microscope':
        return microscope(path)
    if kind == 'fanout':
        return fanout(path, spec['probes'])
    if kind == 'blob':
        return blob(path, spec['rows'], spec['payload_bytes'])
    if kind == 'wide':
        return wide(path, spec['roots'], spec['per_root'])
    raise ValueError(kind)


DEFAULT_SUITE = [
    {'kind': 'microscope'},
    {'kind': 'wide', 'roots': 20, 'per_root': 50},
    {'kind': 'wide', 'roots': 40, 'per_root': 100},
    {'kind': 'fanout', 'probes': 250},
    {'kind': 'fanout', 'probes': 1000},
    {'kind': 'fanout', 'probes': 4500},
    {'kind': 'blob', 'rows': 3, 'payload_bytes': 256 * 1024},
    {'kind': 'blob', 'rows': 3, 'payload_bytes': 1024 * 1024},
    {'kind': 'blob', 'rows': 3, 'payload_bytes': 1600 * 1024},
]

# Scaling sweep: many points along each axis, so no conclusion rests on one
# small sample.  Sizes stay inside the product's own row/byte budgets.
SWEEP_SUITE = [
    {'kind': 'microscope'},
    *({'kind': 'fanout', 'probes': n} for n in (10, 25, 50, 100, 250, 500, 1000, 2000, 4500)),
    *({'kind': 'wide', 'roots': 20, 'per_root': n} for n in (5, 10, 25, 50, 100)),
    *({'kind': 'blob', 'rows': 1, 'payload_bytes': n} for n in (16 * 1024, 64 * 1024, 256 * 1024,
                                                                 512 * 1024, 1024 * 1024, 1600 * 1024)),
]

QUICK_SUITE = [
    {'kind': 'microscope'},
    {'kind': 'fanout', 'probes': 1000},
    {'kind': 'blob', 'rows': 2, 'payload_bytes': 1024 * 1024},
]

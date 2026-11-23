"""Exercise the cross-reference page against schema drift and retained captures.

Run: python3 -m examples.lineage_walkthrough
"""

import json
import sqlite3
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

from keelsplice.api import create_server

DROP = '''
PRAGMA foreign_keys=OFF;
DROP TABLE IF EXISTS frame;
DROP TABLE IF EXISTS assay;
DROP TABLE IF EXISTS provenance;
DROP TABLE IF EXISTS cassette;
DROP TABLE IF EXISTS substrate;
'''

# Two versions of one source.  The second rebuilds ``assay`` so that SQLite
# renumbers the substrate composite key from 0 to 1 and gives 0 to a new
# ``provenance`` reference.  It also widens the substrate reference to include
# an extra column, which must be reported as a changed definition rather than a
# match.
V1 = '''
CREATE TABLE substrate (region TEXT COLLATE NOCASE NOT NULL, code TEXT COLLATE NOCASE NOT NULL,
    texture BLOB NOT NULL, PRIMARY KEY (region, code)) WITHOUT ROWID;
CREATE TABLE cassette (serial INTEGER PRIMARY KEY, prior INTEGER REFERENCES cassette, label TEXT NOT NULL);
CREATE TABLE assay (mark TEXT PRIMARY KEY NOT NULL, region TEXT, code TEXT,
    cassette INTEGER NOT NULL REFERENCES cassette, length_um REAL NOT NULL,
    FOREIGN KEY (region, code) REFERENCES substrate);
CREATE TABLE frame (frame_id INTEGER PRIMARY KEY, mark TEXT REFERENCES assay, note TEXT NOT NULL);
INSERT INTO substrate VALUES ('East','Mica',x'00'), ('West','Basalt',x'01');
INSERT INTO cassette VALUES (11,12,'opal'), (12,11,'agate');
INSERT INTO assay(mark,region,code,cassette,length_um) VALUES
    ('S-01','east','MICA',11,3.25), ('S-02','EAST','mica',12,5.5);
INSERT INTO frame VALUES (101,'S-01','front');
'''

V2 = '''
CREATE TABLE substrate (region TEXT COLLATE NOCASE NOT NULL, code TEXT COLLATE NOCASE NOT NULL,
    texture BLOB NOT NULL, PRIMARY KEY (region, code)) WITHOUT ROWID;
CREATE TABLE cassette (serial INTEGER PRIMARY KEY, prior INTEGER REFERENCES cassette, label TEXT NOT NULL);
CREATE TABLE provenance (tag TEXT PRIMARY KEY NOT NULL);
CREATE TABLE assay (mark TEXT PRIMARY KEY NOT NULL, region TEXT, code TEXT,
    cassette INTEGER NOT NULL, tag TEXT,
    length_um REAL NOT NULL,
    FOREIGN KEY (cassette) REFERENCES cassette,
    FOREIGN KEY (region, code) REFERENCES substrate,
    FOREIGN KEY (tag) REFERENCES provenance);
CREATE TABLE frame (frame_id INTEGER PRIMARY KEY, mark TEXT REFERENCES assay, note TEXT NOT NULL);
INSERT INTO substrate VALUES ('East','Mica',x'00'), ('West','Basalt',x'01');
INSERT INTO cassette VALUES (11,12,'opal'), (12,11,'agate');
INSERT INTO provenance VALUES ('lab');
INSERT INTO assay(mark,region,code,cassette,tag,length_um) VALUES
    ('S-01','east','MICA',11,'lab',3.25), ('S-02','EAST','mica',12,'lab',5.5);
INSERT INTO frame VALUES (101,'S-01','front');
'''

# A fourth version only reorders the foreign keys, so the substrate constraint
# moves from 0 to 1 while the definition stays exactly the same.
V4 = '''
CREATE TABLE substrate (region TEXT COLLATE NOCASE NOT NULL, code TEXT COLLATE NOCASE NOT NULL,
    texture BLOB NOT NULL, PRIMARY KEY (region, code)) WITHOUT ROWID;
CREATE TABLE cassette (serial INTEGER PRIMARY KEY, prior INTEGER REFERENCES cassette, label TEXT NOT NULL);
CREATE TABLE assay (mark TEXT PRIMARY KEY NOT NULL, region TEXT, code TEXT,
    cassette INTEGER NOT NULL, length_um REAL NOT NULL,
    FOREIGN KEY (region, code) REFERENCES substrate,
    FOREIGN KEY (cassette) REFERENCES cassette);
CREATE TABLE frame (frame_id INTEGER PRIMARY KEY, mark TEXT REFERENCES assay, note TEXT NOT NULL);
INSERT INTO substrate VALUES ('East','Mica',x'00'), ('West','Basalt',x'01');
INSERT INTO cassette VALUES (11,12,'opal'), (12,11,'agate');
INSERT INTO assay(mark,region,code,cassette,length_um) VALUES
    ('S-01','east','MICA',11,3.25), ('S-02','EAST','mica',12,5.5);
INSERT INTO frame VALUES (101,'S-01','front');
'''

# A fifth version keeps the same child, parent and number but reverses the
# column mapping, which is a different constraint and must not be reported as a
# true correspondence.
V5 = '''
CREATE TABLE substrate (region TEXT COLLATE NOCASE NOT NULL, code TEXT COLLATE NOCASE NOT NULL,
    texture BLOB NOT NULL, PRIMARY KEY (region, code)) WITHOUT ROWID;
CREATE TABLE cassette (serial INTEGER PRIMARY KEY, prior INTEGER REFERENCES cassette, label TEXT NOT NULL);
CREATE TABLE assay (mark TEXT PRIMARY KEY NOT NULL, region TEXT, code TEXT,
    cassette INTEGER NOT NULL REFERENCES cassette, length_um REAL NOT NULL,
    FOREIGN KEY (code, region) REFERENCES substrate);
CREATE TABLE frame (frame_id INTEGER PRIMARY KEY, mark TEXT REFERENCES assay, note TEXT NOT NULL);
INSERT INTO substrate VALUES ('East','Mica',x'00'), ('West','Basalt',x'01');
INSERT INTO cassette VALUES (11,12,'opal'), (12,11,'agate');
INSERT INTO assay(mark,region,code,cassette,length_um) VALUES
    ('S-01','MICA','east',11,3.25), ('S-02','mica','EAST',12,5.5);
INSERT INTO frame VALUES (101,'S-01','front');
'''

# A third version drops the substrate reference entirely.
V3 = '''
CREATE TABLE substrate (region TEXT COLLATE NOCASE NOT NULL, code TEXT COLLATE NOCASE NOT NULL,
    texture BLOB NOT NULL, PRIMARY KEY (region, code)) WITHOUT ROWID;
CREATE TABLE cassette (serial INTEGER PRIMARY KEY, prior INTEGER REFERENCES cassette, label TEXT NOT NULL);
CREATE TABLE assay (mark TEXT PRIMARY KEY NOT NULL, region TEXT, code TEXT,
    cassette INTEGER NOT NULL REFERENCES cassette, length_um REAL NOT NULL);
CREATE TABLE frame (frame_id INTEGER PRIMARY KEY, mark TEXT REFERENCES assay, note TEXT NOT NULL);
INSERT INTO substrate VALUES ('East','Mica',x'00'), ('West','Basalt',x'01');
INSERT INTO cassette VALUES (11,12,'opal'), (12,11,'agate');
INSERT INTO assay(mark,region,code,cassette,length_um) VALUES
    ('S-01','east','MICA',11,3.25), ('S-02','EAST','mica',12,5.5);
INSERT INTO frame VALUES (101,'S-01','front');
'''


def open_page(base, path, payload=None, expected=200, origin=None):
    data = None if payload is None else urlencode(payload, doseq=True).encode('ascii')
    headers = {} if data is None else {'Origin': origin or base}
    request = Request(base + path, data=data, headers=headers)
    try:
        response = urlopen(request, timeout=5)
    except HTTPError as exc:
        response = exc
    with response:
        body = response.read()
        assert response.status == expected, (path, response.status, body[:600])
        assert response.headers.get_content_type() == 'text/html', path
        return urlsplit(response.url).path, body.decode('utf-8')


def json_call(base, route, body=None, status=200):
    data = None if body is None else json.dumps(body).encode('utf-8')
    request = Request(base + route, data=data, headers={'Content-Type': 'application/json'})
    try:
        response = urlopen(request, timeout=5)
    except HTTPError as exc:
        response = exc
    with response:
        payload = response.read()
        assert response.status == status, (route, response.status, payload[:600])
        return json.loads(payload)


def rewrite(source, script):
    with closing(sqlite3.connect(source)) as conn, conn:
        conn.executescript(DROP + script)


def edge_id(catalog, child, parent):
    return next(edge['id'] for edge in catalog['schema']['edges']
                if edge['child'] == child and edge['parent'] == parent)


def run(base, source):
    catalog = json_call(base, '/sources/microscope/catalog')
    v1_edge = edge_id(catalog, 'assay', 'substrate')
    recipe = {'source': 'microscope',
              'roots': [{'table': 'substrate', 'key': ['east', 'MICA']}],
              'follow_children': [{'child': 'assay', 'id': v1_edge}], 'row_limit': 20}
    version_one = json_call(base, '/recipes/slice/versions', recipe, 201)
    assert version_one['version'] == 1
    first = json_call(base, '/captures', {'name': 'slice', 'version': 1}, 201)

    # Same schema: the relationship is a genuine correspondence with witnesses.
    _, page = open_page(base, f'/workbench/lineage?source=microscope&table=assay&edge={v1_edge}')
    assert '真实对应' in page and first['id'][:12] in page, page
    assert '/workbench/captures/' in page and '/trace' in page
    assert '同名不同定义' in page and '历史关系已不可比' in page

    # Per-table index lists the relationship and offers the drill-down link.
    _, table_page = open_page(base, '/workbench/lineage?source=microscope&table=assay')
    assert '当前外键关系' in table_page and f'edge={v1_edge}' in table_page
    assert '历史记录如何使用这张表' in table_page

    # Catalog links to the cross-reference page.
    _, catalog_page = open_page(base, '/workbench?source=microscope&table=assay')
    assert '历史使用' in catalog_page and '/workbench/lineage' in catalog_page

    # Now renumber: substrate moves to slot 1 and slot 0 becomes provenance.
    rewrite(source, V2)
    catalog_v2 = json_call(base, '/sources/microscope/catalog')
    moved = edge_id(catalog_v2, 'assay', 'substrate')
    assert moved != v1_edge, (moved, v1_edge)
    _, page = open_page(base, f'/workbench/lineage?source=microscope&table=assay&edge={moved}')
    assert '真实对应' in page and first['id'][:12] in page
    assert '外键号' in page and '另一条定义' in page, page

    # The old slot now means provenance: report it as a divergent same-number edge.
    recipe_two = dict(recipe, follow_children=[{'child': 'assay', 'id': moved}])
    version_two = json_call(base, '/recipes/slice/versions', recipe_two, 201)
    assert version_two['version'] == 2
    second = json_call(base, '/captures', {'name': 'slice', 'version': 2}, 201)
    _, divergent = open_page(base, f'/workbench/lineage?source=microscope&table=assay&edge={v1_edge}')
    assert '同名不同定义' in divergent and second['id'][:12] in divergent, divergent
    assert '另一条定义' in divergent

    # A pure reorder keeps the definition but moves it to another number: the
    # history must still report a genuine correspondence and flag the move.
    rewrite(source, V4)
    catalog_v4 = json_call(base, '/sources/microscope/catalog')
    moved = edge_id(catalog_v4, 'assay', 'substrate')
    assert moved != v1_edge, (moved, v1_edge)
    _, renumbered = open_page(base, f'/workbench/lineage?source=microscope&table=assay&edge={moved}')
    assert '真实对应' in renumbered and first['id'][:12] in renumbered, renumbered
    assert '同一关系在旧模式中使用外键号' in renumbered, renumbered

    # Same endpoints and number but a reversed column mapping: a different
    # constraint that must be reported as a divergent definition.
    rewrite(source, V5)
    catalog_v5 = json_call(base, '/sources/microscope/catalog')
    _, reversed_page = open_page(
        base, f'/workbench/lineage?source=microscope&table=assay&edge={edge_id(catalog_v5, "assay", "substrate")}')
    assert '同名不同定义' in reversed_page and first['id'][:12] in reversed_page, reversed_page
    assert '列映射或引用动作指向另一条定义' in reversed_page

    # Drop the relationship: retained captures now have no comparable edge.
    rewrite(source, V3)
    catalog_v3 = json_call(base, '/sources/microscope/catalog')
    assert not any(e['parent'] == 'substrate' and e['child'] == 'assay' for e in catalog_v3['schema']['edges'])
    _, table_page = open_page(base, '/workbench/lineage?source=microscope&table=assay')
    assert '历史记录如何使用这张表' in table_page and first['id'][:12] in table_page

    # Detailed drill-down for a non-existent edge is a 404 page.
    _, missing = open_page(base, '/workbench/lineage?source=microscope&table=assay&edge=99', expected=404)
    assert '操作未完成' in missing

    # A version whose bound schema no capture retained cannot be compared and
    # is reported as unavailable rather than silently dropped.
    rewrite(source, V1)
    catalog = json_call(base, '/sources/microscope/catalog')
    saved = dict(recipe, follow_children=[{'child': 'assay', 'id': edge_id(catalog, 'assay', 'substrate')}])
    lonely = json_call(base, '/recipes/lonely/versions', saved, 201)
    rewrite(source, V4)
    catalog_v4 = json_call(base, '/sources/microscope/catalog')
    moved = edge_id(catalog_v4, 'assay', 'substrate')
    _, unavailable = open_page(base, f'/workbench/lineage?source=microscope&table=assay&edge={moved}')
    assert '历史关系已不可比' in unavailable and 'lonely' in unavailable, unavailable
    assert '没有被任何捕获保留' in unavailable
    assert lonely['version'] == 1

    # Only the requested alias contributes history: the page loads one source,
    # so an unregistered alias is rejected instead of mixing registries.
    _, page = open_page(base, '/workbench/lineage?source=absent', expected=404)
    assert '未注册' in page
    print('交叉引用检查通过：真实对应、外键改派、关系消失、表级索引与封存证据回链')


def main():
    with TemporaryDirectory(prefix='keelsplice-lineage-') as directory:
        root = Path(directory)
        source = root / 'source.sqlite'
        rewrite(source, V1)
        with create_server(('127.0.0.1', 0), root / 'state.sqlite', {'microscope': source}) as server:
            thread = Thread(target=server.serve_forever, kwargs={'poll_interval': 0.05})
            thread.start()
            try:
                run(f'http://127.0.0.1:{server.server_port}', source)
            finally:
                server.shutdown()
                thread.join()


if __name__ == '__main__':
    main()

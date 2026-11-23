"""A repository created before preview jobs existed must upgrade without data loss."""

import sqlite3
import sys
import threading
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, '/workspace')
from keelsplice.api import create_server

with TemporaryDirectory() as d:
    repo = Path(d) / 'old.sqlite'
    src = Path(d) / 'src.sqlite'
    sqlite3.connect(src).executescript(
        'CREATE TABLE t(id INTEGER PRIMARY KEY); INSERT INTO t VALUES(1);')
    # Repository shape as it existed before preview_jobs was introduced.
    with sqlite3.connect(repo) as c:
        c.executescript('''
          CREATE TABLE recipes (name TEXT, version INTEGER, active INTEGER,
            schema_digest TEXT, recipe TEXT, PRIMARY KEY(name,version));
          CREATE TABLE captures (id TEXT PRIMARY KEY, name TEXT, version INTEGER,
            sealed TEXT, state TEXT, artifact BLOB, certificate TEXT);
        ''')
        c.execute("INSERT INTO recipes VALUES('r',1,1,'digest','{}')")

    srv = create_server(('127.0.0.1', 0), repo, {'s': src})
    t = threading.Thread(target=srv.serve_forever, kwargs={'poll_interval': 0.02})
    t.start()
    try:
        cols = [r[1] for r in sqlite3.connect(repo).execute('PRAGMA table_info(preview_jobs)')]
        assert cols == ['id', 'name', 'version', 'state', 'detail', 'created', 'updated'], cols
        with sqlite3.connect(repo) as c:
            assert c.execute('SELECT name,version,active FROM recipes').fetchall() == [('r', 1, 1)]
            assert c.execute("SELECT state FROM preview_jobs").fetchall() == []
    finally:
        srv.shutdown()
        t.join()
        srv.server_close()
print('MIGRATION OK')

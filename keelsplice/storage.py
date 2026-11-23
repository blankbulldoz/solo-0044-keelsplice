"""Read-only source snapshots and transactional local repository state."""

import sqlite3
from contextlib import closing, contextmanager
from pathlib import Path
from uuid import uuid4

from .codec import Fault, dumps, loads

REPOSITORY_DDL = '''
CREATE TABLE IF NOT EXISTS recipes (
    name TEXT NOT NULL, version INTEGER NOT NULL, active INTEGER NOT NULL,
    schema_digest TEXT NOT NULL, recipe TEXT NOT NULL, PRIMARY KEY (name, version)
);
CREATE TABLE IF NOT EXISTS captures (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, version INTEGER NOT NULL,
    sealed TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN ('sealed','built','certified')),
    artifact BLOB, certificate TEXT,
    FOREIGN KEY(name, version) REFERENCES recipes(name, version)
);
CREATE TABLE IF NOT EXISTS ledgers (
    name TEXT PRIMARY KEY,
    baseline TEXT NOT NULL REFERENCES captures(id),
    schema_digest TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS comparisons (
    id TEXT PRIMARY KEY,
    ledger TEXT NOT NULL REFERENCES ledgers(name),
    ordinal INTEGER NOT NULL,
    baseline TEXT NOT NULL REFERENCES captures(id),
    candidate TEXT NOT NULL REFERENCES captures(id),
    changes TEXT NOT NULL,
    UNIQUE(ledger, ordinal)
);
CREATE TABLE IF NOT EXISTS reviews (
    comparison TEXT NOT NULL REFERENCES comparisons(id),
    item TEXT NOT NULL,
    table_name TEXT NOT NULL,
    row_key TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('added','removed','modified','retarget')),
    verdict TEXT NOT NULL CHECK(verdict IN ('confirmed','rejected')),
    note TEXT NOT NULL,
    PRIMARY KEY(comparison, item)
);
'''


class Repository:
    def __init__(self, path, sources):
        self.path = Path(path).resolve()
        self.sources = {alias: Path(file).resolve(strict=True) for alias, file in sources.items()}
        if self.path in self.sources.values() or any(not path.is_file() for path in self.sources.values()):
            raise ValueError('Source files must exist and be separate from the repository')
        if self.path.exists() and any(self.path.samefile(path) for path in self.sources.values()):
            raise ValueError('Source files must not alias the repository')
        with self.session() as conn:
            conn.executescript(REPOSITORY_DDL)

    @contextmanager
    def session(self, write=False):
        with closing(sqlite3.connect(self.path, timeout=5, isolation_level=None)) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute('PRAGMA foreign_keys=ON')
            with conn:
                if write:
                    conn.execute('BEGIN IMMEDIATE')
                yield conn

    @contextmanager
    def source(self, alias):
        if not isinstance(alias, str) or alias not in self.sources:
            raise Fault('unknown_source', 'Source alias is not registered', 404)
        work = 0

        def check_work():
            nonlocal work
            work += 1
            return work > 5000

        with closing(sqlite3.connect(self.sources[alias].as_uri() + '?mode=ro', uri=True,
                                     timeout=2, isolation_level=None)) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute('PRAGMA query_only=ON')
            conn.execute('PRAGMA trusted_schema=OFF')
            conn.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 8 * 1024 * 1024)
            conn.set_progress_handler(check_work, 1000)
            conn.execute('BEGIN')
            try:
                yield conn
            except sqlite3.OperationalError as exc:
                if work > 5000:
                    raise Fault('scan_budget', 'Source query work exceeded the snapshot budget') from exc
                raise

    def add_recipe(self, name, schema_digest, recipe):
        with self.session(write=True) as conn:
            version = conn.execute('SELECT COALESCE(MAX(version),0)+1 FROM recipes WHERE name=?', (name,)).fetchone()[0]
            conn.execute('INSERT INTO recipes VALUES(?,?,1,?,?)', (name, version, schema_digest, dumps(recipe)))
        return dict(name=name, version=version, active=True, schema_digest=schema_digest, recipe=recipe)

    def version(self, name, number):
        with self.session() as conn:
            row = conn.execute('SELECT * FROM recipes WHERE name=? AND version=?', (name, number)).fetchone()
        if row is None:
            raise Fault('unknown_recipe', 'Recipe version is unknown', 404)
        return dict(row) | {'recipe': loads(row['recipe']), 'active': bool(row['active'])}

    def recent_recipes(self):
        with self.session() as conn:
            rows = conn.execute("""
                SELECT name, version, active, schema_digest, recipe
                FROM recipes ORDER BY rowid DESC LIMIT 50
            """).fetchall()
        return [dict(row) | {'source': loads(row['recipe'])['source']} for row in rows]

    def recent_captures(self):
        with self.session() as conn:
            rows = conn.execute("""
                SELECT id, name, version, state FROM captures ORDER BY rowid DESC LIMIT 50
            """).fetchall()
        return [dict(row) for row in rows]

    def retire(self, name, number):
        with self.session(write=True) as conn:
            update = conn.execute('UPDATE recipes SET active=0 WHERE name=? AND version=?', (name, number))
            if update.rowcount != 1:
                raise Fault('unknown_recipe', 'Recipe version is unknown', 404)
        return self.version(name, number)

    def add_capture(self, name, number, sealed):
        capture_id = uuid4().hex
        with self.session(write=True) as conn:
            saved = conn.execute("""INSERT INTO captures (id,name,version,sealed,state)
                SELECT ?,name,version,?,'sealed' FROM recipes WHERE name=? AND version=? AND active=1""",
                (capture_id, dumps(sealed), name, number))
            if saved.rowcount != 1:
                raise Fault('retired_recipe', 'Recipe retired before this capture could be sealed', 409)
        return dict(id=capture_id, name=name, version=number, sealed=sealed, state='sealed', certificate=None)

    def load_capture(self, capture_id, with_artifact=False):
        columns = 'id,name,version,sealed,state,certificate' + (',artifact' if with_artifact else '')
        with self.session() as conn:
            row = conn.execute(f'SELECT {columns} FROM captures WHERE id=?', (capture_id,)).fetchone()
        if row is None:
            raise Fault('unknown_capture', 'Capture does not exist', 404)
        return dict(row) | {'sealed': loads(row['sealed']),
                            'certificate': loads(row['certificate']) if row['certificate'] else None}

    def promote(self, capture_id, *, artifact=None, certificate=None):
        with self.session(write=True) as conn:
            if artifact is not None:
                conn.execute("UPDATE captures SET state='built',artifact=? WHERE id=? AND state='sealed'",
                             (artifact, capture_id))
            else:
                conn.execute("UPDATE captures SET state='certified',certificate=? WHERE id=? AND state='built'",
                             (dumps(certificate), capture_id))
        return self.load_capture(capture_id)

    def add_ledger(self, name, schema_digest, baseline):
        with self.session(write=True) as conn:
            if conn.execute('SELECT 1 FROM ledgers WHERE name=?', (name,)).fetchone():
                raise Fault('ledger_exists', 'A difference ledger already uses this name', 409)
            conn.execute('INSERT INTO ledgers VALUES(?,?,?)', (name, baseline, schema_digest))
        return self.ledger(name)

    def ledger(self, name):
        with self.session() as conn:
            row = conn.execute('SELECT * FROM ledgers WHERE name=?', (name,)).fetchone()
        if row is None:
            raise Fault('unknown_ledger', 'Difference ledger does not exist', 404)
        return dict(row)

    def recent_ledgers(self):
        with self.session() as conn:
            rows = conn.execute("""
                SELECT ledgers.name, ledgers.baseline, ledgers.schema_digest, COUNT(comparisons.id) AS comparisons
                FROM ledgers LEFT JOIN comparisons ON comparisons.ledger=ledgers.name
                GROUP BY ledgers.name ORDER BY ledgers.rowid DESC LIMIT 50
            """).fetchall()
        return [dict(row) for row in rows]

    def set_baseline(self, name, capture_id):
        with self.session(write=True) as conn:
            updated = conn.execute('UPDATE ledgers SET baseline=? WHERE name=?', (capture_id, name))
            if updated.rowcount != 1:
                raise Fault('unknown_ledger', 'Difference ledger does not exist', 404)
        return self.ledger(name)

    def add_comparison(self, name, baseline, candidate, items):
        comparison_id = uuid4().hex
        with self.session(write=True) as conn:
            number = conn.execute('SELECT COALESCE(MAX(ordinal),0)+1 FROM comparisons WHERE ledger=?',
                                  (name,)).fetchone()[0]
            conn.execute('INSERT INTO comparisons VALUES(?,?,?,?,?,?)',
                         (comparison_id, name, number, baseline, candidate, dumps(items)))
        return dict(id=comparison_id, ledger=name, ordinal=number, baseline=baseline,
                    candidate=candidate, items=items)

    def comparisons(self, name):
        with self.session() as conn:
            rows = conn.execute('SELECT * FROM comparisons WHERE ledger=? ORDER BY ordinal', (name,)).fetchall()
        return [dict(row) | {'items': loads(row['changes'])} for row in rows]

    def review(self, comparison_id):
        with self.session() as conn:
            row = conn.execute('SELECT * FROM comparisons WHERE id=?', (comparison_id,)).fetchone()
        if row is None:
            raise Fault('unknown_comparison', 'Comparison does not exist', 404)
        return dict(row) | {'items': loads(row['changes'])}

    def add_review(self, comparison_id, item, table, key, kind, verdict, note):
        with self.session(write=True) as conn:
            conn.execute("""INSERT INTO reviews (comparison,item,table_name,row_key,kind,verdict,note)
                VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(comparison,item) DO UPDATE SET
                    table_name=excluded.table_name, row_key=excluded.row_key, kind=excluded.kind,
                    verdict=excluded.verdict, note=excluded.note""",
                (comparison_id, item, table, key, kind, verdict, note))
        return self.reviews(comparison_id)

    def reviews(self, comparison_id):
        with self.session() as conn:
            rows = conn.execute('SELECT * FROM reviews WHERE comparison=? ORDER BY rowid', (comparison_id,)).fetchall()
        return {row['item']: dict(row) for row in rows}

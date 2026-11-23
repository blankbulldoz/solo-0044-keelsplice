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
CREATE TABLE IF NOT EXISTS preview_jobs (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, version INTEGER NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('running','completed','cancelled','inconclusive','failed')),
    detail TEXT, created TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
'''

# Preview states recorded while the process that owns the read transaction is alive.
# 'inconclusive' is assigned at startup to jobs left running by a previous process.
PREVIEW_TERMINAL = ('completed', 'cancelled', 'inconclusive', 'failed')


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
            self.recover_previews(conn)

    def recover_previews(self, conn):
        """Jobs still running when the previous process ended have no durable conclusion."""
        conn.execute("""
            UPDATE preview_jobs SET state='inconclusive',
                updated=strftime('%Y-%m-%dT%H:%M:%fZ','now')
            WHERE state='running'
        """)

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
    def source(self, alias, *, cancel=None):
        """Open one independent read transaction; ``cancel`` interrupts only this connection."""
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
            # A cooperative gate runs between SQL steps; interrupt() additionally aborts a
            # single long-running statement on this connection without touching other jobs.
            if cancel is not None:
                conn.set_progress_handler(cancel.progress(check_work), 1000)
                cancel.bind(conn.interrupt)
            conn.execute('BEGIN')
            try:
                yield conn
            except sqlite3.OperationalError as exc:
                if cancel is not None and cancel.is_set():
                    raise Fault('preview_cancelled', 'Preview read transaction was cancelled', 499) from exc
                if work > 5000:
                    raise Fault('scan_budget', 'Source query work exceeded the snapshot budget') from exc
                raise
            finally:
                if cancel is not None:
                    cancel.release()

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

    def add_preview(self, name, number):
        job_id = uuid4().hex
        with self.session(write=True) as conn:
            conn.execute("INSERT INTO preview_jobs (id,name,version,state) VALUES (?,?,?,'running')",
                         (job_id, name, number))
        return job_id

    def load_preview(self, job_id):
        if not isinstance(job_id, str) or len(job_id) != 32:
            raise Fault('unknown_preview', 'Preview job does not exist', 404)
        with self.session() as conn:
            row = conn.execute('SELECT * FROM preview_jobs WHERE id=?', (job_id,)).fetchone()
        if row is None:
            raise Fault('unknown_preview', 'Preview job does not exist', 404)
        record = dict(row)
        record['detail'] = loads(row['detail']) if row['detail'] else None
        return record

    def recent_previews(self):
        with self.session() as conn:
            rows = conn.execute("""
                SELECT id,name,version,state FROM preview_jobs ORDER BY rowid DESC LIMIT 50
            """).fetchall()
        return [dict(row) for row in rows]

    def finish_preview(self, job_id, state, detail=None):
        """Conditional terminal transition: a settled job never changes, so a late cancel cannot win."""
        if state not in PREVIEW_TERMINAL:
            raise ValueError('finish_preview requires a terminal state')
        with self.session(write=True) as conn:
            updated = conn.execute("""
                UPDATE preview_jobs
                SET state=?, detail=COALESCE(?, detail),
                    updated=strftime('%Y-%m-%dT%H:%M:%fZ','now')
                WHERE id=? AND state='running'
            """, (state, dumps(detail) if detail is not None else None, job_id))
            if updated.rowcount != 1:
                return self.load_preview(job_id)
        return self.load_preview(job_id)

    def request_preview_cancel(self, job_id):
        """Mark cancellation only while the job is still running; otherwise keep its settled state."""
        with self.session(write=True) as conn:
            updated = conn.execute("""
                UPDATE preview_jobs SET state='cancelled',
                    updated=strftime('%Y-%m-%dT%H:%M:%fZ','now')
                WHERE id=? AND state='running'
            """, (job_id,))
            if updated.rowcount != 1:
                return self.load_preview(job_id)
        return self.load_preview(job_id)

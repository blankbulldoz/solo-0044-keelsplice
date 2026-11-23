"""Read-only source snapshots and transactional local repository state."""

import sqlite3
from contextlib import closing, contextmanager
from datetime import datetime, timezone
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
CREATE TABLE IF NOT EXISTS certificates (
    id TEXT PRIMARY KEY, capture TEXT NOT NULL REFERENCES captures(id),
    certificate TEXT NOT NULL, current INTEGER NOT NULL CHECK(current IN (0,1)),
    created TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reviews (
    id TEXT PRIMARY KEY, capture TEXT NOT NULL REFERENCES captures(id),
    station TEXT NOT NULL, certificate_id TEXT NOT NULL REFERENCES certificates(id),
    certificate_sha256 TEXT NOT NULL, artifact_sha256 TEXT NOT NULL,
    artifact_bytes INTEGER NOT NULL,
    verdict TEXT NOT NULL CHECK(verdict IN ('transferable','blocked')),
    created TEXT NOT NULL
);
'''

MIGRATION = '''
INSERT OR IGNORE INTO certificates (id, capture, certificate, current, created)
SELECT 'legacy-' || id, id, certificate, 1, '' FROM captures
WHERE certificate IS NOT NULL
  AND NOT EXISTS (SELECT 1 FROM certificates WHERE certificates.capture = captures.id)
'''


def now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')


class Repository:
    def __init__(self, path, sources):
        self.path = Path(path).resolve()
        self.sources = {alias: Path(file).resolve(strict=True) for alias, file in sources.items()}
        if self.path in self.sources.values() or any(not path.is_file() for path in self.sources.values()):
            raise ValueError('Source files must exist and be separate from the repository')
        if self.path.exists() and any(self.path.samefile(path) for path in self.sources.values()):
            raise ValueError('Source files must not alias the repository')
        with self.session(write=True) as conn:
            conn.executescript(REPOSITORY_DDL)
            conn.execute(MIGRATION)

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

    def archive_certificate(self, conn, capture_id, certificate):
        conn.execute('UPDATE certificates SET current=0 WHERE capture=? AND current=1', (capture_id,))
        certificate_id = uuid4().hex
        conn.execute('INSERT INTO certificates VALUES(?,?,?,1,?)',
                     (certificate_id, capture_id, dumps(certificate), now()))
        return certificate_id

    def promote(self, capture_id, *, artifact=None, certificate=None):
        with self.session(write=True) as conn:
            if artifact is not None:
                conn.execute("UPDATE captures SET state='built',artifact=? WHERE id=? AND state='sealed'",
                             (artifact, capture_id))
            else:
                conn.execute("UPDATE captures SET state='certified',certificate=? WHERE id=? AND state='built'",
                             (dumps(certificate), capture_id))
                self.archive_certificate(conn, capture_id, certificate)
        return self.load_capture(capture_id)

    def rebuild(self, capture_id, artifact):
        with self.session(write=True) as conn:
            updated = conn.execute(
                "UPDATE captures SET state='built',artifact=?,certificate=NULL WHERE id=? AND state='certified'",
                (artifact, capture_id))
            if updated.rowcount != 1:
                raise Fault('fixture_not_certified', 'Only a certified capture can be rebuilt', 409)
            conn.execute('UPDATE certificates SET current=0 WHERE capture=? AND current=1', (capture_id,))
        return self.load_capture(capture_id)

    def current_certificate(self, capture_id):
        with self.session() as conn:
            row = conn.execute(
                'SELECT id, certificate FROM certificates WHERE capture=? AND current=1', (capture_id,)).fetchone()
        return None if row is None else {'id': row['id'], 'certificate': loads(row['certificate'])}

    def register_certificate(self, capture_id, certificate):
        with self.session(write=True) as conn:
            row = conn.execute('SELECT id FROM certificates WHERE capture=? AND current=1',
                               (capture_id,)).fetchone()
            if row is None:
                self.archive_certificate(conn, capture_id, certificate)
        return self.current_certificate(capture_id)

    def certificates(self, capture_id):
        with self.session() as conn:
            rows = conn.execute(
                'SELECT id, certificate, current, created FROM certificates WHERE capture=? ORDER BY rowid DESC',
                (capture_id,)).fetchall()
        return [dict(id=row['id'], sha256=loads(row['certificate'])['sha256'],
                     bytes=loads(row['certificate'])['bytes'],
                     current=bool(row['current']), created=row['created']) for row in rows]

    def add_review(self, capture_id, station, certificate_id, certificate_sha256,
                   artifact_sha256, artifact_bytes, verdict):
        review_id = uuid4().hex
        created = now()
        with self.session(write=True) as conn:
            conn.execute('INSERT INTO reviews VALUES(?,?,?,?,?,?,?,?,?)',
                         (review_id, capture_id, station, certificate_id, certificate_sha256,
                          artifact_sha256, artifact_bytes, verdict, created))
        return dict(id=review_id, capture=capture_id, station=station, certificate_id=certificate_id,
                    certificate_sha256=certificate_sha256, artifact_sha256=artifact_sha256,
                    artifact_bytes=artifact_bytes, verdict=verdict, created=created)

    def reviews(self, capture_id):
        with self.session() as conn:
            rows = conn.execute(
                'SELECT * FROM reviews WHERE capture=? ORDER BY rowid DESC LIMIT 100', (capture_id,)).fetchall()
        return [dict(row) for row in rows]

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
CREATE TABLE IF NOT EXISTS receivers (
    name TEXT PRIMARY KEY, builtin INTEGER NOT NULL, profile TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS trials (
    id TEXT PRIMARY KEY, capture_id TEXT NOT NULL, device TEXT NOT NULL,
    verdict TEXT NOT NULL CHECK(verdict IN ('passed','degraded','failed')),
    report TEXT NOT NULL, payload_sha256 TEXT NOT NULL, payload_bytes INTEGER NOT NULL,
    FOREIGN KEY(capture_id) REFERENCES captures(id),
    FOREIGN KEY(device) REFERENCES receivers(name)
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

    def ensure_builtin_receivers(self, profiles):
        with self.session(write=True) as conn:
            for profile in profiles:
                conn.execute(
                    'INSERT OR IGNORE INTO receivers (name, builtin, profile) VALUES (?,1,?)',
                    (profile['name'], dumps(profile)))

    def list_receivers(self):
        with self.session() as conn:
            rows = conn.execute('SELECT name, builtin, profile FROM receivers ORDER BY builtin DESC, name').fetchall()
        return [dict(name=row['name'], builtin=bool(row['builtin']), profile=loads(row['profile'])) for row in rows]

    def receiver(self, name):
        with self.session() as conn:
            row = conn.execute('SELECT profile FROM receivers WHERE name=?', (name,)).fetchone()
        if row is None:
            raise Fault('unknown_receiver', 'Receiving device is not registered', 404)
        return loads(row['profile'])

    def add_receiver(self, profile):
        with self.session(write=True) as conn:
            inserted = conn.execute(
                'INSERT OR IGNORE INTO receivers (name, builtin, profile) VALUES (?,0,?)',
                (profile['name'], dumps(profile)))
        if inserted.rowcount != 1:
            raise Fault('receiver_exists', 'A device with this name is already registered', 409)
        return dict(name=profile['name'], builtin=False, profile=profile)

    def trial_directory(self):
        directory = self.path.parent / f'{self.path.name}-trials'
        directory.mkdir(exist_ok=True)
        return directory

    def add_trial(self, capture_id, profile, report, payload, encoded, database):
        from .receivers import MAX_PAYLOAD_BYTES
        if len(encoded) > MAX_PAYLOAD_BYTES:
            raise Fault('payload_budget', 'Relay payload exceeds the 8 MiB trial budget')
        trial_id = uuid4().hex
        with self.session(write=True) as conn:
            inserted = conn.execute(
                "INSERT INTO trials (id,capture_id,device,verdict,report,payload_sha256,payload_bytes) "
                "SELECT ?,?,?,?,?,?,? WHERE EXISTS (SELECT 1 FROM captures WHERE id=?)",
                (trial_id, capture_id, profile['name'], report['verdict'], dumps(report),
                 report['payload_sha256'], len(encoded), capture_id))
        if inserted.rowcount != 1:
            raise Fault('unknown_capture', 'Capture does not exist', 404)
        directory = self.trial_directory()
        (directory / f'{trial_id}.json').write_bytes(encoded)
        (directory / f'{trial_id}.sqlite').write_bytes(database)
        return trial_id

    def load_trial(self, trial_id, with_payload=False, with_database=False):
        with self.session() as conn:
            row = conn.execute('SELECT * FROM trials WHERE id=?', (trial_id,)).fetchone()
        if row is None:
            raise Fault('unknown_trial', 'Trial delivery does not exist', 404)
        result = dict(row) | {'report': loads(row['report'])}
        directory = self.trial_directory()
        if with_payload:
            result['payload_bytes'] = (directory / f'{trial_id}.json').read_bytes()
        if with_database:
            result['database_bytes'] = (directory / f'{trial_id}.sqlite').read_bytes()
        return result

    def recent_trials(self, capture_id=None):
        with self.session() as conn:
            if capture_id is None:
                rows = conn.execute('SELECT id,capture_id,device,verdict FROM trials ORDER BY rowid DESC LIMIT 50').fetchall()
            else:
                rows = conn.execute(
                    'SELECT id,capture_id,device,verdict FROM trials WHERE capture_id=? ORDER BY rowid DESC LIMIT 50',
                    (capture_id,)).fetchall()
        return [dict(row) for row in rows]

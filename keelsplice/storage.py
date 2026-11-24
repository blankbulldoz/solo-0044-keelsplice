"""Read-only source snapshots and transactional local repository state."""

import json
import os
import sqlite3
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from .codec import Fault, digest, dumps, fingerprint, loads

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
CREATE TABLE IF NOT EXISTS repository_meta (
    key TEXT PRIMARY KEY, value TEXT NOT NULL
);
'''

SWITCHOVER_FORMAT = 'keelsplice-switchover/1'


def repository_signature(conn):
    """Fingerprint the persistent recipe and capture records only."""
    recipes = conn.execute(
        'SELECT name,version,active,schema_digest,recipe FROM recipes ORDER BY name,version'
    ).fetchall()
    captures = conn.execute(
        'SELECT id,name,version,sealed,state,certificate FROM captures ORDER BY id'
    ).fetchall()
    return fingerprint({
        'recipes': [[row[0], row[1], row[2], row[3], digest(row[4].encode('ascii'))] for row in recipes],
        'captures': [[row[0], row[1], row[2], row[3], digest(row[4].encode('ascii')),
                      digest(row[5].encode('ascii')) if row[5] else None] for row in captures],
    })


def _moment(value):
    """Parse an ISO-8601 instant, treating a missing zone as UTC."""
    if not isinstance(value, str) or not value:
        raise Fault('handover_manifest', 'Switchover manifest needs an ISO-8601 rollback window', 409)
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError as exc:
        raise Fault('handover_manifest', 'Switchover manifest has an invalid timestamp', 409) from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


class Repository:
    def __init__(self, path, sources, *, role='primary', switchover=None):
        if role not in ('primary', 'standby'):
            raise ValueError('Role must be primary or standby')
        self.path = Path(path).resolve()
        self.role = role
        self.read_only = role == 'standby'
        self.replica = None
        self.switchover = None
        self.sources = {alias: Path(file).resolve(strict=True) for alias, file in sources.items()}
        if self.path in self.sources.values() or any(not path.is_file() for path in self.sources.values()):
            raise ValueError('Source files must exist and be separate from the repository')
        if self.path.exists() and any(self.path.samefile(path) for path in self.sources.values()):
            raise ValueError('Source files must not alias the repository')
        if self.read_only:
            if switchover is not None:
                raise ValueError('A standby instance cannot consume a switchover manifest')
            if not self.path.is_file():
                raise Fault('standby_repository', 'Standby repository does not exist', 409)
            self.replica = self._load_replica()
        else:
            if switchover is not None and not self.path.is_file():
                raise Fault('handover_location',
                            'Persistent repository location from the switchover manifest does not exist', 409)
            self.switchover = self._load_switchover(switchover) if switchover is not None else None
            if self.switchover is not None:
                self._verify_switchover()
            with self.session() as conn:
                conn.executescript(REPOSITORY_DDL)

    @contextmanager
    def session(self, write=False):
        if write and self.read_only:
            raise Fault('standby_read_only', 'Standby instance accepts no repository writes', 409)
        if self.read_only:
            with closing(sqlite3.connect(self.path.as_uri() + '?mode=ro', uri=True,
                                         timeout=5, isolation_level=None)) as conn:
                conn.row_factory = sqlite3.Row
                conn.execute('PRAGMA query_only=ON')
                conn.execute('PRAGMA foreign_keys=ON')
                yield conn
            return
        with closing(sqlite3.connect(self.path, timeout=5, isolation_level=None)) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute('PRAGMA foreign_keys=ON')
            with conn:
                if write:
                    conn.execute('BEGIN IMMEDIATE')
                yield conn

    def _load_replica(self):
        try:
            with self.session() as conn:
                row = conn.execute("SELECT value FROM repository_meta WHERE key='replica'").fetchone()
        except sqlite3.OperationalError as exc:
            raise Fault('replica_provenance',
                        'Standby repository lacks snapshot provenance metadata', 409) from exc
        if row is None:
            raise Fault('replica_provenance',
                        'Standby repository lacks snapshot provenance metadata', 409)
        try:
            replica = loads(row['value'])
        except ValueError as exc:
            raise Fault('replica_provenance', 'Standby snapshot provenance is unreadable', 409) from exc
        if not isinstance(replica, dict) or not replica.get('snapshot_at') or not replica.get('source_repository'):
            raise Fault('replica_provenance', 'Standby snapshot provenance is incomplete', 409)
        return replica

    @staticmethod
    def _load_switchover(path):
        try:
            manifest = json.loads(Path(path).read_text(encoding='utf-8'))
        except (OSError, ValueError) as exc:
            raise Fault('handover_manifest', 'Cannot read the switchover manifest', 409) from exc
        if not isinstance(manifest, dict):
            raise Fault('handover_manifest', 'Switchover manifest must be a JSON object', 409)
        return manifest

    def _verify_switchover(self):
        manifest = self.switchover
        if manifest.get('format') != SWITCHOVER_FORMAT:
            raise Fault('handover_manifest', 'Unknown switchover manifest format', 409)
        target = manifest.get('repository')
        if not isinstance(target, str) or Path(target).resolve() != self.path:
            raise Fault('handover_location',
                        'Persistent repository location differs from the switchover manifest', 409)
        try:
            stat = os.stat(self.path)
        except OSError as exc:
            raise Fault('handover_location', 'Persistent repository cannot be inspected', 409) from exc
        if (manifest.get('device'), manifest.get('inode')) != (stat.st_dev, stat.st_ino):
            raise Fault('handover_location',
                        'Persistent repository file identity differs from the switchover manifest', 409)
        try:
            signature = self.content_signature()
        except sqlite3.OperationalError as exc:
            raise Fault('handover_content', 'Switchover repository is not initialized', 409) from exc
        if manifest.get('content_signature') != signature:
            raise Fault('handover_content',
                        'Persistent records changed after the handover manifest was written', 409)
        window = manifest.get('rollback_window') or {}
        if datetime.now(timezone.utc) > _moment(window.get('closes_at')):
            raise Fault('handover_window', 'Rollback window closed before promotion', 409)

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

    def content_signature(self):
        with self.session() as conn:
            return repository_signature(conn)

    def capture_index(self):
        with self.session() as conn:
            rows = conn.execute('SELECT id,name,version,state FROM captures ORDER BY rowid').fetchall()
        return [dict(row) for row in rows]

    def recipe_index(self):
        with self.session() as conn:
            rows = conn.execute('SELECT name,version,active FROM recipes ORDER BY rowid').fetchall()
        return [{'name': row['name'], 'version': row['version'], 'active': bool(row['active'])} for row in rows]

    def role_summary(self):
        summary = {'role': self.role, 'authoritative': not self.read_only, 'repository': str(self.path)}
        if self.read_only:
            summary['replica'] = self.replica
        if self.switchover is not None:
            summary['switchover'] = {
                key: self.switchover.get(key)
                for key in ('format', 'drill_id', 'snapshot', 'rollback_window')
                if self.switchover.get(key) is not None
            }
        return summary

    def instance_info(self):
        return self.role_summary() | {
            'content_signature': self.content_signature(),
            'recipes': self.recipe_index(),
            'captures': self.capture_index(),
        }

    def state_context(self):
        """Describe whether recipe activity is live or only a replica observation."""
        if self.read_only:
            return {'role': 'standby', 'authoritative': False,
                    'as_of': self.replica['snapshot_at'],
                    'snapshot_source_repository': self.replica['source_repository'],
                    'note': '配方投用状态来自快照，不代表主实例当前状态。'}
        context = {'role': 'primary', 'authoritative': True}
        if self.switchover is not None:
            context['rollback_window'] = self.switchover.get('rollback_window')
        return context

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

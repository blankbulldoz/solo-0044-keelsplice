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
CREATE TABLE IF NOT EXISTS watch_subjects (
    id TEXT PRIMARY KEY, source TEXT NOT NULL, kind TEXT NOT NULL CHECK(kind IN ('edge','pk')),
    child_table TEXT, edge_id INTEGER, target_table TEXT NOT NULL,
    continuity_seconds INTEGER NOT NULL, note TEXT NOT NULL,
    baseline_fingerprint TEXT NOT NULL, created_at TEXT NOT NULL, inactive INTEGER NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX IF NOT EXISTS watch_active_unique ON watch_subjects(
    source, kind, COALESCE(child_table, ''), COALESCE(edge_id, -1), target_table
) WHERE inactive = 0;
CREATE TABLE IF NOT EXISTS catalog_checks (
    id TEXT PRIMARY KEY, seq INTEGER NOT NULL, source TEXT NOT NULL, observed_at TEXT NOT NULL,
    outcome TEXT NOT NULL CHECK(outcome IN ('observed','unreachable')),
    schema_digest TEXT, schema TEXT, reason TEXT
);
CREATE INDEX IF NOT EXISTS catalog_checks_source ON catalog_checks(source, seq);
CREATE TABLE IF NOT EXISTS watch_notifications (
    id TEXT PRIMARY KEY, watch_id TEXT NOT NULL, kind TEXT NOT NULL CHECK(kind IN ('changed','unreachable')),
    check_id TEXT NOT NULL, created_at TEXT NOT NULL, acknowledged INTEGER NOT NULL DEFAULT 0,
    summary TEXT NOT NULL, evidence TEXT NOT NULL, recipients TEXT NOT NULL,
    FOREIGN KEY(watch_id) REFERENCES watch_subjects(id)
);
CREATE UNIQUE INDEX IF NOT EXISTS watch_notification_once ON watch_notifications(watch_id, check_id, kind);
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

    # --- Source-schema watches, catalog checks and notifications ---

    @staticmethod
    def _watch(row):
        return dict(row) | {'inactive': bool(row['inactive'])}

    def active_watch(self, source, target):
        with self.session() as conn:
            if target['kind'] == 'edge':
                row = conn.execute(
                    'SELECT * FROM watch_subjects WHERE inactive=0 AND source=? AND kind=? AND target_table=?'
                    ' AND child_table=? AND edge_id=?',
                    (source, 'edge', target['target_table'],
                     target['child_table'], target['edge_id'])).fetchone()
            else:
                row = conn.execute(
                    'SELECT * FROM watch_subjects WHERE inactive=0 AND source=? AND kind=? AND target_table=?'
                    ' AND child_table IS NULL AND edge_id IS NULL',
                    (source, 'pk', target['target_table'])).fetchone()
        return None if row is None else self._watch(row)

    def add_watch(self, target, *, continuity_seconds, note_text, baseline_fingerprint, created_at):
        watch_id = uuid4().hex
        with self.session(write=True) as conn:
            try:
                conn.execute(
                    'INSERT INTO watch_subjects (id,source,kind,child_table,edge_id,target_table,'
                    'continuity_seconds,note,baseline_fingerprint,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)',
                    (watch_id, target['source'], target['kind'], target['child_table'], target['edge_id'],
                     target['target_table'], continuity_seconds, note_text, baseline_fingerprint, created_at))
            except sqlite3.IntegrityError as exc:
                raise Fault('watch_exists', '这个结构已经在关注列表中。', 409) from exc
            row = conn.execute('SELECT * FROM watch_subjects WHERE id=?', (watch_id,)).fetchone()
        return self._watch(row)

    def get_watch(self, watch_id):
        with self.session() as conn:
            row = conn.execute('SELECT * FROM watch_subjects WHERE id=?', (watch_id,)).fetchone()
        if row is None:
            raise Fault('unknown_watch', '找不到指定的关注。', 404)
        return self._watch(row)

    def list_watches(self, *, source=None, include_inactive=False):
        sql = 'SELECT * FROM watch_subjects'
        clauses = []
        params = []
        if not include_inactive:
            clauses.append('inactive=0')
        if source is not None:
            clauses.append('source=?')
            params.append(source)
        if clauses:
            sql += ' WHERE ' + ' AND '.join(clauses)
        sql += ' ORDER BY rowid DESC'
        with self.session() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._watch(row) for row in rows]

    def retire_watch(self, watch_id):
        with self.session(write=True) as conn:
            update = conn.execute('UPDATE watch_subjects SET inactive=1 WHERE id=? AND inactive=0', (watch_id,))
            if update.rowcount != 1:
                raise Fault('unknown_watch', '找不到指定的活动关注。', 404)
        return self.get_watch(watch_id)

    def add_catalog_check(self, source, *, observed_at, outcome, schema_digest=None, schema=None, reason=None):
        check_id = uuid4().hex
        with self.session(write=True) as conn:
            seq = conn.execute('SELECT COALESCE(MAX(seq),0)+1 FROM catalog_checks WHERE source=?',
                               (source,)).fetchone()[0]
            conn.execute(
                'INSERT INTO catalog_checks (id,seq,source,observed_at,outcome,schema_digest,schema,reason)'
                ' VALUES(?,?,?,?,?,?,?,?)',
                (check_id, seq, source, observed_at, outcome, schema_digest,
                 dumps(schema) if schema is not None else None, reason))
            row = conn.execute('SELECT * FROM catalog_checks WHERE id=?', (check_id,)).fetchone()
        return dict(row)

    def checks_for_source(self, source):
        with self.session() as conn:
            rows = conn.execute(
                'SELECT * FROM catalog_checks WHERE source=? ORDER BY seq', (source,)).fetchall()
        return [dict(row) for row in rows]

    def latest_check(self, source):
        with self.session() as conn:
            row = conn.execute(
                'SELECT * FROM catalog_checks WHERE source=? ORDER BY seq DESC LIMIT 1', (source,)).fetchone()
        return None if row is None else dict(row)

    def all_recipes(self):
        with self.session() as conn:
            rows = conn.execute('SELECT name, version, active, recipe FROM recipes ORDER BY rowid').fetchall()
        return [dict(row) | {'recipe': loads(row['recipe']), 'active': bool(row['active'])} for row in rows]

    def add_notification(self, *, watch_id, kind, check_id, created_at, summary, evidence_entries, recipients):
        notification_id = uuid4().hex
        with self.session(write=True) as conn:
            try:
                conn.execute(
                    'INSERT INTO watch_notifications (id,watch_id,kind,check_id,created_at,summary,evidence,recipients)'
                    ' VALUES(?,?,?,?,?,?,?,?)',
                    (notification_id, watch_id, kind, check_id, created_at, summary,
                     dumps(evidence_entries), dumps(recipients)))
            except sqlite3.IntegrityError:
                return None
            row = conn.execute('SELECT * FROM watch_notifications WHERE id=?', (notification_id,)).fetchone()
        return dict(row) | {'evidence': loads(row['evidence']), 'recipients': loads(row['recipients']),
                            'acknowledged': bool(row['acknowledged'])}

    def get_notification(self, notification_id):
        with self.session() as conn:
            row = conn.execute('SELECT * FROM watch_notifications WHERE id=?', (notification_id,)).fetchone()
        if row is None:
            raise Fault('unknown_notification', '找不到指定的通知。', 404)
        return dict(row) | {'evidence': loads(row['evidence']), 'recipients': loads(row['recipients']),
                            'acknowledged': bool(row['acknowledged'])}

    def list_notifications(self, *, watch_id=None, only_unacknowledged=False):
        sql = 'SELECT * FROM watch_notifications'
        clauses = []
        params = []
        if watch_id is not None:
            clauses.append('watch_id=?')
            params.append(watch_id)
        if only_unacknowledged:
            clauses.append('acknowledged=0')
        if clauses:
            sql += ' WHERE ' + ' AND '.join(clauses)
        sql += ' ORDER BY rowid DESC'
        with self.session() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [dict(row) | {'evidence': loads(row['evidence']), 'recipients': loads(row['recipients']),
                             'acknowledged': bool(row['acknowledged'])} for row in rows]

    def acknowledge_notification(self, notification_id):
        with self.session(write=True) as conn:
            update = conn.execute(
                'UPDATE watch_notifications SET acknowledged=1 WHERE id=?', (notification_id,))
            if update.rowcount != 1:
                raise Fault('unknown_notification', '找不到指定的通知。', 404)
        return self.get_notification(notification_id)

    def unacknowledged_watch_counts(self):
        with self.session() as conn:
            rows = conn.execute(
                'SELECT watch_id, COUNT(*) AS total FROM watch_notifications WHERE acknowledged=0 GROUP BY watch_id'
            ).fetchall()
        return {row['watch_id']: row['total'] for row in rows}

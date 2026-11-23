"""Read-only source snapshots and transactional local repository state."""

import sqlite3
from contextlib import closing, contextmanager
from pathlib import Path
from uuid import uuid4

from . import delivery
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
CREATE TABLE IF NOT EXISTS batches (
    id TEXT PRIMARY KEY, label TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('open','closed'))
);
CREATE TABLE IF NOT EXISTS deliveries (
    id TEXT PRIMARY KEY,
    batch_id TEXT NOT NULL REFERENCES batches(id),
    capture_id TEXT NOT NULL REFERENCES captures(id),
    recipient TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('initial','reissue')),
    decision TEXT NOT NULL CHECK(decision IN ('pending','continued','held','settled')),
    note TEXT NOT NULL,
    outcome TEXT CHECK(outcome IN ('delivered','reissue','held')),
    outcome_reason TEXT,
    sent_sha256 TEXT, sent_bytes INTEGER,
    UNIQUE(batch_id, capture_id, recipient)
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

    def open_batch(self, conn):
        return conn.execute("SELECT id FROM batches WHERE state='open'").fetchone()

    def pending_reissue(self, conn):
        return conn.execute("""
            SELECT d.capture_id, d.recipient FROM deliveries d
            JOIN batches b ON b.id = d.batch_id
            WHERE b.state = 'closed' AND d.outcome = 'reissue'
              AND NOT EXISTS (
                  SELECT 1 FROM deliveries d2 JOIN batches b2 ON b2.id = d2.batch_id
                  WHERE d2.capture_id = d.capture_id AND d2.recipient = d.recipient
                    AND b2.state = 'closed' AND b2.rowid > b.rowid)
            ORDER BY d.rowid
        """).fetchall()

    def add_batch(self, label):
        batch_id = uuid4().hex
        with self.session(write=True) as conn:
            if self.open_batch(conn):
                raise Fault('batch_still_open', 'Close the current delivery batch before opening the next', 409)
            conn.execute("INSERT INTO batches (id,label,state) VALUES (?,?,'open')", (batch_id, label))
            for row in self.pending_reissue(conn):
                conn.execute("""INSERT INTO deliveries (id,batch_id,capture_id,recipient,kind,decision,note)
                    VALUES (?,?,?,?,'reissue','pending','')""",
                             (uuid4().hex, batch_id, row['capture_id'], row['recipient']))
        return self.load_batch(batch_id)

    def recent_batches(self):
        with self.session() as conn:
            rows = conn.execute("""
                SELECT b.id, b.label, b.state,
                       (SELECT COUNT(*) FROM deliveries d WHERE d.batch_id = b.id) AS entries,
                       (SELECT COUNT(*) FROM deliveries d
                        WHERE d.batch_id = b.id AND d.outcome = 'delivered') AS delivered
                FROM batches b ORDER BY b.rowid DESC LIMIT 50
            """).fetchall()
        return [dict(row) for row in rows]

    def known_recipients(self):
        with self.session() as conn:
            rows = conn.execute('SELECT DISTINCT recipient FROM deliveries ORDER BY recipient LIMIT 64').fetchall()
        return [row['recipient'] for row in rows]

    def load_batch(self, batch_id):
        with self.session() as conn:
            batch = conn.execute('SELECT * FROM batches WHERE id=?', (batch_id,)).fetchone()
            if batch is None:
                raise Fault('unknown_batch', 'Delivery batch does not exist', 404)
            rows = conn.execute("""
                SELECT d.*, c.name, c.version, c.state AS capture_state, c.certificate
                FROM deliveries d JOIN captures c ON c.id = d.capture_id
                WHERE d.batch_id = ? ORDER BY d.rowid
            """, (batch_id,)).fetchall()
        return dict(batch) | {'entries': [dict(row) for row in rows]}

    def editable_batch(self, conn, batch_id):
        batch = conn.execute('SELECT state FROM batches WHERE id=?', (batch_id,)).fetchone()
        if batch is None:
            raise Fault('unknown_batch', 'Delivery batch does not exist', 404)
        if batch['state'] != 'open':
            raise Fault('batch_closed', 'Delivery batch is closed and read-only', 409)

    def add_delivery(self, batch_id, capture_id, recipient):
        with self.session(write=True) as conn:
            self.editable_batch(conn, batch_id)
            if conn.execute('SELECT 1 FROM captures WHERE id=?', (capture_id,)).fetchone() is None:
                raise Fault('unknown_capture', 'Capture does not exist', 404)
            if conn.execute('SELECT COUNT(*) FROM deliveries WHERE batch_id=?',
                            (batch_id,)).fetchone()[0] >= delivery.MAX_ENTRIES:
                raise Fault('batch_budget', 'A delivery batch holds at most 256 entries')
            if conn.execute('SELECT 1 FROM deliveries WHERE batch_id=? AND capture_id=? AND recipient=?',
                            (batch_id, capture_id, recipient)).fetchone():
                raise Fault('duplicate_delivery', 'This group already settles this capture in the batch', 409)
            seen = conn.execute("""
                SELECT 1 FROM deliveries d JOIN batches b ON b.id = d.batch_id
                WHERE d.capture_id=? AND d.recipient=? AND b.state='closed'
            """, (capture_id, recipient)).fetchone()
            conn.execute("""INSERT INTO deliveries (id,batch_id,capture_id,recipient,kind,decision,note)
                VALUES (?,?,?,?,?,'pending','')""",
                         (uuid4().hex, batch_id, capture_id, recipient, 'reissue' if seen else 'initial'))
        return self.load_batch(batch_id)

    def decide_delivery(self, batch_id, delivery_id, decision, note):
        with self.session(write=True) as conn:
            self.editable_batch(conn, batch_id)
            row = conn.execute('SELECT capture_id FROM deliveries WHERE id=? AND batch_id=?',
                               (delivery_id, batch_id)).fetchone()
            if row is None:
                raise Fault('unknown_delivery', 'Delivery entry does not exist in this batch', 404)
            if decision == 'settled':
                state = conn.execute('SELECT state FROM captures WHERE id=?', (row['capture_id'],)).fetchone()['state']
                if state != 'certified':
                    raise Fault('deliver_not_certified', 'Settle a delivery only after the fixture is certified', 409)
            conn.execute('UPDATE deliveries SET decision=?, note=? WHERE id=?', (decision, note, delivery_id))
        return self.load_batch(batch_id)

    def close_batch(self, batch_id):
        with self.session(write=True) as conn:
            self.editable_batch(conn, batch_id)
            rows = conn.execute("""
                SELECT d.id, d.decision, d.note, c.state AS capture_state, c.certificate
                FROM deliveries d JOIN captures c ON c.id = d.capture_id WHERE d.batch_id = ?
            """, (batch_id,)).fetchall()
            pending = sum(1 for row in rows if row['decision'] == 'pending')
            if pending:
                raise Fault('batch_entries_pending', f'{pending} deliveries still need an operator decision', 409)
            for row in rows:
                if row['decision'] == 'settled':
                    if row['capture_state'] != 'certified' or not row['certificate']:
                        raise Fault('deliver_not_certified', 'Settle a delivery only after the fixture is certified', 409)
                    certificate = loads(row['certificate'])
                    conn.execute("UPDATE deliveries SET outcome='delivered', sent_sha256=?, sent_bytes=? WHERE id=?",
                                 (certificate['sha256'], certificate['bytes'], row['id']))
                elif row['decision'] == 'continued':
                    reason = row['note'] or ('回执未到，转入下批补发' if row['capture_state'] == 'certified'
                                             else '子库未认证，转入下批补发')
                    conn.execute("UPDATE deliveries SET outcome='reissue', outcome_reason=? WHERE id=?",
                                 (reason, row['id']))
                else:
                    conn.execute("UPDATE deliveries SET outcome='held', outcome_reason=? WHERE id=?",
                                 (row['note'] or '操作员暂缓', row['id']))
            conn.execute("UPDATE batches SET state='closed' WHERE id=?", (batch_id,))
        return self.load_batch(batch_id)

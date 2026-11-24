-- Onboarding database for draft foreign-key evaluation.
-- The tables intentionally declare NO foreign keys. Each pair is a hand-built
-- positive or negative case so the evaluation page can be re-checked by hand.
PRAGMA foreign_keys=OFF;

-- device.serial is a declared primary key: parent uniqueness is guaranteed from
-- the catalog alone, and reading rows finds every complete child key once.
-- Positive case (ready).
CREATE TABLE device (
    serial TEXT PRIMARY KEY,
    site TEXT NOT NULL
);

-- reading.device_serial always points at an existing device under BINARY, so the
-- data-level checks pass. The NULL row exercises the composite/single-column
-- NULL exemption: it has no required parent and is not counted as an orphan.
CREATE TABLE reading (
    reading_id INTEGER PRIMARY KEY,
    device_serial TEXT,
    channel TEXT
);

-- asset has a NOCASE natural key but NO usable unique constraint. The sample
-- happens to be unique, which is exactly the "unique in sample, not guaranteed"
-- case. A plain (non-unique) index makes the lookup intent explicit without
-- promising uniqueness.
CREATE TABLE asset (
    asset_id INTEGER PRIMARY KEY,
    tag TEXT COLLATE NOCASE NOT NULL,
    kind TEXT NOT NULL
);
CREATE INDEX asset_tag_lookup ON asset(tag);

-- ticket references asset.tag. Values match under NOCASE even though the binary
-- spellings differ ('AcMe' vs 'acme'). One row has a NULL tag (exempt), and one
-- duplicate asset tag makes the parent ambiguous after a second insert.
CREATE TABLE ticket (
    ticket_id INTEGER PRIMARY KEY,
    asset_tag TEXT COLLATE NOCASE,
    note TEXT NOT NULL
);

-- ledger.account_code looks unique in the sample but has no unique constraint,
-- mirroring the asset case for an integer-style code.
CREATE TABLE ledger (
    ledger_id INTEGER PRIMARY KEY,
    account_code TEXT NOT NULL,
    opened_on TEXT NOT NULL
);

-- payment is a clean data-level negative: code 'P-9' has no parent (orphan).
CREATE TABLE payment (
    payment_id INTEGER PRIMARY KEY,
    account_code TEXT NOT NULL,
    amount_cents INTEGER NOT NULL
);

BEGIN;
INSERT INTO device VALUES ('DEV-1', 'north'), ('DEV-2', 'south');
INSERT INTO reading VALUES
    (1, 'DEV-1', 'temp'),
    (2, 'DEV-1', 'humidity'),   -- same parent, repeated child key is allowed
    (3, NULL, 'idle'),          -- NULL child key: no parent required
    (4, 'DEV-2', 'pressure');

INSERT INTO asset (tag, kind) VALUES ('AcMe', 'gateway'), ('globex', 'sensor');
-- A second row collides under NOCASE ('acme' = 'AcMe') but not under BINARY.
INSERT INTO asset (tag, kind) VALUES ('acme', 'spare');
INSERT INTO ticket (ticket_id, asset_tag, note) VALUES
    (101, 'ACME', 'provisioned'),   -- matches both AcMe and acme under NOCASE: ambiguous
    (102, 'GLOBEX', 'renewed'),     -- matches globex under NOCASE, binary differs
    (103, NULL, 'blank');           -- NULL child key: exempt

INSERT INTO ledger (ledger_id, account_code, opened_on) VALUES
    (1, 'A-100', '2026-01-04'), (2, 'A-101', '2026-02-11');
INSERT INTO payment VALUES
    (201, 'A-100', 1200),
    (202, 'A-101', 850),
    (203, 'P-9', 400);             -- no ledger row: genuine orphan
COMMIT;

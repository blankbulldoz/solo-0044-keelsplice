PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
CREATE TABLE lot (
    code TEXT COLLATE NOCASE NOT NULL PRIMARY KEY,
    note TEXT NOT NULL
);
CREATE TABLE unit (
    serial INTEGER PRIMARY KEY,
    lot TEXT NOT NULL REFERENCES lot,
    prev INTEGER REFERENCES unit,
    remark TEXT NOT NULL
);
CREATE TABLE link (
    link_id INTEGER PRIMARY KEY,
    unit INTEGER NOT NULL,
    tag TEXT NOT NULL,
    FOREIGN KEY (unit) REFERENCES unit,
    FOREIGN KEY (unit) REFERENCES unit
);
BEGIN;
INSERT INTO lot VALUES ('L1','first'), ('L2','second');
INSERT INTO unit VALUES (1,'L1',NULL,'alpha'), (2,'L1',1,'beta'), (3,'L2',2,'gamma');
INSERT INTO link VALUES (10,1,'x'), (20,2,'y');
COMMIT;

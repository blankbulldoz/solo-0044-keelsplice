PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
CREATE TABLE lot (
    region TEXT COLLATE NOCASE NOT NULL,
    code TEXT COLLATE NOCASE NOT NULL,
    note TEXT NOT NULL,
    PRIMARY KEY (region, code)
) WITHOUT ROWID;
CREATE TABLE vial (
    rack TEXT COLLATE NOCASE NOT NULL,
    slot NOT NULL,
    fill TEXT NOT NULL,
    region TEXT,
    code TEXT,
    PRIMARY KEY (rack, slot),
    FOREIGN KEY (region, code) REFERENCES lot
);
BEGIN;
INSERT INTO lot VALUES ('East','Mica','kept');
INSERT INTO vial VALUES ('R1','7','brine','East','Mica'), ('R1',7,'oil',NULL,NULL);
COMMIT;

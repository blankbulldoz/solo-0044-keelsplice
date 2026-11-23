PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
CREATE TABLE substrate (
    region TEXT COLLATE NOCASE NOT NULL,
    code TEXT COLLATE NOCASE NOT NULL,
    texture BLOB NOT NULL,
    PRIMARY KEY (region, code)
) WITHOUT ROWID;
CREATE TABLE cassette (
    serial INTEGER PRIMARY KEY,
    prior INTEGER REFERENCES cassette,
    successor INTEGER REFERENCES cassette,
    label TEXT NOT NULL
);
CREATE TABLE assay (
    mark TEXT PRIMARY KEY NOT NULL,
    region TEXT,
    code TEXT,
    cassette INTEGER NOT NULL REFERENCES cassette,
    length_um REAL NOT NULL,
    doubled REAL GENERATED ALWAYS AS (length_um * 2) STORED,
    FOREIGN KEY (region, code) REFERENCES substrate
);
CREATE TABLE frame (
    frame_id INTEGER PRIMARY KEY,
    mark TEXT REFERENCES assay,
    note TEXT NOT NULL
);
CREATE INDEX assay_substrate_lookup ON assay(region,code) WHERE code IS NOT NULL;
CREATE INDEX frame_mark_lookup ON frame(mark);
BEGIN;
PRAGMA defer_foreign_keys=ON;
INSERT INTO substrate VALUES ('East','Mica',x'0000ff'), ('West','Basalt',x'fe');
INSERT INTO cassette (serial, prior, successor, label) VALUES
    (11,12,12,'opal'), (12,11,11,'agate'), (13,NULL,NULL,'garnet');
INSERT INTO assay(mark,region,code,cassette,length_um) VALUES
    ('S-01','east','MICA',11,3.25), ('S-02','EAST','mica',12,5.5),
    ('S-03','West',NULL,13,2.5), ('S-04','west','basalt',13,9.0);
INSERT INTO frame VALUES (101,'S-01','front'), (102,'S-02','detail'), (103,'S-02','side'), (104,NULL,'blank');
COMMIT;

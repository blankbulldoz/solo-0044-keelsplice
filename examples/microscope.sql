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
-- Exercises large BLOBs, values that look alike across storage classes, and a
-- virtual generated column that is never written by the workbench.
CREATE TABLE specimen (
    specimen_id INTEGER PRIMARY KEY,
    label TEXT NOT NULL,
    payload BLOB NOT NULL,
    note TEXT,
    tag INTEGER,
    alias_text TEXT GENERATED ALWAYS AS (label || '#' || specimen_id) VIRTUAL
);
CREATE INDEX assay_substrate_lookup ON assay(region,code) WHERE code IS NOT NULL;
CREATE INDEX frame_mark_lookup ON frame(mark);
BEGIN;
PRAGMA defer_foreign_keys=ON;
INSERT INTO substrate VALUES ('East','Mica',x'0000ff'), ('West','Basalt',x'fe');
INSERT INTO cassette VALUES (11,12,'opal'), (12,11,'agate'), (13,NULL,'garnet');
INSERT INTO assay(mark,region,code,cassette,length_um) VALUES
    ('S-01','east','MICA',11,3.25), ('S-02','EAST','mica',12,5.5),
    ('S-03','West',NULL,13,2.5), ('S-04','west','basalt',13,9.0);
INSERT INTO frame VALUES (101,'S-01','front'), (102,'S-02','detail'), (103,'S-02','side'), (104,NULL,'blank');
INSERT INTO specimen(specimen_id,label,payload,note,tag) VALUES
    (1,'alpha',x'030a11181f262d343b424950575e656c737a81888f969da4abb2b9c0c7ced5dce3eaf1f8ff060d141b222930373e454c535a61686f767d848b9299a0a7aeb5bcc3cad1d8dfe6edf4fb020910171e252c333a41484f565d646b727980878e959ca3aab1b8bfc6cdd4dbe2e9f0f7fe050c131a21282f363d444b525960676e757c838a91989fa6adb4bbc2c9d0d7dee5ecf3fa01080f161d242b323940474e555c636a71787f868d949ba2a9b0b7bec5ccd3dae1e8eff6fd040b121920272e353c434a51585f666d747b828990979ea5acb3bac1c8cfd6dde4ebf2f900070e151c232a31383f464d545b626970777e858c939aa1a8afb6bdc4cbd2d9e0e7eef5fc030a11181f262d343b424950575e656c737a81888f969da4abb2b9c0c7ced5dce3eaf1f8ff060d141b222930','twelve',12),
    (2,'beta',x'fe0d','12',12),
    (3,'gamma',x'deadbeef',NULL,NULL);
COMMIT;

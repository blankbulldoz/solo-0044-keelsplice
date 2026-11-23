PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
-- microscope 的漂移副本：assay 的底盘列 region 改名 area，
-- 复合外键的源列映射随之不同，用于演示列映射不一致的排障结论。
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
    area TEXT,
    code TEXT,
    cassette INTEGER NOT NULL REFERENCES cassette,
    length_um REAL NOT NULL,
    doubled REAL GENERATED ALWAYS AS (length_um * 2) STORED,
    FOREIGN KEY (area, code) REFERENCES substrate
);
CREATE TABLE frame (
    frame_id INTEGER PRIMARY KEY,
    mark TEXT REFERENCES assay,
    note TEXT NOT NULL
);
CREATE INDEX assay_substrate_lookup ON assay(area,code) WHERE code IS NOT NULL;
CREATE INDEX frame_mark_lookup ON frame(mark);

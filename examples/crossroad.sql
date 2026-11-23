PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
-- 路径排障演练用模式：复合外键、指向同一张表的两条外键、自环，
-- 以及只能从父表反向遍历到达的关系（hub → probe）。
CREATE TABLE hub (
    zone TEXT COLLATE NOCASE NOT NULL,
    code TEXT NOT NULL,
    note TEXT,
    PRIMARY KEY (zone, code)
) WITHOUT ROWID;
CREATE TABLE spoke (
    spoke_id INTEGER PRIMARY KEY,
    label TEXT NOT NULL
);
CREATE TABLE probe (
    probe_id INTEGER PRIMARY KEY,
    zone TEXT,
    code TEXT,
    spoke_id INTEGER REFERENCES spoke,
    backup_spoke INTEGER REFERENCES spoke,
    FOREIGN KEY (zone, code) REFERENCES hub
);
CREATE TABLE tether (
    tether_id INTEGER PRIMARY KEY,
    prior INTEGER REFERENCES tether,
    label TEXT
);

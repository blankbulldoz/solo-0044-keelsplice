-- 可公开重放的替代源库（合成数据）。
-- 与工位内部 microscope.sql 保持完全相同的表、主键、NOCASE 排序规则、
-- 外键、部分索引、生成列和自引用循环结构；所有键值与文字均为重新编造，
-- 不包含任何内部库的原始字面量。
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
CREATE INDEX assay_substrate_lookup ON assay(region,code) WHERE code IS NOT NULL;
CREATE INDEX frame_mark_lookup ON frame(mark);
BEGIN;
PRAGMA defer_foreign_keys=ON;
-- 两个复合 NOCASE 父键；assay 侧用不同大小写拼写命中同一父行。
INSERT INTO substrate VALUES ('North','Quartz',x'0a1b2c'), ('South','Granite',x'd7e8');
-- 盒循环：41 -> 42 -> 41；以及一个 prior 为 NULL 的孤立尾节点 43。
INSERT INTO cassette VALUES (41,42,'beryl'), (42,41,'jasper'), (43,NULL,'onyx');
INSERT INTO assay(mark,region,code,cassette,length_um) VALUES
    ('T-10','north','QUARTZ',41,4.5), ('T-11','NORTH','quartz',42,7.25),
    ('T-12','South',NULL,43,1.75), ('T-13','south','granite',43,6.0);
-- frame：T-10 一帧、T-11 两帧（共享子行扇出）、一条 mark 为 NULL 的帧（部分 NULL 关系）。
INSERT INTO frame VALUES (201,'T-10','entry'), (202,'T-11','closeup'), (203,'T-11','edge'), (204,NULL,'spare');
COMMIT;

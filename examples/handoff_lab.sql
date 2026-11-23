-- 接收试交付专用认证源库。
-- 无亲和性主键让整数 1 与文本 '1' 各自可区分；可空复合外键、负零、空串、
-- 空 BLOB、int64 极值和自引用边共同覆盖接收端的每一类表示风险。
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

-- slot 声明为 BLOB 但 WITHOUT ROWID 主键不施加亲和性，五类 SQLite 存储值可共存。
CREATE TABLE token (
    slot BLOB PRIMARY KEY,
    label TEXT NOT NULL
) WITHOUT ROWID;

-- 自引用整数主键表：文本中继重新导入时数字文本会按 INTEGER 亲和性重新解释。
CREATE TABLE node (
    node_id INTEGER PRIMARY KEY,
    prior INTEGER REFERENCES node,
    tag TEXT NOT NULL
);

-- 复合外键（含可空分量）；reading 为 REAL 亲和，raw_reading 无亲和，对照负零保真。
-- SQLite 在 REAL/NUMERIC 亲和列存储时会把负零归一为正零；无亲和列才保留符号位。
CREATE TABLE probe (
    probe_id TEXT PRIMARY KEY,
    slot BLOB REFERENCES token,
    region TEXT,
    code TEXT,
    reading REAL,
    raw_reading,
    sample BLOB,
    note TEXT NOT NULL,
    FOREIGN KEY (region, code) REFERENCES token_origin(region, code)
);

-- NOCASE 复合父表，验证复合 FK 仍能按原父行解析。
CREATE TABLE token_origin (
    region TEXT COLLATE NOCASE NOT NULL,
    code TEXT COLLATE NOCASE NOT NULL,
    PRIMARY KEY (region, code)
) WITHOUT ROWID;

CREATE INDEX probe_slot_lookup ON probe(slot) WHERE slot IS NOT NULL;
CREATE INDEX node_prior_lookup ON node(prior);

BEGIN;
PRAGMA defer_foreign_keys=ON;
INSERT INTO token_origin VALUES ('East','Mica'), ('West','Basalt');

-- 无亲和性主键：整数、纯数字文本、实数、BLOB 必须仍是六个可区分身份。
INSERT INTO token VALUES
    (9007199254740993, 'int64 beyond double precision'),
    (1, 'integer one'),
    ('1', 'numeric text one'),
    (2.5, 'real two and a half'),
    (-0.0, 'real negative zero parent'),
    (x'00ff', 'binary marker');

INSERT INTO node VALUES
    (1, NULL, 'seed'),
    (2, 1, 'links to seed'),
    (9223372036854775807, 2, 'max signed int64'),
    (-9223372036854775808, NULL, 'min signed int64'),
    (-0, NULL, 'negative zero stored in an integer column');

INSERT INTO probe(probe_id, slot, region, code, reading, raw_reading, sample, note) VALUES
    ('p-01', 1, 'east', 'MICA', -0.0, -0.0, x'00ff', 'negative zero: REAL column normalizes, no-affinity keeps sign'),
    ('p-02', '1', 'EAST', 'mica', 3.25, '3.25', x'', 'numeric text parent, empty blob'),
    ('p-03', 2.5, 'West', NULL, 0.0, '', NULL, 'nullable composite component and real zero'),
    ('p-04', x'00ff', 'west', 'basalt', 5.5, NULL, x'cafe', 'blob parent and a NULL raw reading'),
    ('p-05', NULL, NULL, NULL, NULL, NULL, NULL, 'all-nullable edge row'),
    ('p-06', 9007199254740993, 'East', 'Mica', 9.0, '9', x'', 'int64 identity beyond float precision'),
    ('p-07', -0.0, NULL, NULL, NULL, 'empty-string-collision', x'', 'real-valued slot and a genuine empty note slot');
UPDATE probe SET note='' WHERE probe_id='p-07';
COMMIT;

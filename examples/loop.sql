PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
CREATE TABLE loop (
    token TEXT COLLATE NOCASE NOT NULL,
    serial INTEGER NOT NULL,
    ratio REAL NOT NULL,
    prior_token TEXT,
    prior_serial INTEGER,
    prior_ratio REAL,
    note TEXT NOT NULL,
    PRIMARY KEY (token, serial, ratio),
    FOREIGN KEY (prior_token, prior_serial, prior_ratio)
        REFERENCES loop (token, serial, ratio)
);
CREATE TABLE probe (
    mark TEXT COLLATE NOCASE PRIMARY KEY NOT NULL,
    loop_token TEXT NOT NULL,
    loop_serial INTEGER NOT NULL,
    loop_ratio REAL NOT NULL,
    reading REAL NOT NULL,
    FOREIGN KEY (loop_token, loop_serial, loop_ratio)
        REFERENCES loop (token, serial, ratio)
);
BEGIN;
PRAGMA defer_foreign_keys=ON;
INSERT INTO loop VALUES
    ('alpha-east-ridge-mica-0000000000000001', 7, 1.5,
     'beta-west-ridge-basalt-000000000000002', 9, 2.5, '顺时针'),
    ('beta-west-ridge-basalt-000000000000002', 9, 2.5,
     'alpha-east-ridge-mica-0000000000000001', 7, 1.5, '逆时针');
INSERT INTO probe VALUES
    ('P-01', 'BETA-WEST-RIDGE-BASALT-000000000000002', 9, 2.5, 0.75);
COMMIT;

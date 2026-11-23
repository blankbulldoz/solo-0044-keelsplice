-- 盲审训练卷源库：项目现场登记数据（与 examples/microscope.sql 的演示域完全独立）。
-- 由 training/build/build_kits.py 灌入全新的 SQLite 文件，可重复初始化。
PRAGMA foreign_keys=ON;

CREATE TABLE staff (
    staff_id TEXT PRIMARY KEY,
    team TEXT NOT NULL,
    lead_id TEXT REFERENCES staff(staff_id)
);
CREATE TABLE project (
    proj_id TEXT PRIMARY KEY,
    program TEXT NOT NULL,
    stage TEXT NOT NULL CHECK(stage IN ('open', 'review', 'closed'))
);
CREATE TABLE site (
    site_id TEXT PRIMARY KEY,
    proj_id TEXT NOT NULL REFERENCES project(proj_id),
    region TEXT NOT NULL
);
CREATE TABLE reading (
    reading_id INTEGER PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES site(site_id),
    taken_on TEXT NOT NULL,
    value_um REAL NOT NULL,
    reviewer_id TEXT REFERENCES staff(staff_id)
);
CREATE INDEX reading_site_lookup ON reading(site_id);
CREATE INDEX site_project_lookup ON site(proj_id);

BEGIN;
PRAGMA defer_foreign_keys=ON;
-- 带队内自引用环：S1 与 S2 互为 lead。
INSERT INTO staff VALUES
    ('S1', 'optics', 'S2'),
    ('S2', 'optics', 'S1'),
    ('S3', 'optics', 'S2'),
    ('S4', 'field', NULL);
INSERT INTO project VALUES
    ('P1', 'aurora', 'open'),
    ('P2', 'borealis', 'review'),
    ('P3', 'cygnus', 'open'),
    ('P4', 'draco', 'closed'),
    ('P5', 'lyra', 'open'),
    ('P6', 'vega', 'review');
INSERT INTO site VALUES
    ('ST-1', 'P1', 'north'),
    ('ST-2', 'P1', 'south'),
    ('ST-3', 'P2', 'east'),
    ('ST-4', 'P2', 'west'),
    ('ST-5', 'P3', 'north'),
    ('ST-6', 'P4', 'south'),
    ('ST-7', 'P5', 'east');
INSERT INTO reading VALUES
    (1, 'ST-1', '2026-10-02', 3.25, 'S1'),
    (2, 'ST-1', '2026-10-03', 3.40, NULL),
    (3, 'ST-2', '2026-10-03', 5.50, 'S2'),
    (4, 'ST-3', '2026-10-04', 2.10, 'S3'),
    (5, 'ST-3', '2026-10-05', 2.25, NULL),
    (6, 'ST-4', '2026-10-05', 9.00, 'S1'),
    (7, 'ST-5', '2026-10-06', 4.75, 'S4'),
    (8, 'ST-5', '2026-10-07', 4.80, NULL),
    (9, 'ST-6', '2026-10-08', 6.10, 'S2'),
    (10, 'ST-7', '2026-10-09', 7.30, NULL);
COMMIT;

PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

-- Shape B: several roots that all resolve to the same parent key.  The roots
-- share one primary-key seek, and the required parent is looked up once.  The
-- partial index looks tempting by column name but cannot serve the root lookup,
-- and the BINARY namesake on the parent is measurably worse than its key.
CREATE TABLE parent_node (
    region TEXT COLLATE NOCASE NOT NULL,
    code   TEXT COLLATE NOCASE NOT NULL,
    label  TEXT,
    PRIMARY KEY (region, code)
) WITHOUT ROWID;

CREATE TABLE leaf_node (
    tag    TEXT PRIMARY KEY,
    region TEXT NOT NULL,
    code   TEXT NOT NULL,
    FOREIGN KEY (region, code) REFERENCES parent_node
);

-- Partial child index: its predicate is not implied by a primary-key lookup on
-- leaf_node, so SQLite refuses to use it for the root read.
CREATE INDEX leaf_region_code ON leaf_node(region, code) WHERE code IS NOT NULL;
-- BINARY namesake on the parent: cannot serve the NOCASE parent key lookup.
CREATE INDEX parent_namesake_binary ON parent_node(region COLLATE BINARY, code COLLATE BINARY);

BEGIN;
INSERT INTO parent_node VALUES ('East', 'Mica', 'shared seafloor');
INSERT INTO leaf_node VALUES
    ('LEAF-0', 'east', 'mica'),
    ('LEAF-1', 'EAST', 'MICA'),
    ('LEAF-2', 'East', 'Mica');
COMMIT;

PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

-- Shape A: a NOCASE parent key reached by a matching partial child index, plus
-- three namesake indexes whose column names overlap but whose collation or
-- column order means they cannot serve the reads the capture engine performs.
CREATE TABLE seam (
    region TEXT COLLATE NOCASE NOT NULL,
    code   TEXT COLLATE NOCASE NOT NULL,
    texture BLOB NOT NULL,
    PRIMARY KEY (region, code)
) WITHOUT ROWID;

CREATE TABLE spline (
    serial INTEGER PRIMARY KEY,
    region TEXT,
    code   TEXT,
    weight INTEGER NOT NULL,
    FOREIGN KEY (region, code) REFERENCES seam
);

-- Serves the child fan-out: NOCASE, code predicate the join implies, and it
-- prunes the NULL-keyed rows outright.
CREATE INDEX spline_match_nocase
    ON spline(region COLLATE NOCASE, code COLLATE NOCASE) WHERE code IS NOT NULL;
-- Same columns, wrong collation: only ever a scan.
CREATE INDEX spline_namesake_binary ON spline(region, code);
-- Same columns, reversed order: only ever a scan.
CREATE INDEX spline_namesake_reversed ON spline(code, region);
-- Same column names on the parent, BINARY collation: cannot serve a NOCASE
-- primary-key lookup, and forcing it is measurably worse than the plain key.
CREATE INDEX seam_namesake_binary ON seam(region COLLATE BINARY, code COLLATE BINARY);

BEGIN;
INSERT INTO seam VALUES ('East', 'Mica', x'0000ff');
-- 2,000 matching children, then 18,000 rows with a NULL code that a partial
-- index can skip entirely.
WITH RECURSIVE n(i) AS (SELECT 0 UNION ALL SELECT i + 1 FROM n WHERE i < 19999)
INSERT INTO spline(serial, region, code, weight)
SELECT i,
       CASE WHEN i < 2000 THEN 'east' ELSE 'North' END,
       CASE WHEN i < 2000 THEN 'mica' ELSE NULL END,
       i % 9
FROM n;
COMMIT;

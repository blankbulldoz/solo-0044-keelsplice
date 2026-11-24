PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

-- Table and index names deliberately contain double quotes and spaces so every
-- identifier that reaches SQLite must be quoted with the embedded quotes
-- doubled. The primary key is a composite (NOCASE text, exact BLOB).
CREATE TABLE "slab ""core""" (
    region TEXT COLLATE NOCASE NOT NULL,
    token BLOB NOT NULL,
    caption TEXT NOT NULL,
    PRIMARY KEY (region, token)
) WITHOUT ROWID;

-- Two foreign keys point at the same parent with the same parent columns, so
-- the pair (child table, foreign key number) is the only stable edge identity.
CREATE TABLE "link ""pair""" (
    link_id INTEGER PRIMARY KEY,
    left_region TEXT COLLATE NOCASE NOT NULL,
    left_token BLOB NOT NULL,
    right_region TEXT COLLATE NOCASE NOT NULL,
    right_token BLOB NOT NULL,
    note TEXT NOT NULL,
    FOREIGN KEY (left_region, left_token) REFERENCES "slab ""core"""(region, token),
    FOREIGN KEY (right_region, right_token) REFERENCES "slab ""core"""(region, token)
);

-- Same-named columns: the child column names equal the parent key names.
CREATE TABLE "mirror ""copy""" (
    region TEXT COLLATE NOCASE NOT NULL,
    token BLOB NOT NULL,
    note TEXT,
    PRIMARY KEY (region, token),
    FOREIGN KEY (region, token) REFERENCES "slab ""core"""(region, token)
) WITHOUT ROWID;

CREATE TABLE "read ""out""" (
    seq INTEGER PRIMARY KEY,
    slab_region TEXT COLLATE NOCASE,
    slab_token BLOB,
    observed TEXT NOT NULL,
    FOREIGN KEY (slab_region, slab_token) REFERENCES "slab ""core"""(region, token)
);

CREATE INDEX "read ""lookup""" ON "read ""out"""(slab_region, slab_token);

BEGIN;
PRAGMA defer_foreign_keys=ON;
INSERT INTO "slab ""core""" VALUES
    ('East', x'00ff', 'east core'),
    ('West', x'fe00', 'west core'),
    ('east2', x'cafe', 'second east'),
    -- A long text component and a long binary component so displays must gloss
    -- values instead of dumping them, while the exact key stays intact.
    ('north-east corridor with a very long readable label', x'000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f', 'long core');
INSERT INTO "link ""pair""" VALUES
    (1, 'east', x'00ff', 'WEST', x'fe00', 'bridge'),
    (2, 'EAST', x'00ff', 'east2', x'cafe', 'parallel'),
    (3, 'west', x'fe00', 'west', x'fe00', 'self');
INSERT INTO "mirror ""copy""" VALUES
    ('East', x'00ff', 'copy east'),
    ('west', x'fe00', 'copy west');
INSERT INTO "read ""out""" VALUES
    (10, 'east', x'00ff', 'first'),
    (11, 'EAST', x'00ff', 'second'),
    (12, NULL, NULL, 'loose');
COMMIT;

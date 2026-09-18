BEGIN IMMEDIATE;

CREATE TABLE IF NOT EXISTS reservations (
    id TEXT PRIMARY KEY,
    tenant TEXT NOT NULL,
    creator TEXT NOT NULL,
    quantity INTEGER NOT NULL CHECK (quantity BETWEEN 1 AND 8),
    status TEXT NOT NULL CHECK (status IN ('reserved', 'approved', 'cancelled', 'fulfilled')),
    seq INTEGER NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', '1');
COMMIT;

BEGIN IMMEDIATE;

CREATE TABLE IF NOT EXISTS outbox (
    id TEXT PRIMARY KEY,
    reservation_id TEXT NOT NULL UNIQUE REFERENCES reservations(id),
    tenant TEXT NOT NULL,
    quantity INTEGER NOT NULL CHECK (quantity BETWEEN 1 AND 8),
    delivery_key TEXT NOT NULL UNIQUE,
    state TEXT NOT NULL CHECK (state IN ('pending', 'leased', 'done')),
    fence INTEGER NOT NULL DEFAULT 0,
    lease_until INTEGER,
    receipt TEXT
);

CREATE TABLE IF NOT EXISTS commands (
    actor TEXT NOT NULL,
    key TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    response TEXT NOT NULL,
    PRIMARY KEY (actor, key)
);

CREATE TABLE IF NOT EXISTS identities (
    token TEXT PRIMARY KEY,
    tenant TEXT NOT NULL,
    role TEXT NOT NULL,
    active INTEGER NOT NULL CHECK (active IN (0, 1))
);

INSERT OR IGNORE INTO identities(token, tenant, role, active) VALUES
    ('alpha-maker', 'alpha', 'maker', 1),
    ('alpha-reviewer', 'alpha', 'reviewer', 1),
    ('beta-maker', 'beta', 'maker', 1),
    ('beta-reviewer', 'beta', 'reviewer', 1),
    ('synthetic-worker', 'system', 'worker', 1),
    ('synthetic-control', 'system', 'control', 1);

INSERT OR IGNORE INTO meta(key, value) VALUES ('virtual_clock', '0');
UPDATE meta SET value = '2' WHERE key = 'schema_version';
COMMIT;

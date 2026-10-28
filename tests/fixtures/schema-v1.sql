CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE events (
    identity TEXT PRIMARY KEY,
    announced_at TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE TABLE provider_state (provider TEXT PRIMARY KEY, cooldown_until REAL NOT NULL);
CREATE TABLE endpoint_state (
    webhook_id TEXT PRIMARY KEY,
    paused INTEGER NOT NULL DEFAULT 0,
    reason TEXT,
    cooldown_until REAL NOT NULL DEFAULT 0
);
CREATE TABLE deliveries (
    id INTEGER PRIMARY KEY,
    event_identity TEXT NOT NULL REFERENCES events(identity),
    webhook_id TEXT NOT NULL,
    detected_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending', 'delivered')),
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at REAL NOT NULL DEFAULT 0,
    last_error TEXT,
    UNIQUE(event_identity, webhook_id)
);
CREATE INDEX pending_deliveries ON deliveries(webhook_id, status);
PRAGMA user_version = 1;

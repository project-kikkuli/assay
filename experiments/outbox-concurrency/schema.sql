-- Adapted from ../outbox/schema.sql, which is the public MIT fixture.
DROP TABLE IF EXISTS ledger_effects;
DROP TABLE IF EXISTS processed_events;
DROP TABLE IF EXISTS outbox_events;
DROP TABLE IF EXISTS accepted_commands;

CREATE TABLE accepted_commands (
  command_id text PRIMARY KEY
);

CREATE TABLE outbox_events (
  event_id text PRIMARY KEY,
  command_id text NOT NULL UNIQUE REFERENCES accepted_commands(command_id),
  status text NOT NULL CHECK (status IN ('pending', 'published'))
);

CREATE TABLE processed_events (
  event_id text PRIMARY KEY REFERENCES outbox_events(event_id)
);

CREATE TABLE ledger_effects (
  command_id text PRIMARY KEY REFERENCES accepted_commands(command_id),
  effect text NOT NULL
);

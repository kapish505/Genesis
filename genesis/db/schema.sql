PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agents (
  spec_hash TEXT PRIMARY KEY,
  agent_address TEXT NOT NULL,
  handle TEXT,
  title TEXT NOT NULL,
  status TEXT NOT NULL,
  gateway_token_id TEXT,
  runtime_state_json TEXT NOT NULL DEFAULT '{}',
  consecutive_failures INTEGER NOT NULL DEFAULT 0,
  last_used_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS specs (
  spec_hash TEXT PRIMARY KEY,
  spec_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS builds (
  build_id TEXT PRIMARY KEY,
  spec_hash TEXT NOT NULL,
  session_id TEXT,
  state TEXT NOT NULL,
  agent_address TEXT,
  handle TEXT,
  code_hash TEXT,
  failure_stage TEXT,
  failure_reason TEXT,
  attempts INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS builds_one_active_per_spec
  ON builds(spec_hash)
  WHERE state NOT IN ('DONE','FAILED','COMPENSATED');

CREATE INDEX IF NOT EXISTS builds_active
  ON builds(state, updated_at);

CREATE TABLE IF NOT EXISTS build_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  build_id TEXT NOT NULL,
  state TEXT NOT NULL,
  detail TEXT,
  created_at TEXT NOT NULL,
  FOREIGN KEY(build_id) REFERENCES builds(build_id)
);

CREATE TABLE IF NOT EXISTS sessions (
  session_id TEXT PRIMARY KEY,
  sender_address TEXT NOT NULL,
  external_session_id TEXT,
  state TEXT NOT NULL,
  data_json TEXT NOT NULL,
  pending_action TEXT,
  pending_actions_json TEXT NOT NULL DEFAULT '[]',
  pending_since REAL NOT NULL DEFAULT 0,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS sessions_sender
  ON sessions(sender_address, external_session_id);

CREATE TABLE IF NOT EXISTS usage (
  sender_address TEXT NOT NULL,
  bucket TEXT NOT NULL,
  window_start REAL NOT NULL,
  count INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (sender_address, bucket)
);

CREATE TABLE IF NOT EXISTS payments (
  reference TEXT PRIMARY KEY,
  session_id TEXT,
  status TEXT NOT NULL,
  rail TEXT,
  transaction_id TEXT,
  amount TEXT,
  currency TEXT,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS capability_requests (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  sender_address TEXT NOT NULL,
  request_text TEXT NOT NULL,
  reason TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS message_ids (
  msg_id TEXT PRIMARY KEY,
  seen_at REAL NOT NULL
);

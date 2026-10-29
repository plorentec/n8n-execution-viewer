"""DDL for the viewer's own history database (n8n's database is never touched)."""
import sqlite3

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS workflows (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 0,
    deleted_at TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS executions (
    id INTEGER PRIMARY KEY,
    workflow_id TEXT NOT NULL,
    status TEXT NOT NULL,
    mode TEXT,
    started_at TEXT NOT NULL,
    stopped_at TEXT,
    duration_ms INTEGER,
    last_node TEXT,
    error_message TEXT,
    error_node TEXT,
    json_size INTEGER,
    deleted_at TEXT,
    data_missing INTEGER NOT NULL DEFAULT 0,
    parse_error TEXT,
    imported_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_exec_workflow ON executions(workflow_id);
CREATE INDEX IF NOT EXISTS idx_exec_started ON executions(started_at);
CREATE INDEX IF NOT EXISTS idx_exec_status ON executions(status);

CREATE TABLE IF NOT EXISTS executions_data (
    execution_id INTEGER PRIMARY KEY REFERENCES executions(id) ON DELETE CASCADE,
    data TEXT NOT NULL
);

CREATE VIRTUAL TABLE IF NOT EXISTS fts_executions USING fts5(
    execution_id UNINDEXED,
    workflow_id UNINDEXED,
    node_name UNINDEXED,
    text
);

CREATE TABLE IF NOT EXISTS sync_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def init_history(path):
    con = sqlite3.connect(path)
    con.executescript(SCHEMA)
    con.commit()
    con.close()

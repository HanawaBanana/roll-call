"""SQLite storage: schema, connection factory, row helpers.

Everything lives in one file so the whole app is offline-first: no service, no
network, no account.  The attendance log is append-only by convention and is
protected by the hash chain in :mod:`rollcall.chain`.
"""

from __future__ import annotations

import os
import sqlite3

SCHEMA = """
PRAGMA journal_mode = MEMORY;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS sessions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    label        TEXT    NOT NULL,
    location     TEXT    NOT NULL DEFAULT '',
    lat          REAL,
    lon          REAL,
    opens_at     TEXT    NOT NULL,
    closes_at    TEXT    NOT NULL,
    step_seconds INTEGER NOT NULL,
    secret       TEXT    NOT NULL,
    closed_at    TEXT,
    created_at   TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS participants (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ref        TEXT    NOT NULL UNIQUE,
    name       TEXT    NOT NULL,
    created_at TEXT    NOT NULL
);

-- Append-only attendance log.  Every row is sealed with prev_hash -> entry_hash.
CREATE TABLE IF NOT EXISTS attendance (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    seq           INTEGER NOT NULL UNIQUE,
    session_id    INTEGER NOT NULL REFERENCES sessions(id),
    participant_id INTEGER NOT NULL REFERENCES participants(id),
    kind          TEXT    NOT NULL,          -- checkin | duplicate
    code_used     TEXT    NOT NULL,
    code_step     INTEGER NOT NULL,          -- rotation step the presented code belongs to
    session_step  INTEGER NOT NULL,          -- rotation step live when the row was accepted
    checked_in_at TEXT    NOT NULL,
    recorded_at   TEXT    NOT NULL,
    source        TEXT    NOT NULL DEFAULT 'web',
    note          TEXT    NOT NULL DEFAULT '',
    prev_hash     TEXT    NOT NULL,
    entry_hash    TEXT    NOT NULL
);

-- Head pointer.  Kept outside the log so truncating the log is detectable too.
CREATE TABLE IF NOT EXISTS chain_state (
    id         INTEGER PRIMARY KEY CHECK (id = 1),
    last_seq   INTEGER NOT NULL,
    head_hash  TEXT    NOT NULL,
    updated_at TEXT    NOT NULL
);

-- Rejected check-in attempts (expired / malformed / unknown code).
CREATE TABLE IF NOT EXISTS rejected_attempts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id      INTEGER,
    participant_ref TEXT,
    code            TEXT,
    reason          TEXT,
    at              TEXT
);

CREATE INDEX IF NOT EXISTS idx_attendance_session ON attendance(session_id);
CREATE INDEX IF NOT EXISTS idx_attendance_participant ON attendance(participant_id);
"""


def connect(path: str = "rollcall.sqlite3") -> sqlite3.Connection:
    """Open (and if needed create) the database at *path*."""
    if path != ":memory:":
        parent = os.path.dirname(os.path.abspath(path))
        if parent and not os.path.isdir(parent):
            os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(path, timeout=15)
    # Autocommit mode: transactions are managed explicitly (chain.tx).
    conn.isolation_level = None
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def rows_to_dicts(rows) -> list:
    return [dict(r) for r in rows]


def one(conn: sqlite3.Connection, sql: str, args=()) -> dict:
    row = conn.execute(sql, args).fetchone()
    return dict(row) if row is not None else None


def many(conn: sqlite3.Connection, sql: str, args=()) -> list:
    return [dict(r) for r in conn.execute(sql, args).fetchall()]

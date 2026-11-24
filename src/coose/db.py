"""The server database: accounts, story admins and which story is running.

Each story keeps its own live game data in a separate database inside its
folder (see stories.py); this one only holds what belongs to the server.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    username      TEXT PRIMARY KEY COLLATE NOCASE,
    password_hash TEXT NOT NULL,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS story_admins (
    username TEXT NOT NULL COLLATE NOCASE REFERENCES accounts(username) ON DELETE CASCADE,
    story    TEXT NOT NULL,
    PRIMARY KEY (username, story)
);

CREATE TABLE IF NOT EXISTS server_state (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def connect(path: Path) -> sqlite3.Connection:
    """Open the server database, creating it and its tables if needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(SCHEMA)
    return conn


def get_state(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM server_state WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_state(conn: sqlite3.Connection, key: str, value: str | None) -> None:
    with conn:
        conn.execute(
            "INSERT INTO server_state (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

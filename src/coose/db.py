"""The server database: accounts, story admins and which story is running.

Each story keeps its own live game data in a separate database inside its
folder (see stories.py); this one only holds what belongs to the server.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from . import pricing

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

-- One row per Claude API call, so the owner can see what play costs.
CREATE TABLE IF NOT EXISTS ai_usage (
    id                 INTEGER PRIMARY KEY,
    at                 TEXT NOT NULL DEFAULT (datetime('now')),
    story              TEXT,
    pass               TEXT NOT NULL,
    model              TEXT NOT NULL,
    input_tokens       INTEGER NOT NULL,
    output_tokens      INTEGER NOT NULL,
    cache_read_tokens  INTEGER NOT NULL,
    cache_write_tokens INTEGER NOT NULL,
    seconds            REAL NOT NULL,
    stop_reason        TEXT,
    cost_usd           REAL   -- estimated; NULL when the model has no known price
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
    columns = {r["name"] for r in conn.execute("PRAGMA table_info(ai_usage)")}
    if "cost_usd" not in columns:  # databases from before cost estimates
        conn.execute("ALTER TABLE ai_usage ADD COLUMN cost_usd REAL")
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


def log_usage(conn: sqlite3.Connection, story: str | None, record: dict) -> None:
    cost = pricing.estimate(record["model"], record["input_tokens"], record["output_tokens"],
                            record["cache_read_tokens"], record["cache_write_tokens"],
                            record.get("cache_ttl", "5m"), record.get("cache_write_1h_tokens"))
    with conn:
        conn.execute(
            "INSERT INTO ai_usage (story, pass, model, input_tokens, output_tokens, cache_read_tokens, "
            "cache_write_tokens, seconds, stop_reason, cost_usd) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (story, record["pass"], record["model"], record["input_tokens"], record["output_tokens"],
             record["cache_read_tokens"], record["cache_write_tokens"], record["seconds"],
             record.get("stop_reason"), cost),
        )


def usage_summary(conn: sqlite3.Connection, story: str | None = None, since: str | None = None) -> list[sqlite3.Row]:
    """Totals per pass: calls, tokens, average seconds."""
    where, args = [], []
    if story:
        where.append("story = ?")
        args.append(story)
    if since:
        where.append("at >= ?")
        args.append(since)
    return conn.execute(
        "SELECT pass, COUNT(*) AS calls, SUM(input_tokens) AS input, SUM(output_tokens) AS output, "
        "SUM(cache_read_tokens) AS cache_read, SUM(cache_write_tokens) AS cache_write, "
        "ROUND(AVG(seconds), 1) AS avg_seconds, ROUND(SUM(cost_usd), 4) AS cost FROM ai_usage"
        + (" WHERE " + " AND ".join(where) if where else "")
        + " GROUP BY pass ORDER BY calls DESC",
        args,
    ).fetchall()

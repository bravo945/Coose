"""Rounds, time skips, the Logbook and snapshots.

Characters act freely; a round is a reconciliation event. It comes after a
set number of Actions, when every online player votes for it, or when a time
skip can't wait. At a round change the AI rewrites the World State, decides
what the working memory still needs, updates every character, and writes
each one a short in-character notice. The old World State and a summary go
to the story's Logbook, and the story database is snapshotted so a story
admin can roll back to the start of any round.

Round state lives in the story database's `world` table, next to the clock.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from . import game
from .game import _get, _set, _write_lock

SNAPSHOT_DIR = Path(".coose") / "snapshots"
LOGBOOK_DIR = "Logbook"


class RoundError(game.GameError):
    pass


# --- Round state ----------------------------------------------------------------

def current_round(conn: sqlite3.Connection) -> int:
    return int(_get(conn, "round", "1"))


def round_actions(conn: sqlite3.Connection) -> int:
    return int(_get(conn, "round_actions", "0"))


def round_due(conn: sqlite3.Connection, settings: game.Settings) -> bool:
    return round_actions(conn) >= settings.actions_per_round


@dataclass(frozen=True)
class PendingSkip:
    character_id: int
    minutes: int
    action: str


def pending_skips(conn: sqlite3.Connection) -> list[PendingSkip]:
    return [PendingSkip(**s) for s in json.loads(_get(conn, "pending_skips", "[]"))]


def longest_skip(conn: sqlite3.Connection) -> int:
    return max((s.minutes for s in pending_skips(conn)), default=0)


def is_skipping(conn: sqlite3.Connection, character_id: int) -> bool:
    return any(s.character_id == character_id for s in pending_skips(conn))


def add_skip(conn: sqlite3.Connection, character_id: int, minutes: int, action: str) -> None:
    with _write_lock, conn:
        skips = [s.__dict__ for s in pending_skips(conn) if s.character_id != character_id]
        skips.append({"character_id": character_id, "minutes": int(minutes), "action": action[:500]})
        _set(conn, "pending_skips", json.dumps(skips))


def votes(conn: sqlite3.Connection) -> list[str]:
    return json.loads(_get(conn, "round_votes", "[]"))


def add_vote(conn: sqlite3.Connection, username: str) -> list[str]:
    with _write_lock, conn:
        current = votes(conn)
        if username.lower() not in (v.lower() for v in current):
            current.append(username)
        _set(conn, "round_votes", json.dumps(current))
    return current


def vote_passes(conn: sqlite3.Connection, online: list[str]) -> bool:
    """Every online player has voted (and someone is online)."""
    voted = {v.lower() for v in votes(conn)}
    return bool(online) and all(u.lower() in voted for u in online)


def backfills(conn: sqlite3.Connection) -> dict[int, int]:
    """Offline characters owed a backstory for skipped time: id -> minutes."""
    return {int(k): v for k, v in json.loads(_get(conn, "backfill", "{}")).items()}


def take_backfill(conn: sqlite3.Connection, character_id: int) -> int:
    with _write_lock, conn:
        owed = backfills(conn)
        minutes = owed.pop(character_id, 0)
        _set(conn, "backfill", json.dumps(owed))
    return minutes


def owe_backfill(conn: sqlite3.Connection, character_id: int, minutes: int) -> None:
    with _write_lock, conn:
        owed = backfills(conn)
        owed[character_id] = owed.get(character_id, 0) + minutes
        _set(conn, "backfill", json.dumps({str(k): v for k, v in owed.items()}))


def round_events(conn: sqlite3.Connection, limit: int = 300) -> list[tuple[str, str, str]]:
    """What players did and saw this round, as (character, kind, text)."""
    since = int(_get(conn, "round_start_message", "0"))
    rows = conn.execute(
        "SELECT * FROM (SELECT c.name, m.kind, m.text, m.id FROM messages m JOIN characters c ON c.id = m.character_id "
        "WHERE m.id > ? AND m.kind IN ('action', 'narration', 'dialogue', 'witness') ORDER BY m.id DESC LIMIT ?) "
        "ORDER BY id",
        (since, limit),
    )
    return [(r["name"], r["kind"], r["text"][:600]) for r in rows]


# --- Applying a round change ----------------------------------------------------

@dataclass
class CharacterUpdate:
    name: str
    notice: str = ""
    condition: str = ""
    location: str = ""
    learned: list[str] = field(default_factory=list)
    alive: bool = True


@dataclass
class RoundOutcome:
    """What the round-change pass decided. Mirrors narrator.RoundResult."""

    title: str
    summary: str
    relevant_characters: str
    deaths: str
    world_state: str
    working_memory: list[str]
    characters: list[CharacterUpdate] = field(default_factory=list)


def apply_round(conn: sqlite3.Connection, story_path: Path, settings: game.Settings, outcome: RoundOutcome,
                online_ids: set[int]) -> int:
    """Close the current round and open the next. Returns the new round number.

    Archives the old World State in the Logbook, advances the clock by the
    longest pending skip, updates characters and delivers notices, then
    snapshots the database as the start of the new round.
    """
    number = current_round(conn)
    old_state = game.world_state(conn)
    skip = longest_skip(conn)
    write_logbook(story_path, number, outcome, old_state)
    by_name = {u.name.strip().lower(): u for u in outcome.characters}
    with _write_lock:
        characters = [game.Character.from_row(r) for r in conn.execute("SELECT * FROM characters WHERE alive = 1")]
        owed = backfills(conn)
        with conn:
            _set(conn, "clock_minutes", str(game.clock_minutes(conn) + skip))
            _set(conn, "world_state", outcome.world_state.strip() or old_state)
            _set(conn, "working_memory", json.dumps([m for m in outcome.working_memory if m.strip()]))
            for c in characters:
                update = by_name.get(c.name.lower())
                if skip and c.id not in online_ids and settings.offline_during_skips == "backfill_on_return":
                    owed[c.id] = owed.get(c.id, 0) + skip  # left untouched; a backstory comes on return
                    continue
                if update is None:
                    continue
                conn.execute(
                    "UPDATE characters SET condition = ?, location = ?, knowledge = ?, alive = ? WHERE id = ?",
                    (update.condition.strip() or c.condition, update.location.strip() or c.location,
                     json.dumps(c.knowledge + [k for k in update.learned if k.strip()]), int(bool(update.alive)),
                     c.id),
                )
                if update.notice.strip():
                    conn.execute(
                        "INSERT INTO messages (character_id, kind, text, world_minutes) VALUES (?, 'round', ?, ?)",
                        (c.id, f"Round {number + 1}. {update.notice.strip()}", game.clock_minutes(conn)),
                    )
            _set(conn, "backfill", json.dumps({str(k): v for k, v in owed.items()}))
            _set(conn, "pending_skips", "[]")
            _set(conn, "round_votes", "[]")
            _set(conn, "round_actions", "0")
            _set(conn, "round", str(number + 1))
            last = conn.execute("SELECT COALESCE(MAX(id), 0) FROM messages").fetchone()[0]
            _set(conn, "round_start_message", str(last))
    snapshot(story_path, conn, number + 1)
    return number + 1


def write_logbook(story_path: Path, number: int, outcome: RoundOutcome, old_state: str) -> Path:
    """Archive a finished round, following Logbook/Round Format.md."""
    folder = story_path / LOGBOOK_DIR
    folder.mkdir(exist_ok=True)
    title = " ".join(outcome.title.split())[:80] or "Untitled"
    path = folder / f"Round {number}.md"
    path.write_text(
        f"# Round {number} ({title})\n\n"
        f"## Summary\n{outcome.summary.strip() or 'Nothing recorded.'}\n\n"
        f"## Relevant Characters\n{outcome.relevant_characters.strip() or 'None.'}\n\n"
        f"## Deaths\n{outcome.deaths.strip() or 'None.'}\n\n"
        f"## World State at the start of the round\n{old_state.strip()}\n",
        encoding="utf-8",
    )
    return path


# --- Snapshots and rollback -----------------------------------------------------

def snapshot_path(story_path: Path, number: int) -> Path:
    return story_path / SNAPSHOT_DIR / f"round-{number}.db"


def snapshot(story_path: Path, conn: sqlite3.Connection, number: int) -> Path:
    """Save the whole story database as it stands at the start of a round."""
    path = snapshot_path(story_path, number)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    dest = sqlite3.connect(tmp)
    try:
        conn.backup(dest)
    finally:
        dest.close()
    tmp.replace(path)
    return path


def snapshots(story_path: Path) -> list[int]:
    folder = story_path / SNAPSHOT_DIR
    if not folder.is_dir():
        return []
    return sorted(int(p.stem.split("-")[1]) for p in folder.glob("round-*.db"))


def rollback(story_path: Path, conn: sqlite3.Connection, number: int) -> None:
    """Restore the story to the start of round `number`.

    Characters, knowledge, time and chat after that point are undone, along
    with later Logbook entries and snapshots. Who is playing which character
    is kept where that character still exists.
    """
    source_path = snapshot_path(story_path, number)
    if not source_path.exists():
        raise RoundError(f"There's no snapshot for round {number}. Available: "
                         f"{', '.join(map(str, snapshots(story_path))) or 'none'}.")
    with _write_lock:
        players = [tuple(r) for r in conn.execute("SELECT username, character_id, since FROM players")]
        source = sqlite3.connect(source_path)
        try:
            source.backup(conn)
        finally:
            source.close()
        with conn:
            conn.execute("DELETE FROM players")
            for username, character_id, since in players:
                conn.execute(
                    "INSERT INTO players (username, character_id, since) SELECT ?, id, ? FROM characters "
                    "WHERE id = ? AND alive = 1",
                    (username, since, character_id),
                )
    for later in snapshots(story_path):
        if later > number:
            snapshot_path(story_path, later).unlink()
    logbook = story_path / LOGBOOK_DIR
    for entry in logbook.glob("Round *.md") if logbook.is_dir() else ():
        n = entry.stem.removeprefix("Round ").strip()
        if n.isdigit() and int(n) >= number:
            entry.unlink()


# --- What the round-change pass reads ---------------------------------------------

def round_context(conn: sqlite3.Connection, settings: game.Settings, story_title: str, online_ids: set[int]) -> str:
    from .narrator import format_minutes  # narrator imports game; keep this module importable on its own

    skip = longest_skip(conn)
    skips = pending_skips(conn)
    characters = [game.Character.from_row(r) for r in
                  conn.execute("SELECT * FROM characters WHERE alive = 1 ORDER BY name COLLATE NOCASE")]
    names = {c.id: c.name for c in characters}
    lines = [
        f"# Round change in {story_title}",
        f"Round {current_round(conn)} is ending. In-game time now: {game.world_time(conn, settings):%A %d %B %Y, %H:%M}",
        f"# World state (objective)\n{game.world_state(conn).strip() or '(not yet written)'}",
        "# Working memory\n" + ("\n".join(f"- {m}" for m in game.working_memory(conn)) or "(empty)"),
    ]
    if skip:
        lines.append(
            f"# Time skip\nThe clock jumps ahead {format_minutes(skip)} as this round closes. Pending skips:\n"
            + "\n".join(f"- {names.get(s.character_id, '?')}: {format_minutes(s.minutes)} for: {s.action}"
                        for s in skips)
        )
        if settings.offline_during_skips == "ai_plays":
            lines.append("Characters marked offline are played by you through the skip: decide plausibly what "
                         "they did, within reason and in character, and tell them in their notice.")
        else:
            lines.append("Characters marked offline are left untouched through the skip; leave them out of "
                         "the character updates. They get their own backstory when their player returns.")
    lines.append("# Living characters")
    for c in characters:
        status = "online" if c.id in online_ids else "offline"
        if any(s.character_id == c.id for s in skips):
            status += ", in the middle of a skip"
        lines.append(f"- {c.name} ({status}) | location: {c.location or 'unspecified'} | condition: {c.condition}"
                     f" | inventory: {', '.join(c.inventory) or 'nothing'}")
    events = round_events(conn)
    lines.append("# This round's events, in order\n"
                 + ("\n".join(f"[{name} {kind}] {text}" for name, kind, text in events) or "(nothing happened)"))
    return "\n\n".join(lines)

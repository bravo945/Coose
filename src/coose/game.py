"""Live game data for one story: characters, chat, the world clock and the
AI's working memory. Everything here lives in the story's own database
(.coose/story.db), so pausing, switching and (later) rolling back a story
never touch the author's files.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta

SCHEMA = """
CREATE TABLE IF NOT EXISTS characters (
    id            INTEGER PRIMARY KEY,
    name          TEXT NOT NULL UNIQUE COLLATE NOCASE,
    description   TEXT NOT NULL,
    location      TEXT NOT NULL DEFAULT '',
    condition     TEXT NOT NULL DEFAULT 'Healthy, no injuries',
    inventory     TEXT NOT NULL DEFAULT '[]',   -- JSON list of item names
    knowledge     TEXT NOT NULL DEFAULT '[]',   -- JSON list of things the character knows
    needs         TEXT NOT NULL DEFAULT '{}',   -- JSON {need: minutes since last met}; hidden
    alive         INTEGER NOT NULL DEFAULT 1,
    visibility    TEXT NOT NULL DEFAULT 'public' CHECK (visibility IN ('public', 'locked')),
    owner         TEXT,                          -- username, or NULL for AI-created characters
    shared_with   TEXT NOT NULL DEFAULT '[]',   -- JSON list of usernames (locked characters)
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    collective    INTEGER NOT NULL DEFAULT 0,    -- a group played as one character
    pending       INTEGER NOT NULL DEFAULT 0,    -- waiting for a story admin's approval
    parts         TEXT NOT NULL DEFAULT '[]'     -- JSON list of {name, location, members} for a collective
);

CREATE TABLE IF NOT EXISTS messages (
    id            INTEGER PRIMARY KEY,
    character_id  INTEGER NOT NULL REFERENCES characters(id),
    kind          TEXT NOT NULL,   -- action | question | dialogue | narration | answer | system
    username      TEXT,            -- who sent it, for player prompts
    text          TEXT NOT NULL,
    world_minutes INTEGER NOT NULL,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS messages_by_character ON messages (character_id, id);

-- Which character each logged-in player is playing right now.
CREATE TABLE IF NOT EXISTS players (
    username      TEXT PRIMARY KEY COLLATE NOCASE,
    character_id  INTEGER NOT NULL REFERENCES characters(id),
    since         TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS world (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""

MAX_NAME_LENGTH = 60
MAX_DESCRIPTION_WORDS = 300
MAX_STARTING_ITEMS = 4
PLAYER_KINDS = ("action", "question", "dialogue")
ABSENT_MODES = ("speak_only", "act_within_reason", "freeze")
SKIP_MODES = ("ai_plays", "backfill_on_return")


class GameError(ValueError):
    pass


# Columns added after a story's database may already exist.
LATER_COLUMNS = {
    "characters": [
        ("collective", "INTEGER NOT NULL DEFAULT 0"),
        ("pending", "INTEGER NOT NULL DEFAULT 0"),
        ("parts", "TEXT NOT NULL DEFAULT '[]'"),
    ],
}


def migrate(conn: sqlite3.Connection) -> None:
    """Bring an older story database (or a restored snapshot) up to the current columns."""
    for table, columns in LATER_COLUMNS.items():
        have = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        for name, definition in columns:
            if name not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
    conn.commit()


# --- Settings ---------------------------------------------------------------

@dataclass(frozen=True)
class Settings:
    """The parts of a story's Settings.yaml that the game reads."""

    start_time: datetime
    players_can_create: bool
    players_per_character: int
    tracked_needs: dict[str, float]  # need -> hours until it runs out
    collectives: bool = True
    ai_characters_public: bool = True
    absent_characters: str = "speak_only"
    actions_per_round: int = 5
    round_vote: bool = True
    skip_threshold_minutes: int = 10
    offline_during_skips: str = "ai_plays"

    @classmethod
    def from_dict(cls, raw: dict) -> "Settings":
        start = raw.get("start_time") or "2035-01-01 08:00"
        try:
            start_time = datetime.fromisoformat(str(start))
        except ValueError:
            raise GameError(f"start_time '{start}' isn't a date like 2035-01-01 08:00") from None
        needs = raw.get("tracked_needs") or {}
        if not isinstance(needs, dict):
            raise GameError("tracked_needs must be a set of 'need: hours' lines")
        return cls(
            start_time=start_time,
            players_can_create=bool(raw.get("players_can_create", True)),
            players_per_character=max(1, int(raw.get("players_per_character", 3))),
            tracked_needs={str(k): float(v) for k, v in needs.items()},
            ai_characters_public=bool(raw.get("ai_characters_public", True)),
            collectives=bool(raw.get("collectives", True)),
            absent_characters=_one_of("absent_characters", raw.get("absent_characters", "speak_only"),
                                      ABSENT_MODES),
            actions_per_round=max(1, int(raw.get("actions_per_round", 5))),
            round_vote=bool(raw.get("round_vote", True)),
            skip_threshold_minutes=max(0, int(raw.get("skip_threshold_minutes", 10))),
            offline_during_skips=_one_of("offline_during_skips", raw.get("offline_during_skips", "ai_plays"),
                                         SKIP_MODES),
        )


def _one_of(name: str, value, allowed: tuple[str, ...]) -> str:
    value = str(value)
    if value not in allowed:
        raise GameError(f"{name} must be one of: {', '.join(allowed)}")
    return value


# --- World values -----------------------------------------------------------

def _get(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM world WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def _set(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO world (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def init_world(conn: sqlite3.Connection, world_state_text: str) -> bool:
    """Seed world values the first time a story runs. Safe to call every time.
    Returns True when the world was new."""
    with conn:
        fresh = conn.execute("INSERT OR IGNORE INTO world VALUES ('clock_minutes', '0')").rowcount == 1
        conn.execute("INSERT OR IGNORE INTO world VALUES ('working_memory', '[]')")
        conn.execute("INSERT OR IGNORE INTO world VALUES ('world_state', ?)", (world_state_text,))
    return fresh


def clock_minutes(conn: sqlite3.Connection) -> int:
    return int(_get(conn, "clock_minutes", "0"))


def world_time(conn: sqlite3.Connection, settings: Settings) -> datetime:
    return settings.start_time + timedelta(minutes=clock_minutes(conn))


def total_actions(conn: sqlite3.Connection) -> int:
    """Player Actions over the story's whole life, for caps."""
    return int(_get(conn, "total_actions", "0"))


def set_clock(conn: sqlite3.Connection, minutes: int) -> None:
    with _write_lock:
        with conn:
            _set(conn, "clock_minutes", str(max(0, int(minutes))))


def set_world_state(conn: sqlite3.Connection, text: str) -> None:
    with conn:
        _set(conn, "world_state", text)


def removed_players(conn: sqlite3.Connection) -> list[str]:
    """Accounts a story admin has removed from this story."""
    return json.loads(_get(conn, "removed_players", "[]"))


def set_removed(conn: sqlite3.Connection, username: str, removed: bool) -> None:
    names = [n for n in removed_players(conn) if n.lower() != username.lower()]
    if removed:
        names.append(username)
    with conn:
        _set(conn, "removed_players", json.dumps(names))
    if removed:
        leave(conn, username)


def world_state(conn: sqlite3.Connection) -> str:
    return _get(conn, "world_state", "") or ""


def working_memory(conn: sqlite3.Connection) -> list[str]:
    return json.loads(_get(conn, "working_memory", "[]"))


# --- Characters -------------------------------------------------------------

@dataclass
class Character:
    id: int
    name: str
    description: str
    location: str
    condition: str
    inventory: list[str]
    knowledge: list[str]
    needs: dict[str, int]  # need -> world clock minute it was last met; hidden
    alive: bool
    visibility: str
    owner: str | None
    shared_with: list[str] = field(default_factory=list)
    collective: bool = False
    pending: bool = False
    parts: list[dict] = field(default_factory=list)

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Character":
        return cls(
            id=row["id"],
            name=row["name"],
            description=row["description"],
            location=row["location"],
            condition=row["condition"],
            inventory=json.loads(row["inventory"]),
            knowledge=json.loads(row["knowledge"]),
            needs=json.loads(row["needs"]),
            alive=bool(row["alive"]),
            visibility=row["visibility"],
            owner=row["owner"],
            shared_with=json.loads(row["shared_with"]),
            collective=bool(row["collective"]),
            pending=bool(row["pending"]),
            parts=json.loads(row["parts"]),
        )

    def can_be_played_by(self, username: str) -> bool:
        if not self.alive or self.pending:
            return False
        if self.visibility == "public":
            return True
        u = username.lower()
        return (self.owner or "").lower() == u or u in (s.lower() for s in self.shared_with)


def get_character(conn: sqlite3.Connection, character_id: int) -> Character | None:
    row = conn.execute("SELECT * FROM characters WHERE id = ?", (character_id,)).fetchone()
    return Character.from_row(row) if row else None


def create_character(
    conn: sqlite3.Connection,
    settings: Settings,
    *,
    name: str,
    description: str,
    location: str,
    items: list[str],
    visibility: str,
    owner: str | None,
    collective: bool = False,
) -> Character:
    name = " ".join(name.split())
    description = description.strip()
    items = [i.strip() for i in items if i.strip()]
    if not name:
        raise GameError("Give your character a name.")
    if len(name) > MAX_NAME_LENGTH:
        raise GameError(f"Names can be at most {MAX_NAME_LENGTH} characters.")
    if not description:
        raise GameError("Give your character a description.")
    if len(re.findall(r"\S+", description)) > MAX_DESCRIPTION_WORDS:
        raise GameError(f"Descriptions can be at most {MAX_DESCRIPTION_WORDS} words.")
    if len(items) > MAX_STARTING_ITEMS:
        raise GameError(f"Characters can start with at most {MAX_STARTING_ITEMS} items.")
    if visibility not in ("public", "locked"):
        raise GameError("Visibility must be public or locked.")
    if collective and not settings.collectives:
        raise GameError("This story doesn't allow collective characters.")
    needs = {n: clock_minutes(conn) for n in settings.tracked_needs}
    try:
        with conn:
            cur = conn.execute(
                "INSERT INTO characters (name, description, location, inventory, needs, visibility, owner, "
                "collective, pending) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (name, description, location.strip(), json.dumps(items), json.dumps(needs), visibility, owner,
                 int(collective), int(collective)),
            )
    except sqlite3.IntegrityError:
        raise GameError(f"A character named '{name}' already exists.") from None
    return get_character(conn, cur.lastrowid)


def create_ai_character(conn: sqlite3.Connection, settings: Settings, *, name: str, description: str,
                        location: str) -> Character | None:
    """A character the narrator introduced. Returns None if the name is taken."""
    try:
        return create_character(
            conn, settings, name=name, description=description[:2000], location=location, items=[],
            visibility="public" if settings.ai_characters_public else "locked", owner=None,
        )
    except GameError:
        return None


def find_character_by_name(conn: sqlite3.Connection, name: str) -> Character | None:
    row = conn.execute("SELECT * FROM characters WHERE name = ?", (name.strip(),)).fetchone()
    return Character.from_row(row) if row else None


def other_characters(conn: sqlite3.Connection, character_id: int) -> list[Character]:
    """Every living character except this one: the candidates for witnessing."""
    rows = conn.execute(
        "SELECT * FROM characters WHERE alive = 1 AND pending = 0 AND id != ? ORDER BY name COLLATE NOCASE",
        (character_id,)
    )
    return [Character.from_row(r) for r in rows]


def characters_here(conn: sqlite3.Connection, character: Character) -> list[Character]:
    """Living characters at the same location, who can be spoken to directly."""
    if not character.location.strip():
        return []
    return [c for c in other_characters(conn, character.id)
            if c.location.strip().lower() == character.location.strip().lower()]


def pending_collectives(conn: sqlite3.Connection) -> list[Character]:
    rows = conn.execute("SELECT * FROM characters WHERE pending = 1 ORDER BY id")
    return [Character.from_row(r) for r in rows]


def decide_collective(conn: sqlite3.Connection, name: str, approve: bool) -> Character | None:
    """Approve a requested collective into the story, or delete the request. None if there's no such request."""
    row = conn.execute("SELECT * FROM characters WHERE pending = 1 AND name = ?", (name.strip(),)).fetchone()
    if row is None:
        return None
    character = Character.from_row(row)
    with conn:
        if approve:
            conn.execute("UPDATE characters SET pending = 0 WHERE id = ?", (character.id,))
        else:
            conn.execute("DELETE FROM messages WHERE character_id = ?", (character.id,))
            conn.execute("DELETE FROM players WHERE character_id = ?", (character.id,))
            conn.execute("DELETE FROM characters WHERE id = ?", (character.id,))
    return character


def players_of(conn: sqlite3.Connection, character_id: int) -> list[str]:
    rows = conn.execute("SELECT username FROM players WHERE character_id = ?", (character_id,))
    return [r["username"] for r in rows]


def player_counts(conn: sqlite3.Connection) -> dict[int, int]:
    rows = conn.execute("SELECT character_id, COUNT(*) AS n FROM players GROUP BY character_id")
    return {r["character_id"]: r["n"] for r in rows}


def playable_characters(conn: sqlite3.Connection, username: str) -> list[Character]:
    rows = conn.execute("SELECT * FROM characters WHERE alive = 1 ORDER BY name COLLATE NOCASE")
    return [c for c in map(Character.from_row, rows) if c.can_be_played_by(username)]


def current_character(conn: sqlite3.Connection, username: str) -> Character | None:
    row = conn.execute("SELECT character_id FROM players WHERE username = ?", (username,)).fetchone()
    return get_character(conn, row["character_id"]) if row else None


def join(conn: sqlite3.Connection, settings: Settings, username: str, character_id: int) -> Character:
    """Start playing a character. A player plays one character at a time."""
    if username.lower() in {n.lower() for n in removed_players(conn)}:
        raise GameError("A story admin has removed you from this story.")
    character = get_character(conn, character_id)
    if character is None or not character.can_be_played_by(username):
        raise GameError("You can't play that character.")
    if current_character(conn, username) is not None:
        raise GameError("Leave your current character first.")
    if player_counts(conn).get(character_id, 0) >= settings.players_per_character:
        raise GameError(f"{character.name} already has {settings.players_per_character} players.")
    with conn:
        conn.execute("INSERT INTO players (username, character_id) VALUES (?, ?)", (username, character_id))
    return character


def leave(conn: sqlite3.Connection, username: str) -> None:
    with conn:
        conn.execute("DELETE FROM players WHERE username = ?", (username,))


# --- Chat -------------------------------------------------------------------

@dataclass(frozen=True)
class Message:
    id: int
    kind: str
    username: str | None
    text: str
    world_minutes: int


def add_message(conn: sqlite3.Connection, character_id: int, kind: str, text: str, username: str | None = None) -> None:
    with conn:
        conn.execute(
            "INSERT INTO messages (character_id, kind, username, text, world_minutes) VALUES (?, ?, ?, ?, ?)",
            (character_id, kind, username, text, clock_minutes(conn)),
        )


def recent_messages(conn: sqlite3.Connection, character_id: int, limit: int = 200) -> list[Message]:
    rows = conn.execute(
        "SELECT * FROM (SELECT * FROM messages WHERE character_id = ? ORDER BY id DESC LIMIT ?) ORDER BY id",
        (character_id, limit),
    )
    return [Message(r["id"], r["kind"], r["username"], r["text"], r["world_minutes"]) for r in rows]


# --- Needs ------------------------------------------------------------------

NEED_BANDS = ((0.5, "fine"), (0.75, "getting low"), (1.0, "urgent"))


def need_levels(conn: sqlite3.Connection, character: Character, settings: Settings) -> dict[str, str]:
    """Describe each hidden need in words. Players never see the numbers.
    Needs run down with the world clock, for every character alike."""
    now = clock_minutes(conn)
    levels = {}
    for need, hours in settings.tracked_needs.items():
        since = now - character.needs.get(need, 0)
        used = since / 60 / hours if hours > 0 else 0
        levels[need] = next((word for limit, word in NEED_BANDS if used < limit), "run out")
    return levels


# --- Applying the AI's result -------------------------------------------------

@dataclass
class ActionOutcome:
    """What the Author pass decided. Mirrors narrator.AuthorResult."""

    narration: str
    minutes_elapsed: int
    location: str = ""
    condition: str = ""
    inventory_add: list[str] = field(default_factory=list)
    inventory_remove: list[str] = field(default_factory=list)
    learned: list[str] = field(default_factory=list)
    needs_met: list[str] = field(default_factory=list)
    alive: bool = True
    memory_add: list[str] = field(default_factory=list)
    parts: list[dict] = field(default_factory=list)  # a collective's parts afterwards; empty if unchanged


# Read-modify-write of shared rows (clock, working memory, knowledge) happens
# under this lock, so two characters' results landing at once can't lose each
# other's changes. Coose runs as one process, so a thread lock is enough.
_write_lock = threading.Lock()


def apply_action(conn: sqlite3.Connection, character: Character, outcome: ActionOutcome, *,
                 advance_clock: bool = True, count: bool = True) -> Character:
    """Write an Action's consequences in one transaction: the character, the
    working memory, the clock (unless the time is held for a skip) and the
    round's action count (unless this isn't a player's Action)."""
    minutes = max(0, int(outcome.minutes_elapsed)) if advance_clock else 0
    with _write_lock:
        character = get_character(conn, character.id)  # pick up anything witnessed meanwhile
        inventory = list(character.inventory)
        for item in outcome.inventory_remove:
            match = next((i for i in inventory if i.lower() == item.lower()), None)
            if match:
                inventory.remove(match)
        inventory += [i for i in outcome.inventory_add if i.strip()]
        knowledge = character.knowledge + [k for k in outcome.learned if k.strip()]
        now = clock_minutes(conn) + minutes
        met = {n.lower() for n in outcome.needs_met}
        needs = {n: now if n.lower() in met else v for n, v in character.needs.items()}
        memory = working_memory(conn) + [m for m in outcome.memory_add if m.strip()]
        with conn:
            _set(conn, "clock_minutes", str(now))
            _set(conn, "working_memory", json.dumps(memory))
            if count:
                _set(conn, "round_actions", str(int(_get(conn, "round_actions", "0")) + 1))
                _set(conn, "total_actions", str(total_actions(conn) + 1))
            conn.execute(
                "UPDATE characters SET location = ?, condition = ?, inventory = ?, knowledge = ?, needs = ?, "
                "alive = ?, parts = ? WHERE id = ?",
                (
                    outcome.location.strip() or character.location,
                    outcome.condition.strip() or character.condition,
                    json.dumps(inventory),
                    json.dumps(knowledge),
                    json.dumps(needs),
                    int(bool(outcome.alive)),
                    json.dumps(clean_parts(outcome.parts) if character.collective and outcome.parts
                               else character.parts),
                    character.id,
                ),
            )
    return get_character(conn, character.id)


def clean_parts(parts: list) -> list[dict]:
    """A collective's parts as plain dicts, from the AI's models or dicts."""
    result = []
    for p in parts:
        p = p if isinstance(p, dict) else p.model_dump()
        name = str(p.get("name", "")).strip()
        if name:
            result.append({"name": name, "location": str(p.get("location", "")).strip(),
                           "members": str(p.get("members", "")).strip()})
    return result


def describe_parts(parts: list[dict]) -> str:
    return "; ".join(f"{p['name']} ({p.get('members') or '?'}) at {p.get('location') or '?'}" for p in parts)


def add_knowledge(conn: sqlite3.Connection, character_id: int, facts: list[str]) -> None:
    facts = [f for f in facts if f.strip()]
    if not facts:
        return
    with _write_lock:
        character = get_character(conn, character_id)
        if character is None:
            return
        with conn:
            conn.execute("UPDATE characters SET knowledge = ? WHERE id = ?",
                         (json.dumps(character.knowledge + facts), character_id))


def add_memory(conn: sqlite3.Connection, notes: list[str]) -> None:
    notes = [n for n in notes if n.strip()]
    if not notes:
        return
    with _write_lock:
        with conn:
            _set(conn, "working_memory", json.dumps(working_memory(conn) + notes))


# --- One Action at a time per character -------------------------------------

_locks: dict[tuple[str, int], threading.Lock] = {}
_locks_guard = threading.Lock()


def character_lock(story: str, character_id: int) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault((story, character_id), threading.Lock())

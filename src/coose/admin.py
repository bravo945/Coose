"""Story admin commands, typed in the admin chatroom.

Commands are out of character. The simple ones change the live data
directly; /intervene and /rewrite go to the AI, which works out every
objective consequence. /round and /rollback need the running server, so
they come back to the web app as a request rather than being done here.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from . import game, rounds

ADMIN_SCHEMA = """
CREATE TABLE IF NOT EXISTS admin_log (
    id         INTEGER PRIMARY KEY,
    kind       TEXT NOT NULL,   -- command | reply
    username   TEXT,
    text       TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

AI_ADDITIONS = Path("Premise") / "AI Additions"

HELP = """\
Commands (out of character; players never see them):
/round: force a round change now
/rollback <round>: restore the story to the start of that round
/time +2h | -30m | +1d: move the world clock
/give <character>: <item>   /take <character>: <item>
/kill <character>   /revive <character>
/edit character <character>: <condition|location|description> = <text>
/edit world <the complete new World State>
/remove player <account>   /add player <account>
/approve [file]   /reject <file>: AI-written lore in Premise/AI Additions (no file: list it)
/intervene <instruction>: the AI carries out any change you describe
/rewrite <how a past event really went>: the AI rewrites it and what followed
/help: this list"""


class CommandError(ValueError):
    pass


@dataclass
class CommandResult:
    reply: str = ""
    notify: list[int] = field(default_factory=list)  # characters whose chatroom changed
    notify_all: bool = False
    force_round: bool = False
    rollback_to: int | None = None
    ai: tuple[str, str] | None = None  # (command, text) for the AI to carry out


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(ADMIN_SCHEMA)


def log(conn: sqlite3.Connection, kind: str, text: str, username: str | None = None) -> None:
    with conn:
        conn.execute("INSERT INTO admin_log (kind, username, text) VALUES (?, ?, ?)", (kind, username, text))


def recent(conn: sqlite3.Connection, limit: int = 200) -> list[sqlite3.Row]:
    rows = conn.execute("SELECT * FROM admin_log ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return list(reversed(rows))


def _character(conn: sqlite3.Connection, name: str) -> game.Character:
    character = game.find_character_by_name(conn, name.strip())
    if character is None:
        raise CommandError(f"There's no character named '{name.strip()}'.")
    return character


def _split(rest: str, usage: str) -> tuple[str, str]:
    if ":" not in rest:
        raise CommandError(f"Usage: {usage}")
    name, value = rest.split(":", 1)
    if not name.strip() or not value.strip():
        raise CommandError(f"Usage: {usage}")
    return name.strip(), value.strip()


def _tell(conn: sqlite3.Connection, character: game.Character, text: str) -> None:
    game.add_message(conn, character.id, "system", text)


DURATION = re.compile(r"^([+-])\s*(\d+)\s*(m|min|minutes?|h|hours?|d|days?)$", re.IGNORECASE)


def parse_duration(text: str) -> int:
    m = DURATION.match(text.strip())
    if not m:
        raise CommandError("Usage: /time +2h, /time -30m or /time +1d")
    sign, number, unit = m.groups()
    minutes = int(number) * {"m": 1, "h": 60, "d": 24 * 60}[unit[0].lower()]
    return minutes if sign == "+" else -minutes


def run(conn: sqlite3.Connection, story_path: Path, text: str) -> CommandResult:
    """Carry out one command. Raises CommandError with a message for the admin."""
    text = text.strip()
    if not text.startswith("/"):
        raise CommandError("Commands start with /. Type /help for the list.")
    head, _, rest = text[1:].partition(" ")
    head, rest = head.lower(), rest.strip()
    if head == "edit":
        sub, _, rest = rest.partition(" ")
        head, rest = f"edit {sub.lower()}", rest.strip()
    elif head in ("add", "remove") and rest.lower().startswith("player"):
        head, rest = f"{head} player", rest[len("player"):].strip()

    if head == "help":
        return CommandResult(HELP)

    if head == "round":
        return CommandResult("Forcing a round change.", force_round=True)

    if head == "rollback":
        if not rest.isdigit():
            raise CommandError("Usage: /rollback <round>. Rounds you can go back to: "
                               + (", ".join(map(str, rounds.snapshots(story_path))) or "none"))
        return CommandResult(rollback_to=int(rest))

    if head == "time":
        delta = parse_duration(rest)
        now = game.clock_minutes(conn)
        if now + delta < 0:
            raise CommandError("That would move the clock before the story's start time.")
        game.set_clock(conn, now + delta)
        game.add_memory(conn, [f"A story admin moved the world clock by {delta:+d} minutes."])
        return CommandResult(f"World clock moved {rest}.", notify_all=True)

    if head in ("give", "take"):
        name, item = _split(rest, f"/{head} <character>: <item>")
        character = _character(conn, name)
        outcome = game.ActionOutcome(narration="", minutes_elapsed=0, alive=character.alive,
                                     inventory_add=[item] if head == "give" else [],
                                     inventory_remove=[item] if head == "take" else [])
        if head == "take" and item.lower() not in {i.lower() for i in character.inventory}:
            raise CommandError(f"{character.name} doesn't carry '{item}'. They carry: "
                               f"{', '.join(character.inventory) or 'nothing'}.")
        game.apply_action(conn, character, outcome, advance_clock=False, count=False)
        _tell(conn, character, f"Your inventory changed: {'+' if head == 'give' else '-'} {item}.")
        return CommandResult(f"{'Gave' if head == 'give' else 'Took'} {item} "
                             f"{'to' if head == 'give' else 'from'} {character.name}.", notify=[character.id])

    if head in ("kill", "revive"):
        if not rest:
            raise CommandError(f"Usage: /{head} <character>")
        character = _character(conn, rest)
        alive = head == "revive"
        if character.alive == alive:
            raise CommandError(f"{character.name} is already {'alive' if alive else 'dead'}.")
        game.apply_action(conn, character, game.ActionOutcome(narration="", minutes_elapsed=0, alive=alive),
                          advance_clock=False, count=False)
        game.add_memory(conn, [f"A story admin made {character.name} {'alive again' if alive else 'dead'}."])
        _tell(conn, character, f"{character.name} is {'alive again' if alive else 'dead'}.")
        return CommandResult(f"{character.name} is now {'alive' if alive else 'dead'}.", notify=[character.id])

    if head == "edit character":
        name, change = _split(rest, "/edit character <character>: <condition|location|description> = <text>")
        if "=" not in change:
            raise CommandError("Usage: /edit character <character>: <condition|location|description> = <text>")
        what, value = (s.strip() for s in change.split("=", 1))
        what = what.lower()
        if what not in ("condition", "location", "description") or not value:
            raise CommandError("You can edit a character's condition, location or description.")
        character = _character(conn, name)
        with conn:
            conn.execute(f"UPDATE characters SET {what} = ? WHERE id = ?", (value, character.id))
        _tell(conn, character, f"Your character sheet changed: {what} is now \"{value}\".")
        return CommandResult(f"{character.name}'s {what} is now: {value}", notify=[character.id])

    if head == "edit world":
        if not rest:
            raise CommandError("Usage: /edit world <the complete new World State>")
        game.set_world_state(conn, rest)
        return CommandResult("World State replaced.")

    if head in ("remove player", "add player"):
        if not rest:
            raise CommandError(f"Usage: /{head} <account>")
        removing = head == "remove player"
        was_playing = game.current_character(conn, rest)
        if not removing and rest.lower() not in {n.lower() for n in game.removed_players(conn)}:
            raise CommandError(f"'{rest}' hasn't been removed from this story.")
        game.set_removed(conn, rest, removing)
        return CommandResult(f"'{rest}' {'can no longer' if removing else 'can again'} play in this story.",
                             notify=[was_playing.id] if was_playing else [])

    if head in ("approve", "reject"):
        return _lore(story_path, head, rest)

    if head in ("intervene", "rewrite"):
        if not rest:
            raise CommandError(f"Usage: /{head} <what should change>")
        return CommandResult(ai=(head, rest))

    raise CommandError(f"Unknown command '/{head}'. Type /help for the list.")


def pending_lore(story_path: Path) -> list[str]:
    folder = story_path / AI_ADDITIONS
    if not folder.is_dir():
        return []
    return sorted(p.name for p in folder.glob("*.md") if p.name.lower() != "readme.md")


def _lore(story_path: Path, head: str, name: str) -> CommandResult:
    pending = pending_lore(story_path)
    if not name:
        if head == "reject":
            raise CommandError("Usage: /reject <file>")
        return CommandResult("AI-written lore waiting for review: " + (", ".join(pending) or "none") + ".")
    match = next((p for p in pending if p.lower() in (name.lower(), f"{name.lower()}.md")), None)
    if match is None:
        raise CommandError(f"No AI-written lore named '{name}'. Waiting: {', '.join(pending) or 'none'}.")
    source = story_path / AI_ADDITIONS / match
    if head == "reject":
        source.unlink()
        return CommandResult(f"Deleted {match}.")
    dest = story_path / "Premise" / match
    if dest.exists():
        raise CommandError(f"Premise/{match} already exists. Rename one of them first.")
    source.replace(dest)
    return CommandResult(f"Moved {match} into the Premise.")


def apply_intervention(conn: sqlite3.Connection, result) -> list[int]:
    """Write what the AI decided for /intervene or /rewrite. Returns the characters told about it."""
    if result.world_state.strip():
        game.set_world_state(conn, result.world_state.strip())
    game.add_memory(conn, result.memory_add)
    told = []
    for change in result.characters:
        character = game.find_character_by_name(conn, change.name)
        if character is None:
            continue
        game.apply_action(conn, character, game.ActionOutcome(
            narration="", minutes_elapsed=0, location=change.location, condition=change.condition,
            inventory_add=change.inventory_add, inventory_remove=change.inventory_remove, learned=change.learned,
            alive=change.alive,
        ), advance_clock=False, count=False)
        if change.notice.strip():
            game.add_message(conn, character.id, "narration", change.notice.strip())
            told.append(character.id)
    return told

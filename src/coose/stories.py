"""Story instances: discovering, checking and running story folders.

A story is a folder built from story-template/. The server runs one story at
a time. Switching or pausing never touches a story's files: its live data
stays in its own database (.coose/story.db) and resumes where it left off.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from . import db, game

REQUIRED_DIRS = ("Premise", "Rulebook", "Characters")
SETTINGS_PATH = Path("Rulebook") / "Settings.yaml"
STORY_DB_PATH = Path(".coose") / "story.db"

STATE_RUNNING = "running_story"
STATE_PAUSED = "paused"

STORY_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
INSERT OR IGNORE INTO meta (key, value) VALUES ('created_at', datetime('now'));
"""


class StoryError(ValueError):
    pass


@dataclass
class Story:
    name: str
    path: Path
    settings: dict = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)

    @property
    def valid(self) -> bool:
        return not self.problems

    def game_settings(self) -> "game.Settings":
        return game.Settings.from_dict(self.settings)

    @property
    def display_name(self) -> str:
        return self.settings.get("display_name") or self.name


def load_story(path: Path) -> Story:
    """Read a story folder and report anything that would stop it running."""
    story = Story(name=path.name, path=path)
    for d in REQUIRED_DIRS:
        if not (path / d).is_dir():
            story.problems.append(f"missing the {d}/ folder")
    settings_file = path / SETTINGS_PATH
    if settings_file.exists():
        try:
            loaded = yaml.safe_load(settings_file.read_text(encoding="utf-8"))
        except yaml.YAMLError as e:
            story.problems.append(f"{SETTINGS_PATH} is not valid YAML: {e}")
        else:
            if loaded is None:
                loaded = {}
            if isinstance(loaded, dict):
                story.settings = loaded
                try:
                    game.Settings.from_dict(loaded)
                except (game.GameError, TypeError, ValueError) as e:
                    story.problems.append(f"{SETTINGS_PATH}: {e}")
            else:
                story.problems.append(f"{SETTINGS_PATH} must be a set of 'key: value' lines")
    return story


def list_stories(stories_dir: Path) -> list[Story]:
    if not stories_dir.is_dir():
        return []
    return [
        load_story(p)
        for p in sorted(stories_dir.iterdir(), key=lambda p: p.name.lower())
        if p.is_dir() and not p.name.startswith(".")
    ]


def find_story(stories_dir: Path, name: str) -> Story:
    path = stories_dir / name
    if not name or "/" in name or "\\" in name or name.startswith(".") or not path.is_dir():
        raise StoryError(f"No story folder named '{name}' in {stories_dir}.")
    return load_story(path)


def open_story_db(story: Story) -> sqlite3.Connection:
    """Open (and on first run create) the story's own live-data database."""
    path = story.path / STORY_DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(STORY_SCHEMA)
    conn.executescript(game.SCHEMA)
    world_state_file = story.path / "WorldState.md"
    game.init_world(
        conn, world_state_file.read_text(encoding="utf-8") if world_state_file.exists() else ""
    )
    return conn


# --- Which story is running -------------------------------------------------

@dataclass(frozen=True)
class ServerStatus:
    story: str | None
    paused: bool

    @property
    def online(self) -> bool:
        return self.story is not None and not self.paused


def status(conn: sqlite3.Connection) -> ServerStatus:
    return ServerStatus(
        story=db.get_state(conn, STATE_RUNNING),
        paused=db.get_state(conn, STATE_PAUSED) == "1",
    )


def run_story(conn: sqlite3.Connection, stories_dir: Path, name: str) -> Story:
    """Make a story the running one. Any other story is simply set aside."""
    story = find_story(stories_dir, name)
    if not story.valid:
        raise StoryError(f"Story '{name}' can't run: " + "; ".join(story.problems))
    open_story_db(story).close()
    db.set_state(conn, STATE_RUNNING, story.name)
    db.set_state(conn, STATE_PAUSED, "0")
    return story


def pause(conn: sqlite3.Connection) -> None:
    if status(conn).story is None:
        raise StoryError("No story is running.")
    db.set_state(conn, STATE_PAUSED, "1")


def resume(conn: sqlite3.Connection) -> None:
    if status(conn).story is None:
        raise StoryError("No story is running.")
    db.set_state(conn, STATE_PAUSED, "0")

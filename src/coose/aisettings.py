"""Which Claude model runs each kind of AI task, and how hard it thinks.

The owner sets these per story, from the panel, its console or the terminal
(`coose ai ...`). Anything not set for a story falls back to coose.toml's
[ai] section. They live in the server database, not the story folder,
because they decide what the owner pays.

Tasks:
  action     the Responder and Author passes of an Action
  witnesses  who noticed an Action and what each of them perceived
  question   answering a player's out-of-character Question
  round      the round change over the whole world
  backfill   the backstory for a character who sat out a skip
  admin      /intervene and /rewrite

Each model keeps its own cache of the Premise, so every extra model in use
pays its own cache write (about once an hour). Cheap models pay very little
for that; it matters most when mixing two expensive ones.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, replace

from . import pricing

TASKS = ("action", "witnesses", "question", "round", "backfill", "admin")
EFFORTS = ("low", "medium", "high", "xhigh", "max")
THINKING = ("on", "off")
MODELS = tuple(pricing.PRICES)

# Models whose thinking can't be switched off; effort is their only control.
ALWAYS_THINKING = ("claude-opus-5-5", "claude-fable-5-2", "claude-fable-5-1", "claude-fable-5")

TASK_HELP = {
    "action": "Responder and Author passes of every Action",
    "witnesses": "who noticed an Action, and what they saw",
    "question": "out-of-character Questions",
    "round": "round changes",
    "backfill": "backstories after a skip a player sat out",
    "admin": "/intervene and /rewrite",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS ai_settings (
    story TEXT NOT NULL,
    key   TEXT NOT NULL,   -- <task>.model | <task>.effort | <task>.thinking | action_passes
    value TEXT NOT NULL,
    PRIMARY KEY (story, key)
);
"""


class SettingError(ValueError):
    pass


@dataclass(frozen=True)
class TaskSettings:
    model: str
    effort: str = "medium"
    thinking: str = "on"

    def describe(self) -> str:
        return f"{self.model}, {self.effort} effort" + (", thinking off" if self.thinking == "off" else "")


@dataclass(frozen=True)
class Profile:
    """Everything the narrator needs to know to pick a model for a call."""

    tasks: dict[str, TaskSettings]
    action_passes: int = 2  # 2: Responder then Author. 1: one combined pass, cheaper.

    def for_task(self, task: str) -> TaskSettings:
        return self.tasks.get(task) or self.tasks["action"]


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def defaults(model: str, effort: str, task_overrides: dict | None = None) -> Profile:
    """The server-wide defaults from coose.toml."""
    tasks = {}
    for task in TASKS:
        raw = (task_overrides or {}).get(task) or {}
        tasks[task] = check(TaskSettings(
            model=str(raw.get("model", model)),
            effort=str(raw.get("effort", effort)),
            thinking=str(raw.get("thinking", "on")),
        ), task)
    return Profile(tasks)


def check(s: TaskSettings, task: str = "") -> TaskSettings:
    where = f" for {task}" if task else ""
    if s.effort not in EFFORTS:
        raise SettingError(f"Effort{where} must be one of: {', '.join(EFFORTS)}.")
    if s.thinking not in THINKING:
        raise SettingError(f"Thinking{where} must be on or off.")
    if s.thinking == "off":
        if s.model in ALWAYS_THINKING:
            raise SettingError(f"{s.model} always thinks; lower its effort instead of turning thinking off.")
        if s.effort in ("xhigh", "max"):
            raise SettingError("Thinking can only be off at high effort or below.")
    return s


def profile(conn: sqlite3.Connection, story: str | None, base: Profile) -> Profile:
    """The settings for one story: its own choices over the server defaults."""
    if not story:
        return base
    ensure_schema(conn)
    stored = {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM ai_settings WHERE story = ?",
                                                          (story,))}
    tasks = {}
    for task in TASKS:
        s = base.tasks[task]
        s = replace(s, model=stored.get(f"{task}.model", s.model), effort=stored.get(f"{task}.effort", s.effort),
                    thinking=stored.get(f"{task}.thinking", s.thinking))
        try:
            tasks[task] = check(s, task)
        except SettingError:
            tasks[task] = base.tasks[task]  # a stored mix that no longer fits; use the default
    passes = stored.get("action_passes")
    return Profile(tasks, int(passes) if passes in ("1", "2") else base.action_passes)


def set_task(conn: sqlite3.Connection, story: str, task: str, base: Profile, *, model: str | None = None,
             effort: str | None = None, thinking: str | None = None) -> TaskSettings:
    """Change one task's settings for a story. Returns the result."""
    tasks = TASKS if task == "all" else (task,)
    if task != "all" and task not in TASKS:
        raise SettingError(f"Unknown task '{task}'. Tasks: {', '.join(TASKS)}, or all.")
    if model is not None and model not in MODELS:
        raise SettingError(f"Unknown model '{model}'. Models: {', '.join(MODELS)}.")
    current = profile(conn, story, base)
    result = None
    for t in tasks:
        s = current.tasks[t]
        result = check(replace(s, model=model or s.model, effort=effort or s.effort,
                               thinking=thinking or s.thinking), t)
    with conn:
        for t in tasks:
            for key, value in (("model", model), ("effort", effort), ("thinking", thinking)):
                if value is not None:
                    _put(conn, story, f"{t}.{key}", value)
    return result


def set_action_passes(conn: sqlite3.Connection, story: str, passes: int) -> None:
    if passes not in (1, 2):
        raise SettingError("Action passes must be 1 or 2.")
    ensure_schema(conn)
    with conn:
        _put(conn, story, "action_passes", str(passes))


def reset(conn: sqlite3.Connection, story: str, task: str | None = None) -> None:
    """Back to the server defaults, for one task or the whole story."""
    ensure_schema(conn)
    with conn:
        if task in (None, "all"):
            conn.execute("DELETE FROM ai_settings WHERE story = ?", (story,))
        elif task in TASKS:
            conn.execute("DELETE FROM ai_settings WHERE story = ? AND key LIKE ?", (story, f"{task}.%"))
        else:
            raise SettingError(f"Unknown task '{task}'. Tasks: {', '.join(TASKS)}, or all.")


def _put(conn: sqlite3.Connection, story: str, key: str, value: str) -> None:
    ensure_schema(conn)
    conn.execute("INSERT INTO ai_settings (story, key, value) VALUES (?, ?, ?) "
                 "ON CONFLICT(story, key) DO UPDATE SET value = excluded.value", (story, key, value))


def describe(p: Profile) -> list[str]:
    lines = [f"{t:<10} {p.tasks[t].describe()}  ({TASK_HELP[t]})" for t in TASKS]
    lines.append(f"Actions use {p.action_passes} pass{'es' if p.action_passes == 2 else ''} "
                 f"({'Responder then Author' if p.action_passes == 2 else 'one combined pass'}).")
    return lines


def thinking_param(s: TaskSettings) -> dict | None:
    """The request's `thinking` value, or None to leave it out (adaptive)."""
    if s.thinking != "off":
        return None
    if s.model == "claude-sonnet-5-5":
        return {"type": "between_tools"}
    return {"type": "disabled"}

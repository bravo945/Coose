"""The owner's controls: story admins, caps and schedules (opening hours).

These are server-level values. They live in the server database, never in a
story's files, so story admins can't change them. (Accounts the author lists
under `admins` in Settings.yaml are story admins too.)
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path

from . import accounts, db, game, rounds, stories


class ControlError(ValueError):
    pass


# --- Story admins -------------------------------------------------------------

def add_admin(conn: sqlite3.Connection, username: str, story: str) -> None:
    if not accounts.account_exists(conn, username):
        raise ControlError(f"There's no account named '{username}'.")
    with conn:
        conn.execute("INSERT OR IGNORE INTO story_admins (username, story) VALUES (?, ?)", (username, story))


def remove_admin(conn: sqlite3.Connection, username: str, story: str) -> None:
    with conn:
        removed = conn.execute("DELETE FROM story_admins WHERE username = ? AND story = ?",
                               (username, story)).rowcount
    if not removed:
        raise ControlError(f"'{username}' isn't an admin of '{story}' (admins listed in its Settings.yaml "
                           "can only be removed there).")


def admins(conn: sqlite3.Connection, story: stories.Story) -> list[str]:
    """Everyone who can use commands in this story: appointed by the owner or listed by the author."""
    appointed = [r["username"] for r in conn.execute(
        "SELECT username FROM story_admins WHERE story = ? ORDER BY username COLLATE NOCASE", (story.name,))]
    listed = [str(a) for a in (story.settings.get("admins") or []) if str(a).strip()]
    seen, result = set(), []
    for name in appointed + listed:
        if name.lower() not in seen:
            seen.add(name.lower())
            result.append(name)
    return result


def is_admin(conn: sqlite3.Connection, story: stories.Story, username: str | None) -> bool:
    return bool(username) and username.lower() in {a.lower() for a in admins(conn, story)}


# --- Caps -----------------------------------------------------------------------

@dataclass(frozen=True)
class Cap:
    """Pause the story once its totals reach these numbers. None means no limit."""

    actions: int | None = None  # total Actions in the story
    rounds: int | None = None   # the round number that, once reached, pauses the story


def _cap_key(story: str) -> str:
    return f"cap:{story}"


def get_cap(conn: sqlite3.Connection, story: str) -> Cap | None:
    raw = db.get_state(conn, _cap_key(story))
    if not raw:
        return None
    data = json.loads(raw)
    return Cap(actions=data.get("actions"), rounds=data.get("rounds"))


def set_cap(conn: sqlite3.Connection, story: stories.Story, more_actions: int | None,
            more_rounds: int | None) -> Cap:
    """Allow this many more Actions and/or rounds from now, then pause."""
    if more_actions is None and more_rounds is None:
        raise ControlError("Give a number of actions, rounds, or both.")
    if any(n is not None and n < 0 for n in (more_actions, more_rounds)):
        raise ControlError("Caps can't be negative.")
    sconn = stories.open_story_db(story)
    try:
        cap = Cap(
            actions=None if more_actions is None else game.total_actions(sconn) + more_actions,
            rounds=None if more_rounds is None else rounds.current_round(sconn) + more_rounds,
        )
    finally:
        sconn.close()
    db.set_state(conn, _cap_key(story.name), json.dumps({"actions": cap.actions, "rounds": cap.rounds}))
    return cap


def clear_cap(conn: sqlite3.Connection, story: str) -> None:
    db.set_state(conn, _cap_key(story), None)


def cap_reached(conn: sqlite3.Connection, story_name: str, sconn: sqlite3.Connection) -> bool:
    cap = get_cap(conn, story_name)
    if cap is None:
        return False
    return ((cap.actions is not None and game.total_actions(sconn) >= cap.actions)
            or (cap.rounds is not None and rounds.current_round(sconn) >= cap.rounds))


def describe_cap(conn: sqlite3.Connection, story: stories.Story) -> str:
    cap = get_cap(conn, story.name)
    if cap is None:
        return "no cap"
    sconn = stories.open_story_db(story)
    try:
        parts = []
        if cap.actions is not None:
            left = max(0, cap.actions - game.total_actions(sconn))
            parts.append(f"{left} more action{'s' if left != 1 else ''}")
        if cap.rounds is not None:
            parts.append(f"pauses when round {cap.rounds} begins (now round {rounds.current_round(sconn)})")
        reached = cap_reached(conn, story.name, sconn)
    finally:
        sconn.close()
    return ("cap reached" if reached else "cap: ") + ("" if reached else ", ".join(parts))


def story_cap_reached(conn: sqlite3.Connection, story: stories.Story) -> bool:
    if get_cap(conn, story.name) is None:
        return False
    sconn = stories.open_story_db(story)
    try:
        return cap_reached(conn, story.name, sconn)
    finally:
        sconn.close()


# --- Schedules ---------------------------------------------------------------

DAY_NAMES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
DAY_GROUPS = {"daily": range(7), "weekdays": range(5), "weekends": (5, 6)}


@dataclass(frozen=True)
class Span:
    """Opening hours on some days of the week. An end at or before the start runs past midnight."""

    days: tuple[int, ...]  # 0 = Monday
    start: time
    end: time

    def describe(self) -> str:
        days = next((g for g, r in DAY_GROUPS.items() if tuple(r) == self.days), None)
        days = days or ",".join(DAY_NAMES[d] for d in self.days)
        return f"{days} {self.start:%H:%M}-{self.end:%H:%M}"

    def occurrences(self, around_from: date, around_to: date):
        """(start, end) datetimes for every opening that starts between the two dates (inclusive)."""
        d = around_from
        while d <= around_to:
            if d.weekday() in self.days:
                s = datetime.combine(d, self.start)
                e = datetime.combine(d, self.end)
                if e <= s:
                    e += timedelta(days=1)
                yield s, e
            d += timedelta(days=1)


def parse_days(text: str) -> tuple[int, ...]:
    text = text.strip().lower()
    if text in DAY_GROUPS:
        return tuple(DAY_GROUPS[text])
    days = set()
    for part in re.split(r"[,\s]+", text):
        if not part:
            continue
        if part[:3] not in DAY_NAMES:
            raise ControlError(f"'{part}' isn't a day. Use mon..sun, daily, weekdays or weekends.")
        days.add(DAY_NAMES.index(part[:3]))
    if not days:
        raise ControlError("Name at least one day.")
    return tuple(sorted(days))


def parse_time(text: str) -> time:
    try:
        return datetime.strptime(text.strip(), "%H:%M").time()
    except ValueError:
        raise ControlError(f"'{text}' isn't a time like 19:00.") from None


def _schedule_key(story: str) -> str:
    return f"schedule:{story}"


def get_schedule(conn: sqlite3.Connection, story: str) -> list[Span]:
    raw = db.get_state(conn, _schedule_key(story))
    return [Span(tuple(s["days"]), parse_time(s["start"]), parse_time(s["end"])) for s in json.loads(raw or "[]")]


def _save_schedule(conn: sqlite3.Connection, story: str, spans: list[Span]) -> None:
    db.set_state(conn, _schedule_key(story), json.dumps(
        [{"days": list(s.days), "start": f"{s.start:%H:%M}", "end": f"{s.end:%H:%M}"} for s in spans]) if spans
        else None)


def add_span(conn: sqlite3.Connection, story: str, days: str, start: str, end: str) -> Span:
    span = Span(parse_days(days), parse_time(start), parse_time(end))
    if span.start == span.end:
        raise ControlError("The opening has to last more than no time at all.")
    _save_schedule(conn, story, get_schedule(conn, story) + [span])
    return span


def clear_schedule(conn: sqlite3.Connection, story: str) -> None:
    _save_schedule(conn, story, [])


def all_schedules(conn: sqlite3.Connection) -> dict[str, list[Span]]:
    names = [r["key"].removeprefix("schedule:") for r in conn.execute(
        "SELECT key FROM server_state WHERE key LIKE 'schedule:%' AND value IS NOT NULL ORDER BY key")]
    return {n: get_schedule(conn, n) for n in names if get_schedule(conn, n)}


def _active_at(schedules: dict[str, list[Span]], at: datetime, exclude: str | None = None) -> str | None:
    """The story whose opening covers `at` and started most recently (ties: later name)."""
    best = None
    for story, spans in schedules.items():
        if story == exclude:
            continue
        for span in spans:
            for s, e in span.occurrences(at.date() - timedelta(days=1), at.date()):
                if s <= at < e and (best is None or (s, story) > best):
                    best = (s, story)
    return best[1] if best else None


def schedule_tick(conn: sqlite3.Connection, stories_dir: Path, after: datetime, until: datetime) -> list[str]:
    """Apply every opening and closing in (after, until], in order. Returns what happened, for the log.

    Schedules only act at the edges of their openings, so a manual pause or
    resume in between is left alone. When openings overlap, the one that
    started later wins; on an exact tie, the story whose name sorts later.
    """
    schedules = all_schedules(conn)
    events = []
    for story, spans in schedules.items():
        for span in spans:
            for s, e in span.occurrences(after.date() - timedelta(days=1), until.date()):
                if after < s <= until:
                    events.append((s, 1, story))
                if after < e <= until:
                    events.append((e, 0, story))
    log = []
    for at, kind, story in sorted(events):  # closings before openings at the same minute; names in order
        if kind == 1:
            log += _open(conn, stories_dir, story)
            continue
        st = stories.status(conn)
        if st.story == story and not st.paused:
            stories.pause(conn)
            log.append(f"{story}: closed by its schedule")
            follow = _active_at(schedules, at, exclude=story)
            if follow:
                log += _open(conn, stories_dir, follow)
    return log


def _open(conn: sqlite3.Connection, stories_dir: Path, name: str) -> list[str]:
    try:
        story = stories.find_story(stories_dir, name)
        if story_cap_reached(conn, story):
            return [f"{name}: not opened, its cap is reached"]
        stories.run_story(conn, stories_dir, name)
    except stories.StoryError as e:
        return [f"{name}: couldn't open: {e}"]
    return [f"{name}: opened by its schedule"]

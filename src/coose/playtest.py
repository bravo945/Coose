"""Scripted playtests: run a scenario through the real server and the real AI,
and write down everything each player saw.

The story is copied to a scratch folder first, so a playtest never touches
the real story's live data. Players are simulated through the same web routes
real players use, holding live connections so they count as online.

A scenario is a YAML file:

    settings:                 # optional overrides for the story's Settings.yaml
      actions_per_round: 3
    players:
      - user: ana
        character:
          name: Mara
          description: A scavenger from the coast.
          location: Drastead gate
          items: [knife, rope]
      - user: ben
        online: false         # no page open: the narrator answers for them
        character: {name: Jon, description: A gate guard., location: Drastead gate}
    steps:
      - ana: "! I pry open the warehouse door."
      - ben: "\\"\\" Who goes there?"      # shorthand for the Action: I say, "Who goes there?"
      - ana: "? What do I know about the GCSA?"
      - vote: [ana, ben]
"""

from __future__ import annotations

import shutil
import tempfile
import time
from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from . import accounts, aisettings, db, game, rounds, stories
from .config import Config

PROMPT_PREFIXES = (('""', "say"), ("!", "action"), ("?", "question"))
PASSWORD = "playtest-password"


def money(value) -> str:
    return "?" if value is None else f"${value:.2f}"


class ScenarioError(ValueError):
    pass


@dataclass
class Player:
    user: str
    character: dict
    online: bool = True


@dataclass
class Scenario:
    players: list[Player]
    steps: list[dict]
    settings: dict = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> "Scenario":
        try:
            raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as e:
            raise ScenarioError(f"The scenario isn't valid YAML: {e}") from None
        players = []
        for p in raw.get("players") or []:
            if not isinstance(p, dict) or "user" not in p or not isinstance(p.get("character"), dict):
                raise ScenarioError("Each player needs a user and a character.")
            players.append(Player(str(p["user"]), p["character"], bool(p.get("online", True))))
        if not players:
            raise ScenarioError("The scenario has no players.")
        steps = raw.get("steps") or []
        users = {p.user for p in players}
        for step in steps:
            if not isinstance(step, dict) or len(step) != 1:
                raise ScenarioError(f"Each step is one 'user: prompt', 'vote: [users]' or 'wait: seconds': {step!r}")
            (key, value), = step.items()
            if key not in users | {"vote", "wait"}:
                raise ScenarioError(f"Step for unknown player '{key}'.")
            if key in users:
                parse_prompt(str(value))
        return cls(players, steps, raw.get("settings") or {})


def parse_prompt(text: str) -> tuple[str, str]:
    text = text.strip()
    for prefix, kind in PROMPT_PREFIXES:
        if text.startswith(prefix):
            rest = text[len(prefix):].strip()
            if kind == "say":  # speech is an Action now
                return "action", f'I say, "{rest}"'
            return kind, rest
    raise ScenarioError(f"Start each prompt with !, ? or \"\": {text[:40]!r}")


def run(config: Config, story_name: str, scenario: Scenario, out: Path, keep: bool = False,
        narrator=None, log=print) -> Path:
    """Run the scenario and write a Markdown transcript to `out`. Returns the scratch folder."""
    from fastapi.testclient import TestClient

    from .narrator import ClaudeNarrator
    from .web.app import create_app

    source = stories.find_story(config.stories_dir, story_name)
    scratch = Path(tempfile.mkdtemp(prefix="coose-playtest-"))
    try:
        story_dir = scratch / "stories" / source.name
        shutil.copytree(source.path, story_dir, ignore=shutil.ignore_patterns(".coose"))
        if scenario.settings:
            settings_path = story_dir / stories.SETTINGS_PATH
            current = yaml.safe_load(settings_path.read_text(encoding="utf-8")) or {}
            current.update(scenario.settings)
            settings_path.write_text(yaml.safe_dump(current, sort_keys=False), encoding="utf-8")
        test_config = Config(host="127.0.0.1", port=0, data_dir=scratch / "data", stories_dir=scratch / "stories",
                             secret_key_env=config.secret_key_env, api_key_env=config.api_key_env,
                             model=config.model, effort=config.effort, cache_ttl=config.cache_ttl,
                             ai_tasks=config.ai_tasks, action_passes=config.action_passes)
        server = db.connect(test_config.server_db_path)
        for p in scenario.players:
            accounts.create_account(server, p.user, PASSWORD)
        story = stories.run_story(server, test_config.stories_dir, source.name)
        if narrator is None:
            def record(r: dict) -> None:
                c = db.connect(test_config.server_db_path)
                try:
                    db.log_usage(c, source.name, r)
                finally:
                    c.close()
            # The same models and effort the owner chose for this story.
            owner_db = db.connect(config.server_db_path)
            try:
                chosen = aisettings.profile(owner_db, source.name, config.ai_defaults())
            finally:
                owner_db.close()
            narrator = ClaudeNarrator(config.model, config.effort, config.api_key_env, usage_log=record,
                                      cache_ttl=config.cache_ttl, profile=lambda: chosen)
        app = create_app(test_config, secret_key="playtest", narrator=narrator)
        transcript = Transcript(story.display_name, scenario, config.cache_ttl)

        with ExitStack() as stack:
            clients = {}
            for p in scenario.players:
                c = stack.enter_context(TestClient(app))
                c.post("/login", data={"username": p.user, "password": PASSWORD})
                ch = p.character
                items = list(ch.get("items") or [])[:4] + [""] * 4
                r = c.post("/characters/new", data={
                    "name": ch.get("name", ""), "description": ch.get("description", ""),
                    "location": ch.get("location", ""), "item1": items[0], "item2": items[1],
                    "item3": items[2], "item4": items[3], "visibility": "public",
                }, follow_redirects=False)
                if r.status_code != 303:
                    raise ScenarioError(f"Couldn't create {ch.get('name')!r} for {p.user}: "
                                        f"{_error_text(r.text) or r.status_code}")
                clients[p.user] = c
            for p in scenario.players:
                if p.online:
                    stack.enter_context(clients[p.user].websocket_connect("/ws"))

            sconn = stories.open_story_db(stories.find_story(test_config.stories_dir, source.name))
            stack.callback(sconn.close)
            seen = 0
            for number, step in enumerate(scenario.steps, 1):
                (key, value), = step.items()
                started = time.monotonic()
                if key == "wait":
                    time.sleep(float(value))
                    label = f"wait {value}s"
                elif key == "vote":
                    for user in value or []:
                        clients[user].post("/vote")
                    label = f"vote by {', '.join(value or [])}"
                else:
                    kind, text = parse_prompt(str(value))
                    r = clients[key].post("/prompt", data={"kind": kind, "text": text})
                    error = _error_text(r.text)
                    label = f"{key} ({kind}): {text}" + (f"\n  > refused: {error}" if error else "")
                seconds = time.monotonic() - started
                log(f"[{number}/{len(scenario.steps)}] {label.splitlines()[0][:70]} ({seconds:.1f}s)")
                new = sconn.execute(
                    "SELECT m.id, m.kind, m.text, m.username, c.name FROM messages m "
                    "JOIN characters c ON c.id = m.character_id WHERE m.id > ? ORDER BY m.id", (seen,)
                ).fetchall()
                seen = max([seen] + [m["id"] for m in new])
                transcript.step(number, label, seconds, new, rounds.current_round(sconn),
                                game.world_time(sconn, story.game_settings()))

            transcript.finish(sconn, story_dir, db.usage_summary(db.connect(test_config.server_db_path)))
        out.write_text(transcript.render(), encoding="utf-8")
        return scratch
    finally:
        if not keep:
            shutil.rmtree(scratch, ignore_errors=True)


def _error_text(page: str) -> str | None:
    """The error alert on a page the player was sent back to, if any."""
    import html
    import re

    m = re.search(r'class="alert alert--bad" role="alert">(.*?)</p>', page, re.S)
    return html.unescape(m.group(1)).strip() if m else None


class Transcript:
    def __init__(self, title: str, scenario: Scenario, cache_ttl: str = "1h"):
        self.cache_ttl = cache_ttl
        self.lines = [f"# Playtest: {title}", ""]
        self.lines += ["Players: " + ", ".join(
            f"{p.user} as {p.character.get('name')}{'' if p.online else ' (away)'}" for p in scenario.players), ""]
        if scenario.settings:
            self.lines += ["Settings: " + ", ".join(f"{k}={v}" for k, v in scenario.settings.items()), ""]
        self.total = 0.0

    def step(self, number, label, seconds, messages, round_number, world_time) -> None:
        self.total += seconds
        self.lines += [f"## Step {number}: {label}", "",
                       f"*{seconds:.1f}s · round {round_number} · {world_time:%a %d %b %Y, %H:%M}*", ""]
        by_character: dict[str, list] = {}
        for m in messages:
            by_character.setdefault(m["name"], []).append(m)
        for name, items in by_character.items():
            self.lines.append(f"**{name}'s chatroom**")
            self.lines.append("")
            for m in items:
                who = f" ({m['username']})" if m["username"] else ""
                body = m["text"].strip().replace("\n", "\n  ")
                self.lines.append(f"- `{m['kind']}`{who}: {body}")
            self.lines.append("")
        if not messages:
            self.lines += ["(no new messages)", ""]

    def finish(self, sconn, story_dir: Path, usage) -> None:
        self.lines += ["## End state", "", f"Total time: {self.total:.0f}s", ""]
        self.lines += ["### Characters", ""]
        for r in sconn.execute("SELECT * FROM characters ORDER BY id"):
            c = game.Character.from_row(r)
            self.lines.append(f"- **{c.name}**{'' if c.alive else ' (dead)'}{' (AI-created)' if c.owner is None else ''}"
                              f": {c.location or '?'}; {c.condition}; carries {', '.join(c.inventory) or 'nothing'}; "
                              f"knows {len(c.knowledge)} facts")
        self.lines += ["", "### Working memory", ""]
        self.lines += [f"- {m}" for m in game.working_memory(sconn)] or ["(empty)"]
        self.lines += ["", "### World State", "", game.world_state(sconn).strip(), ""]
        for entry in sorted((story_dir / rounds.LOGBOOK_DIR).glob("Round [0-9]*.md")):
            self.lines += [f"### Logbook: {entry.name}", "", entry.read_text(encoding="utf-8").strip(), ""]
        self.lines += ["### AI usage", "",
                       "| Pass | Calls | Input | Output | Cache read | Cache write | Avg s | Est. $ |",
                       "|---|---|---|---|---|---|---|---|"]
        self.lines += [f"| {u['pass']} | {u['calls']} | {u['input']} | {u['output']} | {u['cache_read']} | "
                       f"{u['cache_write']} | {u['avg_seconds']} | {money(u['cost'])} |" for u in usage] \
            or ["| (none) | | | | | | | |"]
        total = sum(u["cost"] or 0 for u in usage)
        self.lines += ["", f"Estimated total: {money(total)} (Premise cache kept for {self.cache_ttl})."]

    def render(self) -> str:
        return "\n".join(self.lines).rstrip() + "\n"

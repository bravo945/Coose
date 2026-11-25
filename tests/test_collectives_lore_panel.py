"""Collective characters, AI-written lore, and the owner's private panel."""

import html
import sqlite3

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from coose import accounts, admin, controls, game, lore, rounds, stories
from coose.cli import app as cli
from coose.narrator import InterventionResult, LoreEntry, Part
from coose.web.app import create_app
from coose.web.panel import create_panel


@pytest.fixture
def story(config, story_dir):
    return stories.find_story(config.stories_dir, "demo")


@pytest.fixture
def sconn(story):
    c = stories.open_story_db(story)
    yield c
    c.close()


# --- Collectives -------------------------------------------------------------------

def make_militia(sconn, story):
    return game.create_character(sconn, story.game_settings(), name="Red Militia", description="Forty fighters.",
                                 location="Southside", items=[], visibility="public", owner="nil", collective=True)


def test_collectives_wait_for_approval(story, sconn):
    militia = make_militia(sconn, story)
    assert militia.pending and militia.collective
    assert militia not in game.playable_characters(sconn, "nil")
    with pytest.raises(game.GameError):
        game.join(sconn, story.game_settings(), "nil", militia.id)
    other = game.create_character(sconn, story.game_settings(), name="Mara", description="d", location="Southside",
                                  items=[], visibility="public", owner="jon")
    assert game.other_characters(sconn, other.id) == []  # not in the world yet

    assert "Red Militia" in admin.run(sconn, story.path, "/approve").reply
    assert "now a playable collective" in admin.run(sconn, story.path, "/approve red militia").reply
    assert game.get_character(sconn, militia.id).can_be_played_by("jon")
    assert "approved" in game.recent_messages(sconn, militia.id)[-1].text


def test_rejected_collectives_are_deleted(story, sconn):
    militia = make_militia(sconn, story)
    assert "Rejected" in admin.run(sconn, story.path, "/reject Red Militia").reply
    assert game.get_character(sconn, militia.id) is None


def test_stories_can_turn_collectives_off(story_dir, config):
    settings = story_dir / "Rulebook" / "Settings.yaml"
    settings.write_text(settings.read_text().replace("collectives: true", "collectives: false"))
    s = stories.find_story(config.stories_dir, "demo")
    c = stories.open_story_db(s)
    with pytest.raises(game.GameError, match="doesn't allow collective"):
        make_militia(c, s)


def test_parts_change_only_for_collectives(story, sconn):
    militia = make_militia(sconn, story)
    game.decide_collective(sconn, "Red Militia", approve=True)
    parts = [Part(name="North patrol", location="Drastead gate", members="8 fighters"),
             Part(name="Main body", location="Southside", members="32 fighters")]
    after = game.apply_action(sconn, militia, game.ActionOutcome(narration="", minutes_elapsed=0, parts=parts))
    assert [p["name"] for p in after.parts] == ["North patrol", "Main body"]
    assert "North patrol (8 fighters) at Drastead gate" in game.describe_parts(after.parts)
    single = game.create_character(sconn, story.game_settings(), name="Mara", description="d", location="x",
                                   items=[], visibility="public", owner="jon")
    assert game.apply_action(sconn, single, game.ActionOutcome(narration="", minutes_elapsed=0,
                                                               parts=parts)).parts == []


def test_web_request_for_a_collective(config, conn, story_dir):
    accounts.create_account(conn, "nil", "correct horse")
    stories.run_story(conn, config.stories_dir, "demo")
    c = TestClient(create_app(config, secret_key="s", narrator=object()))
    c.post("/login", data={"username": "nil", "password": "correct horse"})
    assert "A collective" in c.get("/characters/new").text
    page = c.post("/characters/new", data={"name": "Red Militia", "description": "Forty fighters.",
                                           "location": "Southside", "visibility": "public", "collective": "true"})
    assert "waiting for a story admin" in html.unescape(page.text)


def test_old_story_databases_gain_the_new_columns(story):
    path = story.path / stories.STORY_DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    old = sqlite3.connect(path)
    old.executescript(game.SCHEMA.replace(
        ",\n    collective    INTEGER NOT NULL DEFAULT 0,    -- a group played as one character\n"
        "    pending       INTEGER NOT NULL DEFAULT 0,    -- waiting for a story admin's approval\n"
        "    parts         TEXT NOT NULL DEFAULT '[]'     -- JSON list of {name, location, members} for a collective",
        ""))
    old.execute("INSERT INTO characters (name, description) VALUES ('Old', 'From before.')")
    old.commit()
    assert "collective" not in {r[1] for r in old.execute("PRAGMA table_info(characters)")}
    old.close()
    c = stories.open_story_db(story)
    assert game.find_character_by_name(c, "Old").collective is False


# --- Lore -------------------------------------------------------------------------------

def test_round_lore_goes_to_ai_additions_only(story, sconn):
    premise_before = sorted(p.name for p in (story.path / "Premise").rglob("*.md"))
    outcome = rounds.RoundOutcome(title="T", summary="S", relevant_characters="", deaths="", world_state="W",
                                  working_memory=[], lore=[{"title": "Drastead gate procedure",
                                                            "text": "Three questions: name, origin, cargo."}])
    rounds.apply_round(sconn, story.path, story.game_settings(), outcome, set())
    written = story.path / lore.AI_ADDITIONS / "Drastead gate procedure.md"
    assert "Three questions" in written.read_text() and "Unreviewed" in written.read_text()
    after = sorted(p.name for p in (story.path / "Premise").rglob("*.md"))
    assert set(after) - set(premise_before) == {"Drastead gate procedure.md"}
    lore.write_lore(story.path, [LoreEntry(title="Drastead gate procedure", text="Also a fee.")], "later")
    assert "Also a fee." in written.read_text() and "Three questions" in written.read_text()
    assert lore.write_lore(story.path, [{"title": "../../etc/passwd", "text": "x"}], "t") == ["etcpasswd.md"]


def test_interventions_can_add_lore(story, sconn):
    result = InterventionResult(reply="ok", world_state="", memory_add=[], characters=[],
                                lore=[LoreEntry(title="Kessler's patrol", text="Three Groundwalkers.")])
    admin.apply_intervention(sconn, result, story.path)
    assert "Kessler's patrol.md" in lore.pending_lore(story.path)


# --- Owner panel ------------------------------------------------------------------------

@pytest.fixture
def panel(config, story_dir):
    return TestClient(create_panel(config, password="panel-pass", secret_key="s"))


def test_panel_needs_its_password(panel):
    assert panel.get("/", follow_redirects=False).headers["location"] == "/login"
    assert panel.post("/login", data={"password": "nope"}).status_code == 401
    assert "Owner panel" in panel.post("/login", data={"password": "panel-pass"}).text


def test_panel_controls(panel, conn, config):
    panel.post("/login", data={"password": "panel-pass"})
    page = html.unescape(panel.post("/accounts", data={"username": "ray", "password": "correct horse",
                                                       "do_what": "add"}).text)
    assert "Created account 'ray'." in page
    assert "Now running" in html.unescape(panel.post("/run", data={"name": "demo"}).text)
    assert stories.status(conn).online
    panel.post("/pause")
    assert stories.status(conn).paused
    panel.post("/admins", data={"name": "demo", "username": "ray"})
    assert controls.admins(conn, stories.find_story(config.stories_dir, "demo")) == ["ray"]
    panel.post("/schedule", data={"name": "demo", "days": "fri", "start": "19:00", "end": "23:00"})
    assert controls.get_schedule(conn, "demo")[0].describe() == "fri 19:00-23:00"
    page = html.unescape(panel.post("/cap", data={"name": "demo", "actions": "0", "rounds": ""}).text)
    assert "cap reached" in page
    assert "reached its cap" in html.unescape(panel.post("/resume").text)
    assert stories.status(conn).paused
    panel.post("/cap", data={"name": "demo", "clear": "1"})
    panel.post("/resume")
    assert stories.status(conn).online
    assert "isn't a whole number" in html.unescape(panel.post("/cap", data={"name": "demo", "actions": "x"}).text)


def test_panel_refuses_to_listen_everywhere(config, monkeypatch):
    toml = config.data_dir.parent / "coose.toml"
    toml.write_text(toml.read_text() + '\n[panel]\nhost = "0.0.0.0"\n')
    run = lambda: CliRunner().invoke(cli, ["--config", str(toml), "panel"])
    monkeypatch.delenv("COOSE_PANEL_PASSWORD", raising=False)
    assert "COOSE_PANEL_PASSWORD" in run().output
    monkeypatch.setenv("COOSE_PANEL_PASSWORD", "x")
    result = run()
    assert result.exit_code == 1 and "every network" in result.output

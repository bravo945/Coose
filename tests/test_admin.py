"""Milestone 6: story admins and their commands, caps, and schedules."""

import html
from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from coose import accounts, admin, controls, game, rounds, stories
from coose.cli import app as cli
from coose.narrator import AuthorResult, CharacterChange, InterventionResult
from coose.web.app import create_app


class FakeNarrator:
    def __init__(self):
        self.calls = []

    def action(self, premise, scene, text, existing=None):
        self.calls.append("action")
        return AuthorResult(narration="Done.", minutes_elapsed=1, location="", condition="", inventory_add=[],
                            inventory_remove=[], learned=[], needs_met=[], alive=True, memory_add=[],
                            new_characters=[])

    def witnesses(self, *a, **k):
        return []

    def intervene(self, premise, context, command, text):
        self.calls.append((command, text))
        return InterventionResult(
            reply="The gate is now shut.", world_state="# World State\nThe gate is shut.",
            memory_add=["An admin shut the gate."],
            characters=[CharacterChange(name="Mara", notice="The gate slams shut in front of you.", condition="",
                                        location="", inventory_add=[], inventory_remove=[], learned=["The gate is shut."],
                                        alive=True)],
        )


@pytest.fixture
def narrator():
    return FakeNarrator()


@pytest.fixture
def web(config, conn, story_dir, narrator):
    for name in ("nil", "jon", "boss"):
        accounts.create_account(conn, name, "correct horse")
    stories.run_story(conn, config.stories_dir, "demo")
    controls.add_admin(conn, "boss", "demo")
    return create_app(config, secret_key="test-secret", narrator=narrator)


def login(app, username):
    c = TestClient(app)
    c.post("/login", data={"username": username, "password": "correct horse"})
    return c


def play(app, username, character):
    c = login(app, username)
    c.post("/characters/new", data={"name": character, "description": "Someone.", "location": "Gate",
                                    "item1": "rope", "item2": "", "item3": "", "item4": "", "visibility": "public"})
    return c


@pytest.fixture
def sconn(config, story_dir):
    c = stories.open_story_db(stories.find_story(config.stories_dir, "demo"))
    yield c
    c.close()


def command(client, text):
    client.post("/admin", data={"text": text})
    return html.unescape(client.get("/admin").text)


# --- Admins ---------------------------------------------------------------------

def test_only_story_admins_reach_the_admin_chatroom(web):
    mara, boss = play(web, "nil", "Mara"), login(web, "boss")
    assert mara.get("/admin", follow_redirects=False).status_code == 303
    page = boss.get("/admin")
    assert page.status_code == 200 and "Admin chatroom" in page.text and "Mara" in page.text
    assert 'href="/admin"' in boss.get("/characters").text
    assert 'href="/admin"' not in mara.get("/").text


def test_admins_listed_in_settings_count_too(conn, story_dir, config):
    settings = story_dir / "Rulebook" / "Settings.yaml"
    settings.write_text(settings.read_text().replace("admins: []", "admins: [ray]"))
    story = stories.find_story(config.stories_dir, "demo")
    assert controls.is_admin(conn, story, "Ray") and not controls.is_admin(conn, story, "nil")


def test_owner_appoints_and_removes_admins_from_the_terminal(conn, config, story_dir):
    accounts.create_account(conn, "ray", "correct horse")
    run = lambda *args: CliRunner().invoke(cli, ["--config", str(config.data_dir.parent / "coose.toml"), *args])
    assert "now a story admin" in run("admin", "add", "ray", "demo").output
    assert run("admin", "list", "demo").output.strip() == "ray"
    assert run("admin", "add", "ghost", "demo").exit_code == 1
    assert "no longer" in run("admin", "remove", "ray", "demo").output


# --- Commands -------------------------------------------------------------------

def test_inventory_life_and_sheet_commands(web, sconn):
    mara, boss = play(web, "nil", "Mara"), login(web, "boss")
    page = command(boss, "/give Mara: lantern")
    assert "Gave lantern to Mara." in page
    assert "lantern" in game.find_character_by_name(sconn, "Mara").inventory
    assert "Your inventory changed: + lantern." in html.unescape(mara.get("/live").text)
    assert "doesn't carry 'sword'" in command(boss, "/take Mara: sword")
    command(boss, "/take Mara: rope")
    assert game.find_character_by_name(sconn, "Mara").inventory == ["lantern"]

    command(boss, "/edit character Mara: condition = Broken arm")
    assert game.find_character_by_name(sconn, "Mara").condition == "Broken arm"
    command(boss, "/kill Mara")
    assert not game.find_character_by_name(sconn, "Mara").alive
    command(boss, "/give Mara: coin")
    assert not game.find_character_by_name(sconn, "Mara").alive  # giving items doesn't revive
    command(boss, "/revive Mara")
    assert game.find_character_by_name(sconn, "Mara").alive
    assert "no character named 'Nobody'" in command(boss, "/kill Nobody")
    assert "Unknown command '/fly'" in command(boss, "/fly")


def test_time_and_world_commands(web, sconn):
    boss = login(web, "boss")
    command(boss, "/time +2h")
    assert game.clock_minutes(sconn) == 120
    command(boss, "/time -30m")
    assert game.clock_minutes(sconn) == 90
    assert "before the story's start" in command(boss, "/time -1d")
    command(boss, "/edit world # World State\nEveryone left.")
    assert game.world_state(sconn) == "# World State\nEveryone left."


def test_removed_players_cannot_play_until_added_back(web, sconn):
    mara, boss = play(web, "nil", "Mara"), login(web, "boss")
    command(boss, "/remove player nil")
    assert game.current_character(sconn, "nil") is None
    mara_id = game.find_character_by_name(sconn, "Mara").id
    assert "removed you from this story" in html.unescape(mara.post(f"/characters/{mara_id}/join").text)
    command(boss, "/add player nil")
    mara.post(f"/characters/{mara_id}/join")
    assert game.current_character(sconn, "nil").name == "Mara"


def test_intervene_goes_through_the_ai_and_tells_only_who_perceives_it(web, sconn, narrator):
    mara, jon, boss = play(web, "nil", "Mara"), play(web, "jon", "Jon"), login(web, "boss")
    page = command(boss, "/intervene Shut the gate on everyone.")
    assert narrator.calls == [("intervene", "Shut the gate on everyone.")]
    assert "The gate is now shut." in page
    assert game.world_state(sconn) == "# World State\nThe gate is shut."
    assert "An admin shut the gate." in game.working_memory(sconn)
    assert "The gate is shut." in game.find_character_by_name(sconn, "Mara").knowledge
    assert "slams shut" in html.unescape(mara.get("/live").text)
    assert "slams shut" not in html.unescape(jon.get("/live").text)
    assert "Shut the gate" not in html.unescape(mara.get("/live").text)  # commands stay out of chatrooms


def test_ai_written_lore_is_approved_or_rejected(web, story_dir):
    boss = login(web, "boss")
    additions = story_dir / "Premise" / "AI Additions"
    additions.mkdir(exist_ok=True)
    (additions / "Ferry.md").write_text("A ferry crosses the bay.")
    (additions / "Ghost.md").write_text("A ghost haunts the dam.")
    assert "Ferry.md, Ghost.md" in command(boss, "/approve")
    command(boss, "/approve Ferry")
    assert (story_dir / "Premise" / "Ferry.md").exists() and not (additions / "Ferry.md").exists()
    command(boss, "/reject Ghost.md")
    assert not (additions / "Ghost.md").exists()


def test_round_and_rollback_commands(web, sconn, story_dir, config):
    boss = login(web, "boss")
    command(boss, "/give Nobody: x")  # errors are just replies
    command(boss, "/time +1h")
    assert "Usage: /rollback" in command(boss, "/rollback")
    assert "back at the start of round 1" in command(boss, "/rollback 1")
    assert game.clock_minutes(sconn) == 0


def test_round_command_forces_a_round_change(web, sconn, narrator):
    from coose.narrator import RoundResult

    narrator.round_change = lambda premise, context: RoundResult(
        title="Forced", summary="An admin ended the round.", relevant_characters="", deaths="",
        world_state="# World State\nLater.", working_memory=[], characters=[])
    boss = login(web, "boss")
    assert "Forcing a round change." in command(boss, "/round")
    assert rounds.current_round(sconn) == 2


def test_parse_duration():
    assert admin.parse_duration("+90m") == 90
    assert admin.parse_duration("-2 h") == -120
    assert admin.parse_duration("+1d") == 1440
    with pytest.raises(admin.CommandError):
        admin.parse_duration("soon")


# --- Caps -----------------------------------------------------------------------

def test_a_cap_pauses_the_story_after_the_last_allowed_action(web, conn, config, sconn):
    story = stories.find_story(config.stories_dir, "demo")
    controls.set_cap(conn, story, more_actions=2, more_rounds=None)
    mara = play(web, "nil", "Mara")
    mara.post("/prompt", data={"kind": "action", "text": "I wait."})
    assert stories.status(conn).online
    mara.post("/prompt", data={"kind": "action", "text": "I wait again."})
    assert not stories.status(conn).online
    assert controls.story_cap_reached(conn, story)
    run = lambda *args: CliRunner().invoke(cli, ["--config", str(config.data_dir.parent / "coose.toml"), *args])
    assert run("resume").exit_code == 1
    run("cap", "demo", "--clear")
    assert run("resume").exit_code == 0


def test_round_caps_count_from_now(conn, config, story_dir):
    story = stories.find_story(config.stories_dir, "demo")
    cap = controls.set_cap(conn, story, more_actions=None, more_rounds=2)
    assert cap.rounds == 3 and cap.actions is None
    assert "pauses when round 3 begins" in controls.describe_cap(conn, story)
    with pytest.raises(controls.ControlError):
        controls.set_cap(conn, story, None, None)


# --- Schedules ------------------------------------------------------------------

@pytest.fixture
def two_stories(config, conn, story_dir):
    import shutil

    shutil.copytree(story_dir, config.stories_dir / "zeta")
    return config.stories_dir


def test_schedule_opens_and_closes_at_its_edges(conn, two_stories):
    controls.add_span(conn, "demo", "fri", "19:00", "23:00")  # 2026-11-27 is a Friday
    t = lambda h, m=0: datetime(2026, 11, 27, h, m)
    assert controls.schedule_tick(conn, two_stories, t(18, 59), t(19, 0)) == ["demo: opened by its schedule"]
    assert stories.status(conn).online
    stories.pause(conn)  # a manual pause in between is left alone
    assert controls.schedule_tick(conn, two_stories, t(19, 0), t(22, 0)) == []
    assert stories.status(conn).paused
    stories.resume(conn)
    assert controls.schedule_tick(conn, two_stories, t(22, 0), t(23, 0)) == ["demo: closed by its schedule"]
    assert stories.status(conn).paused


def test_overlapping_schedules_later_start_wins_then_hands_back(conn, two_stories):
    controls.add_span(conn, "demo", "daily", "19:00", "23:00")
    controls.add_span(conn, "zeta", "daily", "21:00", "22:00")
    t = lambda h: datetime(2026, 11, 27, h)
    controls.schedule_tick(conn, two_stories, t(18), t(20))
    assert stories.status(conn).story == "demo"
    controls.schedule_tick(conn, two_stories, t(20), t(21))
    assert stories.status(conn).story == "zeta"
    controls.schedule_tick(conn, two_stories, t(21), t(22))
    assert stories.status(conn).story == "demo" and stories.status(conn).online


def test_exact_tie_goes_to_the_later_name(conn, two_stories):
    controls.add_span(conn, "zeta", "daily", "19:00", "20:00")
    controls.add_span(conn, "demo", "daily", "19:00", "20:00")
    controls.schedule_tick(conn, two_stories, datetime(2026, 11, 27, 18), datetime(2026, 11, 27, 19))
    assert stories.status(conn).story == "zeta"


def test_overnight_spans_and_day_parsing(conn):
    span = controls.add_span(conn, "demo", "weekends", "22:00", "02:00")
    (s, e), = list(span.occurrences(datetime(2026, 11, 28).date(), datetime(2026, 11, 28).date()))
    assert e == datetime(2026, 11, 29, 2, 0) and span.describe() == "weekends 22:00-02:00"
    assert controls.parse_days("Fri, sat") == (4, 5)
    with pytest.raises(controls.ControlError):
        controls.parse_days("someday")


def test_capped_story_is_not_opened_by_its_schedule(conn, two_stories, config):
    story = stories.find_story(config.stories_dir, "demo")
    controls.set_cap(conn, story, more_actions=0, more_rounds=None)
    controls.add_span(conn, "demo", "daily", "19:00", "20:00")
    log = controls.schedule_tick(conn, two_stories, datetime(2026, 11, 27, 18), datetime(2026, 11, 27, 19))
    assert log == ["demo: not opened, its cap is reached"]


def test_schedule_commands(conn, config, story_dir):
    run = lambda *args: CliRunner().invoke(cli, ["--config", str(config.data_dir.parent / "coose.toml"), *args])
    assert "opens fri,sat 19:00-23:00" in run("schedule", "add", "demo", "fri,sat", "19:00", "23:00").output
    assert "demo: fri,sat 19:00-23:00" in run("schedule", "list").output
    assert run("schedule", "add", "demo", "funday", "19:00", "23:00").exit_code == 1
    run("schedule", "clear", "demo")
    assert "No schedules." in run("schedule", "list").output

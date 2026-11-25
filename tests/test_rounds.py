import html
import time

import pytest
from fastapi.testclient import TestClient

from coose import accounts, game, rounds, stories
from coose.narrator import AuthorResult, BackfillResult, CharacterRoundUpdate, NarratorError, RoundResult
from coose.web.app import create_app


def set_settings(story_dir, **values):
    path = story_dir / "Rulebook" / "Settings.yaml"
    text = path.read_text(encoding="utf-8")
    for key, value in values.items():
        lines = [line for line in text.splitlines() if not line.startswith(f"{key}:")]
        text = "\n".join(lines) + f"\n{key}: {value}\n"
    path.write_text(text, encoding="utf-8")


# --- The rounds module on its own ------------------------------------------------

@pytest.fixture
def story(story_dir):
    set_settings(story_dir, start_time='"2035-03-01 06:00"', tracked_needs="{food: 10}")
    return stories.load_story(story_dir)


@pytest.fixture
def sconn(story):
    c = stories.open_story_db(story)
    yield c
    c.close()


def outcome(**overrides):
    base = dict(title="First light", summary="Mara reached the gate.", relevant_characters="Mara, alive.",
                deaths="", world_state="# World State\nThe gate is open.", working_memory=["Keep: Jon owes Mara."],
                characters=[rounds.CharacterUpdate(name="mara", notice="Dawn breaks.", condition="Tired")])
    base.update(overrides)
    return rounds.RoundOutcome(**base)


def make(sconn, story, name, location="Gate"):
    return game.create_character(sconn, story.game_settings(), name=name, description="Someone.",
                                 location=location, items=[], visibility="public", owner="nil")


def test_round_one_is_snapshotted_when_the_story_first_opens(story, sconn):
    assert rounds.snapshots(story.path) == [1]
    assert rounds.current_round(sconn) == 1


def test_apply_round_archives_advances_and_notifies(story, sconn):
    settings = story.game_settings()
    mara, jon = make(sconn, story, "Mara"), make(sconn, story, "Jon")
    game.add_memory(sconn, ["Old detail."])
    rounds.add_skip(sconn, mara.id, 90, "I sleep.")
    rounds.add_skip(sconn, jon.id, 30, "I wait.")
    rounds.add_vote(sconn, "nil")

    assert rounds.apply_round(sconn, story.path, settings, outcome(), online_ids={mara.id, jon.id}) == 2

    entry = (story.path / "Logbook" / "Round 1.md").read_text()
    assert entry.startswith("# Round 1 (First light)") and "Mara reached the gate." in entry
    assert "## Deaths\nNone." in entry and "## World State at the start of the round" in entry and "Write the starting state here" in entry
    assert game.world_state(sconn) == "# World State\nThe gate is open."
    assert game.working_memory(sconn) == ["Keep: Jon owes Mara."]
    assert game.clock_minutes(sconn) == 90  # the longest skip
    assert game.get_character(sconn, mara.id).condition == "Tired"
    assert game.recent_messages(sconn, mara.id)[-1].text == "Round 2. Dawn breaks."
    assert game.recent_messages(sconn, jon.id) == []
    assert rounds.pending_skips(sconn) == [] and rounds.votes(sconn) == [] and rounds.round_actions(sconn) == 0
    assert rounds.snapshots(story.path) == [1, 2]


def test_offline_characters_can_wait_for_a_backstory(story, story_dir, sconn):
    set_settings(story_dir, offline_during_skips="backfill_on_return")
    story = stories.load_story(story_dir)
    mara, jon = make(sconn, story, "Mara"), make(sconn, story, "Jon")
    rounds.add_skip(sconn, mara.id, 600, "I walk to the coast.")
    rounds.apply_round(sconn, story.path, story.game_settings(), outcome(characters=[
        rounds.CharacterUpdate(name="Jon", notice="should be ignored", condition="Changed")]), online_ids={mara.id})
    assert rounds.backfills(sconn) == {jon.id: 600}
    assert game.get_character(sconn, jon.id).condition != "Changed"
    assert rounds.take_backfill(sconn, jon.id) == 600 and rounds.backfills(sconn) == {}


def test_rollback_restores_an_earlier_round(story, sconn):
    settings = story.game_settings()
    mara = make(sconn, story, "Mara")
    with sconn:
        sconn.execute("INSERT INTO players (username, character_id) VALUES ('nil', ?)", (mara.id,))
    rounds.apply_round(sconn, story.path, settings, outcome(), online_ids=set())
    make(sconn, story, "Jon")
    game.apply_action(sconn, mara, game.ActionOutcome(narration="", minutes_elapsed=45))
    rounds.apply_round(sconn, story.path, settings, outcome(), online_ids=set())
    assert rounds.current_round(sconn) == 3 and rounds.snapshots(story.path) == [1, 2, 3]

    rounds.rollback(story.path, sconn, 2)
    assert rounds.current_round(sconn) == 2
    assert game.find_character_by_name(sconn, "Jon") is None
    assert game.clock_minutes(sconn) == 0
    assert game.current_character(sconn, "nil").name == "Mara"
    assert rounds.snapshots(story.path) == [1, 2]
    assert sorted(p.name for p in (story.path / "Logbook").glob("Round [0-9]*.md")) == ["Round 1.md"]
    with pytest.raises(rounds.RoundError):
        rounds.rollback(story.path, sconn, 7)


def test_needs_run_down_for_everyone_on_the_world_clock(story, sconn):
    settings = story.game_settings()
    mara, idle = make(sconn, story, "Mara"), make(sconn, story, "Idle")
    game.apply_action(sconn, mara, game.ActionOutcome(narration="", minutes_elapsed=8 * 60, needs_met=["food"]))
    assert game.need_levels(sconn, game.get_character(sconn, mara.id), settings) == {"food": "fine"}
    assert game.need_levels(sconn, game.get_character(sconn, idle.id), settings) == {"food": "urgent"}


def test_bad_skip_mode_is_a_story_problem(story_dir):
    set_settings(story_dir, offline_during_skips="sometimes")
    assert not stories.load_story(story_dir).valid


# --- Through the web app -----------------------------------------------------------

class FakeNarrator:
    def __init__(self):
        self.calls = []
        self.minutes = 5
        self.fail_round = False

    def action(self, premise, scene, text, existing=None):
        self.calls.append("action")
        return AuthorResult(
            narration=f"You do it: {text}", minutes_elapsed=self.minutes, location="", condition="",
            inventory_add=[], inventory_remove=[], learned=[], needs_met=[], alive=True, memory_add=[],
            new_characters=[],
        )

    def question(self, premise, scene, text):
        self.calls.append("question")
        return "Not sure."

    def witnesses(self, premise, scene, text, result, others):
        self.calls.append("witnesses")
        return []

    def reply_as(self, premise, scene, speaker, text, mode):
        return "Hm."

    def round_change(self, premise, context):
        self.calls.append("round")
        self.context = context
        if self.fail_round:
            raise NarratorError("the AI is busy right now")
        return RoundResult(
            title="Turn", summary="Things happened.", relevant_characters="Everyone.", deaths="",
            world_state="# World State\nLater.", working_memory=[],
            characters=[CharacterRoundUpdate(name=n, notice=f"Time moves on for {n}.", condition="", location="",
                                             learned=[], alive=True) for n in ("Mara", "Jon")],
        )

    def backfill(self, premise, scene, minutes):
        self.calls.append(("backfill", scene.character.name, minutes))
        return BackfillResult(narration="You kept watch at the gate.", location="", condition="", learned=[])


@pytest.fixture
def narrator():
    return FakeNarrator()


@pytest.fixture
def app(config, conn, story_dir, narrator):
    for name in ("nil", "jon"):
        accounts.create_account(conn, name, "correct horse")
    stories.run_story(conn, config.stories_dir, "demo")
    return create_app(config, secret_key="test-secret", narrator=narrator)


def player(app, username, character):
    c = TestClient(app)
    c.post("/login", data={"username": username, "password": "correct horse"})
    c.post("/characters/new", data={"name": character, "description": "Someone.", "location": "Gate",
                                    "item1": "", "item2": "", "item3": "", "item4": "", "visibility": "public"})
    return c


def page(client):
    return html.unescape(client.get("/").text)


def story_db(config):
    return stories.open_story_db(stories.find_story(config.stories_dir, "demo"))


def act(client, text="I look around."):
    return client.post("/prompt", data={"kind": "action", "text": text}, follow_redirects=False)


def test_round_changes_after_the_set_number_of_actions(app, narrator, story_dir, config):
    set_settings(story_dir, actions_per_round=2)
    mara = player(app, "nil", "Mara")
    act(mara)
    assert "round" not in narrator.calls
    act(mara)
    assert narrator.calls.count("round") == 1
    assert "[Mara action] I look around." in narrator.context
    text = page(mara)
    assert "Round 2" in text and "Time moves on for Mara." in text
    assert (story_dir / "Logbook" / "Round 1.md").exists()


def test_long_action_waits_for_the_round_while_others_play(app, narrator, config):
    mara, jon = player(app, "nil", "Mara"), player(app, "jon", "Jon")
    with mara.websocket_connect("/ws"), jon.websocket_connect("/ws"):
        narrator.minutes = 180
        act(mara, "I sleep.")
        assert "round" not in narrator.calls
        assert "time will skip ahead" in page(mara)
        assert "A time skip is coming" in page(jon)
        sconn = story_db(config)
        assert game.clock_minutes(sconn) == 0

        r = act(mara, "I get up.")
        assert narrator.calls.count("action") == 1
        mara.post("/prompt", data={"kind": "question", "text": "Where am I?"})
        assert "question" in narrator.calls

        narrator.minutes = 5
        act(jon, "I stand guard.")
        assert game.clock_minutes(sconn) == 5

        mara.post("/vote")
        assert "round" not in narrator.calls
        assert "1 of 2 online players voted" in page(jon)
        jon.post("/vote")
        assert narrator.calls.count("round") == 1
        assert game.clock_minutes(sconn) == 185
        assert not rounds.is_skipping(sconn, game.find_character_by_name(sconn, "Mara").id)
        sconn.close()


def test_skip_happens_at_once_when_alone(app, narrator, config):
    mara = player(app, "nil", "Mara")
    narrator.minutes = 120
    act(mara, "I walk to the next town.")
    assert narrator.calls[-1] == "round"
    sconn = story_db(config)
    assert game.clock_minutes(sconn) == 120 and rounds.current_round(sconn) == 2
    sconn.close()


def test_votes_can_be_turned_off(app, story_dir):
    set_settings(story_dir, round_vote="false")
    mara = player(app, "nil", "Mara")
    assert "Vote for the next round" not in page(mara)
    r = mara.post("/vote")
    assert "doesn't use round votes" in html.unescape(r.text)


def test_failed_round_change_keeps_everything(app, narrator, config, story_dir):
    set_settings(story_dir, actions_per_round=1)
    mara = player(app, "nil", "Mara")
    narrator.fail_round = True
    with mara.websocket_connect("/ws"):
        act(mara)
        assert "The round change failed" in page(mara)
    sconn = story_db(config)
    assert rounds.current_round(sconn) == 1 and rounds.round_due(sconn, stories.find_story(
        config.stories_dir, "demo").game_settings())
    sconn.close()


def test_returning_player_gets_a_backstory(app, narrator, config, story_dir):
    set_settings(story_dir, offline_during_skips="backfill_on_return")
    mara, jon = player(app, "nil", "Mara"), player(app, "jon", "Jon")
    narrator.minutes = 240
    with mara.websocket_connect("/ws"):
        act(mara, "I sleep.")  # Jon has no page open, so this skips at once
    assert narrator.calls[-1] == "round"
    with jon.websocket_connect("/ws"):
        for _ in range(50):
            if any(isinstance(c, tuple) for c in narrator.calls):
                break
            time.sleep(0.05)
    assert ("backfill", "Jon", 240) in narrator.calls
    for _ in range(50):
        if "kept watch" in page(jon):
            break
        time.sleep(0.05)
    assert "While you were away (4 hours): You kept watch at the gate." in page(jon)


def test_owner_can_roll_back_from_the_terminal(config, conn, story_dir):
    from typer.testing import CliRunner

    from coose.cli import app as cli

    story = stories.load_story(story_dir)
    sconn = stories.open_story_db(story)
    game.create_character(sconn, story.game_settings(), name="Mara", description="Someone.", location="",
                          items=[], visibility="public", owner="nil")
    sconn.close()
    args = ["--config", str(config.data_dir.parent / "coose.toml")]
    run = CliRunner().invoke
    assert "1" in run(cli, args + ["rounds", "demo"]).output
    stories.run_story(conn, config.stories_dir, "demo")
    assert "Pause it first" in run(cli, args + ["rollback", "demo", "1", "--yes"]).output
    stories.pause(conn)
    result = run(cli, args + ["rollback", "demo", "1", "--yes"])
    assert result.exit_code == 0, result.output
    sconn = stories.open_story_db(story)
    assert game.find_character_by_name(sconn, "Mara") is None
    sconn.close()

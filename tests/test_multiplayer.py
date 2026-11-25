import html

import pytest
from fastapi.testclient import TestClient

from coose import accounts, game, stories
from coose.narrator import AuthorResult, NewCharacter, WitnessReport
from coose.web.app import create_app


class FakeNarrator:
    def __init__(self):
        self.calls = []
        self.new_characters = []
        self.present = []
        self.scenes = []

    def action(self, premise, scene, text, existing=None):
        self.calls.append(("action", scene.character.name, text, existing))
        self.scenes.append(scene)
        return AuthorResult(
            narration="You kick over the crate.", minutes_elapsed=1, location="", condition="",
            inventory_add=[], inventory_remove=[], learned=[], needs_met=[], alive=True,
            memory_add=["Mara kicked a crate at the gate."], new_characters=self.new_characters,
            present=self.present,
        )

    def question(self, premise, scene, text):
        return "No idea."

    def witnesses(self, premise, scene, text, result, others):
        self.calls.append(("witnesses", scene.character.name, sorted(o.name for o in others)))
        return [
            WitnessReport(name="jon", narration="A woman kicks a crate over by the gate.",
                          learned=["Someone at the gate is angry."]),
            WitnessReport(name="Nobody", narration="ignored", learned=[]),
        ]


@pytest.fixture
def narrator():
    return FakeNarrator()


@pytest.fixture
def app(config, conn, story_dir, narrator):
    for name in ("nil", "jon"):
        accounts.create_account(conn, name, "correct horse")
    stories.run_story(conn, config.stories_dir, "demo")
    return create_app(config, secret_key="test-secret", narrator=narrator)


def player(app, username, character, location="Drastead gate"):
    c = TestClient(app)
    c.post("/login", data={"username": username, "password": "correct horse"})
    c.post("/characters/new", data={
        "name": character, "description": "Someone.", "location": location,
        "item1": "", "item2": "", "item3": "", "item4": "", "visibility": "public",
    })
    return c


@pytest.fixture
def sconn(config, story_dir):
    c = stories.open_story_db(stories.find_story(config.stories_dir, "demo"))
    yield c
    c.close()


def chat(client):
    return html.unescape(client.get("/live").text)


def people(narrator) -> str:
    return "\n".join(narrator.scenes[-1].people)


def test_speech_goes_through_actions_and_the_narrator_knows_who_it_may_voice(app, narrator, sconn, config):
    mara, jon = player(app, "nil", "Mara"), player(app, "jon", "Jon")
    settings = stories.find_story(config.stories_dir, "demo").game_settings()
    game.create_ai_character(sconn, settings, name="Vendor", description="Sells bread.", location="Drastead gate")
    game.create_ai_character(sconn, settings, name="Far", description="Elsewhere.", location="Coast")
    mara.post("/prompt", data={"kind": "action", "text": 'I ask, "Any bread?"'})
    lines = people(narrator)
    assert "Jon | HERE with them" in lines and "player away: you may have them answer briefly" in lines
    assert "Vendor | HERE with them" in lines and "nobody plays them: you voice them" in lines
    assert "Far | at Coast" in lines
    with jon.websocket_connect("/ws"):
        mara.post("/prompt", data={"kind": "action", "text": "I wave."})
        assert "Jon | HERE with them | Healthy, no injuries | played by an online player" in people(narrator)


def test_freeze_mode_tells_the_narrator_to_keep_away_players_silent(app, narrator, story_dir):
    settings_file = story_dir / "Rulebook" / "Settings.yaml"
    settings_file.write_text(settings_file.read_text().replace(
        "absent_characters: speak_only", "absent_characters: freeze"))
    mara, jon = player(app, "nil", "Mara"), player(app, "jon", "Jon")
    mara.post("/prompt", data={"kind": "action", "text": "I say hello."})
    assert "player away: they stay silent and still" in people(narrator)


def test_people_the_narrator_says_are_here_show_as_here(app, narrator, sconn, config):
    mara = player(app, "nil", "Mara")
    settings = stories.find_story(config.stories_dir, "demo").game_settings()
    game.create_ai_character(sconn, settings, name="Dale", description="Gate guard.", location="Drastead main gate")
    assert "Dale" not in chat(mara)  # "Drastead main gate" isn't the same text as "Drastead gate"
    narrator.present = ["Dale"]
    mara.post("/prompt", data={"kind": "action", "text": "I walk up to the guard."})
    assert game.find_character_by_name(sconn, "Dale").location == "Drastead gate"
    assert "Dale" in chat(mara)


def test_witnesses_learn_what_they_saw(app, narrator, sconn):
    mara, jon = player(app, "nil", "Mara"), player(app, "jon", "Jon")
    mara.post("/prompt", data={"kind": "action", "text": "I kick the crate because I hate Jon."})
    assert narrator.calls[1] == ("witnesses", "Mara", ["Jon"])
    jon_chat = chat(jon)
    assert "A woman kicks a crate over" in jon_chat and "hate" not in jon_chat
    assert game.find_character_by_name(sconn, "Jon").knowledge == ["Someone at the gate is angry."]


def test_no_witness_pass_when_alone(app, narrator):
    mara = player(app, "nil", "Mara")
    mara.post("/prompt", data={"kind": "action", "text": "I wait."})
    assert [c[0] for c in narrator.calls] == ["action"]


def test_narrator_can_introduce_characters(app, narrator, sconn):
    narrator.new_characters = [
        NewCharacter(name="Old Pell", description="The gatekeeper.", location="Drastead gate"),
        NewCharacter(name="mara", description="A duplicate.", location="Drastead gate"),
    ]
    mara = player(app, "nil", "Mara")
    mara.post("/prompt", data={"kind": "action", "text": "I knock on the gatehouse."})
    assert narrator.calls[0][3] == ["Mara"]
    pell = game.find_character_by_name(sconn, "Old Pell")
    assert pell.owner is None and pell.visibility == "public"
    assert game.find_character_by_name(sconn, "Mara").description == "Someone."


def test_presence_shows_who_has_a_player_here(app):
    mara, jon = player(app, "nil", "Mara"), player(app, "jon", "Jon")
    assert "player away" in chat(mara)
    with jon.websocket_connect("/ws"):
        with mara.websocket_connect("/ws") as ws:
            assert "player here" in chat(mara)
            jon.post("/leave")
            assert ws.receive_text() == "update"


def test_socket_refused_without_a_character(app):
    c = TestClient(app)
    c.post("/login", data={"username": "nil", "password": "correct horse"})
    with pytest.raises(Exception):
        with c.websocket_connect("/ws") as ws:
            ws.receive_text()

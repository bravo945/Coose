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

    def action(self, premise, scene, text, existing=None):
        self.calls.append(("action", scene.character.name, text, existing))
        return AuthorResult(
            narration="You kick over the crate.", minutes_elapsed=1, location="", condition="",
            inventory_add=[], inventory_remove=[], learned=[], needs_met=[], alive=True,
            memory_add=["Mara kicked a crate at the gate."], new_characters=self.new_characters,
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

    def reply_as(self, premise, scene, speaker, text, mode):
        self.calls.append(("reply_as", scene.character.name, speaker, text, mode))
        return "Leave me alone."


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


def test_dialogue_reaches_an_online_player_without_the_narrator(app, narrator):
    mara, jon = player(app, "nil", "Mara"), player(app, "jon", "Jon")
    with jon.websocket_connect("/ws") as ws:
        mara.post("/prompt", data={"kind": "dialogue", "text": "Open the gate."})
        assert ws.receive_text() == "update"
        assert 'Mara says to you: "Open the gate."' in chat(jon)
    assert 'To Jon: "Open the gate."' in chat(mara)
    assert narrator.calls == []


def test_dialogue_to_an_away_player_gets_a_narrated_reply(app, narrator):
    mara, jon = player(app, "nil", "Mara"), player(app, "jon", "Jon")
    mara.post("/prompt", data={"kind": "dialogue", "text": "Open the gate."})
    assert narrator.calls == [("reply_as", "Jon", "Mara", "Open the gate.", "speak_only")]
    assert 'Jon says: "Leave me alone."' in chat(mara)
    assert "narrator answered Mara for you" in chat(jon)


def test_dialogue_only_to_chosen_people_at_the_same_place(app, narrator, sconn, config):
    mara, jon = player(app, "nil", "Mara"), player(app, "jon", "Jon")
    settings = stories.find_story(config.stories_dir, "demo").game_settings()
    far = game.create_ai_character(sconn, settings, name="Far", description="Elsewhere.", location="Coast")
    near = game.create_ai_character(sconn, settings, name="Vendor", description="Sells bread.",
                                    location="drastead gate")
    assert "Vendor" in chat(mara) and "Far" not in chat(mara)

    mara.post("/prompt", data={"kind": "dialogue", "text": "Bread?", "recipient": [str(near.id), str(far.id)]})
    assert [c[1] for c in narrator.calls] == ["Vendor"]
    assert narrator.calls[0][4] == "act_within_reason"  # AI characters always answer
    assert "Bread?" not in chat(jon)
    assert any("said to Vendor" in m for m in game.working_memory(sconn))


def test_freeze_mode_leaves_away_players_silent(app, narrator, story_dir):
    settings_file = story_dir / "Rulebook" / "Settings.yaml"
    settings_file.write_text(settings_file.read_text().replace(
        "absent_characters: speak_only", "absent_characters: freeze"))
    mara, jon = player(app, "nil", "Mara"), player(app, "jon", "Jon")
    mara.post("/prompt", data={"kind": "dialogue", "text": "Hello?"})
    assert narrator.calls == []
    assert "Jon seems distracted" in chat(mara)


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
    assert "narrator answers" in chat(mara)
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

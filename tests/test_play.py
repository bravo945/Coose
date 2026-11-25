import html

import pytest
from fastapi.testclient import TestClient

from coose import accounts, game, stories
from coose.narrator import AuthorResult, NarratorError, NewCharacter, QuestionAnswer
from coose.web.app import create_app


class FakeNarrator:
    def __init__(self):
        self.calls = []
        self.fail = False
        self.new_people = []

    def action(self, premise, scene, text, existing=None):
        self.calls.append(("action", scene, text))
        if self.fail:
            raise NarratorError("the AI service returned an error (529)")
        return AuthorResult(
            narration="You pry the door open. Cold air spills out.",
            minutes_elapsed=5, location="Warehouse", condition="", inventory_add=["crowbar"],
            inventory_remove=[], learned=["The warehouse is empty."], needs_met=[], alive=True,
            memory_add=["Nobody saw Mara enter the warehouse."], new_characters=[],
        )

    def question(self, premise, scene, text):
        self.calls.append(("question", scene, text))
        return QuestionAnswer(answer="You don't know who runs the town.", new_characters=self.new_people,
                              present=[p.name for p in self.new_people])

    def witnesses(self, premise, scene, text, result, others):
        self.calls.append(("witnesses", scene, text))
        return []


@pytest.fixture
def narrator():
    return FakeNarrator()


@pytest.fixture
def client(config, conn, story_dir, narrator):
    accounts.create_account(conn, "nil", "correct horse")
    stories.run_story(conn, config.stories_dir, "demo")
    c = TestClient(create_app(config, secret_key="test-secret", narrator=narrator))
    c.post("/login", data={"username": "nil", "password": "correct horse"})
    return c


def create(client, **overrides):
    data = {"name": "Mara", "description": "A scavenger from the coast.", "location": "Drastead gate",
            "item1": "knife", "item2": "", "item3": "", "item4": "", "visibility": "public"}
    data.update(overrides)
    return client.post("/characters/new", data=data, follow_redirects=False)


def test_new_player_is_sent_to_character_selection(client):
    r = client.get("/", follow_redirects=False)
    assert r.headers["location"] == "/characters"
    assert "Create a character" in client.get("/characters").text


def test_create_character_and_play(client, narrator, story_dir):
    r = create(client)
    assert r.status_code == 303
    page = client.get("/").text
    assert "Mara" in page and "Nothing has happened yet" in page

    client.post("/prompt", data={"kind": "action", "text": "I pry open the warehouse door."})
    page = client.get("/").text
    assert "I pry open the warehouse door." in page
    assert "Cold air spills out." in page
    kind, scene, text = narrator.calls[0]
    assert scene.character.name == "Mara" and scene.character.location == "Drastead gate"

    sheet = client.get("/?view=sheet").text
    assert "Warehouse" in sheet and "crowbar" in sheet

    client.post("/prompt", data={"kind": "question", "text": "Who runs the town?"})
    assert "You don't know who runs the town." in html.unescape(client.get("/").text)
    assert narrator.calls[1][1].character.knowledge == ["The warehouse is empty."]


def test_narrator_failure_changes_nothing(client, narrator, config):
    create(client)
    narrator.fail = True
    client.post("/prompt", data={"kind": "action", "text": "I run."})
    assert "couldn't respond" in html.unescape(client.get("/").text)
    sconn = stories.open_story_db(stories.find_story(config.stories_dir, "demo"))
    assert game.clock_minutes(sconn) == 0


def test_dialogue_is_no_longer_a_prompt_type(client, narrator):
    create(client)
    page = client.get("/").text
    assert 'value="dialogue"' not in page and "does or says" in page
    r = client.post("/prompt", data={"kind": "dialogue", "text": "Hello"})
    assert "isn't available" in html.unescape(r.text)
    assert narrator.calls == []


def test_a_question_can_add_people_as_characters(client, narrator, config):
    create(client)
    narrator.new_people = [NewCharacter(name="Front gate guard", description="Bundled up, long gun.",
                                        location="At the gate")]
    client.post("/prompt", data={"kind": "question", "text": "Can you add the guards to the people here?"})
    sconn = stories.open_story_db(stories.find_story(config.stories_dir, "demo"))
    guard = game.find_character_by_name(sconn, "Front gate guard")
    assert guard is not None and guard.owner is None
    assert guard.location == "Drastead gate"  # lined up with Mara's, so they show as here
    page = html.unescape(client.get("/").text)
    assert "Now tracked as characters: Front gate guard." in page and "narrator plays" in page


def test_invalid_character_shows_error(client):
    r = create(client, name="", description="")
    assert r.status_code == 400 and "Give your character a name" in r.text


def test_leave_and_rejoin(client):
    create(client)
    client.post("/leave")
    page = client.get("/characters").text
    assert "Play as Mara" in page
    client.post("/characters/1/join")
    assert "Leave character" in client.get("/").text


def test_dead_character_cannot_act(client, narrator, config):
    create(client)
    sconn = stories.open_story_db(stories.find_story(config.stories_dir, "demo"))
    with sconn:
        sconn.execute("UPDATE characters SET alive = 0")
    client.post("/prompt", data={"kind": "action", "text": "I stand up."})
    assert narrator.calls == []
    assert "has died" in client.get("/").text

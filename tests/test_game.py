import pytest

from coose import game, stories


@pytest.fixture
def story(config, story_dir):
    (story_dir / "Rulebook" / "Settings.yaml").write_text(
        'display_name: "Demo"\nstart_time: "2035-03-01 06:00"\nplayers_per_character: 2\n'
        "tracked_needs:\n  food: 10\n",
        encoding="utf-8",
    )
    return stories.load_story(story_dir)


@pytest.fixture
def sconn(story):
    c = stories.open_story_db(story)
    yield c
    c.close()


def make(sconn, story, name="Mara", owner="nil", visibility="public", items=("knife",)):
    return game.create_character(
        sconn, story.game_settings(), name=name, description="A scavenger.",
        location="Drastead", items=list(items), visibility=visibility, owner=owner,
    )


def test_create_character_validation(sconn, story):
    c = make(sconn, story)
    assert c.inventory == ["knife"] and c.needs == {"food": 0}
    with pytest.raises(game.GameError):
        make(sconn, story)  # duplicate name
    with pytest.raises(game.GameError):
        make(sconn, story, name="Jon", items=["a", "b", "c", "d", "e"])
    with pytest.raises(game.GameError):
        game.create_character(sconn, story.game_settings(), name="Big", description="word " * 301,
                              location="", items=[], visibility="public", owner="nil")


def test_locked_characters_and_player_limit(sconn, story):
    settings = story.game_settings()
    locked = make(sconn, story, name="Ana", owner="ana", visibility="locked")
    public = make(sconn, story, name="Jon")
    assert [c.name for c in game.playable_characters(sconn, "nil")] == ["Jon"]
    assert {c.name for c in game.playable_characters(sconn, "ANA")} == {"Ana", "Jon"}
    with pytest.raises(game.GameError):
        game.join(sconn, settings, "nil", locked.id)

    game.join(sconn, settings, "nil", public.id)
    with pytest.raises(game.GameError):
        game.join(sconn, settings, "nil", public.id)  # already playing
    game.join(sconn, settings, "ana", public.id)
    with pytest.raises(game.GameError):
        game.join(sconn, settings, "bo", public.id)  # limit of 2
    game.leave(sconn, "nil")
    assert game.current_character(sconn, "nil") is None
    game.join(sconn, settings, "bo", public.id)


def test_apply_action_updates_character_clock_and_memory(sconn, story):
    settings = story.game_settings()
    c = make(sconn, story, items=["knife", "Bread"])
    assert game.world_time(sconn, settings).isoformat() == "2035-03-01T06:00:00"
    c = game.apply_action(sconn, c, game.ActionOutcome(
        narration="You eat.", minutes_elapsed=90, location="The market",
        inventory_add=["coin"], inventory_remove=["bread"], learned=["Bread is scarce."],
        memory_add=["A vendor saw Mara eat."],
    ))
    assert c.location == "The market"
    assert c.inventory == ["knife", "coin"]
    assert c.knowledge == ["Bread is scarce."]
    assert c.needs == {"food": 90}
    assert game.world_time(sconn, settings).isoformat() == "2035-03-01T07:30:00"
    assert game.working_memory(sconn) == ["A vendor saw Mara eat."]
    c = game.apply_action(sconn, c, game.ActionOutcome(narration="", minutes_elapsed=30, needs_met=["Food"]))
    assert c.needs == {"food": 0}


def test_need_levels_are_words(sconn, story):
    settings = story.game_settings()
    c = make(sconn, story)
    assert game.need_levels(c, settings) == {"food": "fine"}
    c = game.apply_action(sconn, c, game.ActionOutcome(narration="", minutes_elapsed=8 * 60))
    assert game.need_levels(c, settings) == {"food": "urgent"}
    c = game.apply_action(sconn, c, game.ActionOutcome(narration="", minutes_elapsed=3 * 60))
    assert game.need_levels(c, settings) == {"food": "run out"}


def test_world_state_seeded_from_file(sconn, story_dir):
    assert game.world_state(sconn).startswith("# World State")


def test_bad_start_time_is_a_story_problem(story_dir):
    (story_dir / "Rulebook" / "Settings.yaml").write_text('start_time: "soon"\n')
    assert not stories.load_story(story_dir).valid

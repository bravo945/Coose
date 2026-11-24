import shutil

import pytest

from coose import stories


def test_template_is_a_valid_story(config, story_dir):
    s = stories.load_story(story_dir)
    assert s.valid, s.problems
    assert s.display_name == "My Story"


def test_missing_folders_are_reported(config, story_dir):
    shutil.rmtree(story_dir / "Rulebook")
    s = stories.load_story(story_dir)
    assert not s.valid
    assert any("Rulebook" in p for p in s.problems)


def test_bad_settings_are_reported(config, story_dir):
    (story_dir / "Rulebook" / "Settings.yaml").write_text("- just\n- a list\n")
    assert not stories.load_story(story_dir).valid


def test_run_pause_resume(conn, config, story_dir):
    assert stories.status(conn).story is None
    stories.run_story(conn, config.stories_dir, "demo")
    assert stories.status(conn).online
    assert (story_dir / ".coose" / "story.db").exists()
    stories.pause(conn)
    st = stories.status(conn)
    assert st.story == "demo" and st.paused and not st.online
    stories.resume(conn)
    assert stories.status(conn).online


def test_cannot_run_missing_or_invalid_story(conn, config, story_dir):
    with pytest.raises(stories.StoryError):
        stories.run_story(conn, config.stories_dir, "nope")
    with pytest.raises(stories.StoryError):
        stories.run_story(conn, config.stories_dir, "../demo")
    shutil.rmtree(story_dir / "Characters")
    with pytest.raises(stories.StoryError):
        stories.run_story(conn, config.stories_dir, "demo")


def test_pause_without_story(conn):
    with pytest.raises(stories.StoryError):
        stories.pause(conn)

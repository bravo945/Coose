"""Per-task AI models and effort, and the owner panel's console."""

import html

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from coose import aisettings
from coose.cli import app as cli
from coose.config import Config
from coose.web.panel import create_panel


def test_defaults_come_from_coose_toml(config):
    toml = config.data_dir.parent / "coose.toml"
    toml.write_text(toml.read_text() + '\n[ai]\nmodel = "claude-opus-5-5"\neffort = "high"\naction_passes = 1\n'
                    '[ai.tasks.witnesses]\nmodel = "claude-haiku-5-5"\neffort = "low"\n')
    p = Config.load(toml).ai_defaults()
    assert p.tasks["action"] == aisettings.TaskSettings("claude-opus-5-5", "high", "on")
    assert p.tasks["witnesses"] == aisettings.TaskSettings("claude-haiku-5-5", "low", "on")
    assert p.action_passes == 1


def test_story_settings_override_the_defaults(conn):
    base = aisettings.defaults("claude-opus-5-5", "medium")
    aisettings.set_task(conn, "demo", "question", base, model="claude-sonnet-5-5", effort="low", thinking="off")
    aisettings.set_action_passes(conn, "demo", 1)
    p = aisettings.profile(conn, "demo", base)
    assert p.tasks["question"].describe() == "claude-sonnet-5-5, low effort, thinking off"
    assert aisettings.thinking_param(p.tasks["question"]) == {"type": "between_tools"}
    assert p.tasks["action"].model == "claude-opus-5-5" and p.action_passes == 1
    assert aisettings.profile(conn, "other", base) == base
    aisettings.reset(conn, "demo")
    assert aisettings.profile(conn, "demo", base) == base


def test_settings_that_the_api_would_refuse_are_rejected(conn):
    base = aisettings.defaults("claude-opus-5-5", "medium")
    with pytest.raises(aisettings.SettingError, match="always thinks"):
        aisettings.set_task(conn, "demo", "action", base, thinking="off")
    with pytest.raises(aisettings.SettingError, match="high effort or below"):
        aisettings.set_task(conn, "demo", "witnesses", base, model="claude-haiku-5-5", effort="max", thinking="off")
    with pytest.raises(aisettings.SettingError, match="Unknown model"):
        aisettings.set_task(conn, "demo", "action", base, model="gpt-5")
    with pytest.raises(aisettings.SettingError, match="Unknown task"):
        aisettings.set_task(conn, "demo", "dialogue", base, effort="low")


def test_ai_commands_on_the_terminal(config, story_dir):
    toml = str(config.data_dir.parent / "coose.toml")
    run = lambda *args: CliRunner().invoke(cli, ["--config", toml, *args])
    out = run("ai", "set", "demo", "all", "--model", "claude-sonnet-5-5", "--effort", "low").output
    assert "all: claude-sonnet-5-5, low effort." in out
    assert run("ai", "passes", "demo", "1").exit_code == 0
    shown = run("ai", "show", "demo").output
    assert "witnesses  claude-sonnet-5-5, low effort" in shown and "one combined pass" in shown
    assert run("ai", "set", "demo", "action").exit_code == 1
    run("ai", "reset", "demo")
    assert "claude-opus-5-5, medium effort" in run("ai", "show", "demo").output


@pytest.fixture
def panel(config, story_dir):
    c = TestClient(create_panel(config, password="panel-pass", secret_key="s"))
    c.post("/login", data={"password": "panel-pass"})
    return c


def test_panel_console_runs_coose_commands(panel):
    page = html.unescape(panel.post("/console", data={"command": "coose stories"}).text)
    assert "> coose stories" in page and "demo  (ok)" in page
    page = html.unescape(panel.post("/console", data={"command": "account add ray --password correct-horse"}).text)
    assert "Created account 'ray'." in page and "correct-horse" not in page and "--password ******" in page
    assert "can't run from the panel" in html.unescape(panel.post("/console", data={"command": "serve"}).text)
    page = html.unescape(panel.post("/console", data={"command": "rollback demo 1"}).text)
    assert "can't answer prompts" in page
    page = html.unescape(panel.post("/console", data={"command": "ai set demo witnesses --effort low"}).text)
    assert "witnesses: claude-opus-5-5, low effort." in page
    assert "> coose" not in html.unescape(panel.post("/console/clear").text)


def test_console_needs_the_panel_password(config, story_dir):
    c = TestClient(create_panel(config, password="panel-pass", secret_key="s"))
    assert c.post("/console", data={"command": "status"}, follow_redirects=False).headers["location"] == "/login"


def test_panel_ai_settings(panel, conn):
    base = aisettings.defaults("claude-opus-5-5", "medium")
    page = html.unescape(panel.post("/ai", data={"name": "demo", "task": "question", "model": "claude-haiku-5-5",
                                                  "effort": "low", "thinking": "off"}).text)
    assert "question: claude-haiku-5-5, low effort, thinking off." in page
    assert aisettings.profile(conn, "demo", base).tasks["question"].model == "claude-haiku-5-5"
    page = html.unescape(panel.post("/ai", data={"name": "demo", "task": "action", "model": "claude-opus-5-5",
                                                  "effort": "low", "thinking": "off"}).text)
    assert "always thinks" in page
    panel.post("/ai/passes", data={"name": "demo", "passes": "1"})
    assert aisettings.profile(conn, "demo", base).action_passes == 1
    panel.post("/ai/reset", data={"name": "demo"})
    assert aisettings.profile(conn, "demo", base) == base

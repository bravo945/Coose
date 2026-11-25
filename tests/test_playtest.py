import json

import httpx2
import pytest
from typer.testing import CliRunner

from coose import db, playtest
from coose.cli import app as cli
from coose.narrator import ClaudeNarrator
from test_rounds import FakeNarrator

SCENARIO = """
settings:
  actions_per_round: 2
players:
  - user: ana
    character: {name: Mara, description: A scavenger., location: Gate, items: [knife]}
  - user: ben
    online: false
    character: {name: Jon, description: A guard., location: Gate}
steps:
  - ana: "! I look around."
  - ana: '"" Hello, Jon.'
  - ana: "? Where am I?"
  - ben: "! I wave."
  - vote: [ana]
"""


def test_scenario_validation(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text("players: [{user: a, character: {name: A}}]\nsteps: [{a: 'no prefix'}]\n")
    with pytest.raises(playtest.ScenarioError, match="Start each prompt"):
        playtest.Scenario.load(bad)
    bad.write_text("players: [{user: a, character: {name: A}}]\nsteps: [{zed: '! hi'}]\n")
    with pytest.raises(playtest.ScenarioError, match="unknown player"):
        playtest.Scenario.load(bad)
    assert playtest.parse_prompt('"" hi there') == ("dialogue", "hi there")


def test_playtest_runs_on_a_scratch_copy(config, story_dir, tmp_path):
    path = tmp_path / "scenario.yaml"
    path.write_text(SCENARIO)
    out = tmp_path / "transcript.md"
    narrator = FakeNarrator()
    playtest.run(config, "demo", playtest.Scenario.load(path), out, narrator=narrator, log=lambda *_: None)
    text = out.read_text()
    assert "Players: ana as Mara, ben as Jon (away)" in text
    assert "You do it: I look around." in text
    assert 'Mara says to you: "Hello, Jon."' in text
    assert "## Step 4: ben (action): I wave." in text
    assert "round 2" in text and "Logbook: Round 1.md" in text
    assert narrator.calls.count("round") == 2  # two Actions, then a vote by the only online player
    assert not (story_dir / ".coose").exists() or not list((story_dir / "Logbook").glob("Round [0-9]*.md"))


def test_api_usage_is_recorded_and_summarised(config, conn):
    def handler(request):
        return httpx2.Response(200, json={
            "id": "m", "type": "message", "role": "assistant", "model": "claude-opus-5-5",
            "content": [{"type": "text", "text": "You don't know."}], "stop_reason": "end_turn",
            "stop_sequence": None,
            "usage": {"input_tokens": 120, "output_tokens": 30, "cache_read_input_tokens": 9000,
                      "cache_creation_input_tokens": 0},
        })

    import anthropic
    from datetime import datetime

    from coose import game
    from coose.narrator import Scene

    records = []
    n = ClaudeNarrator("claude-opus-5-5", "medium", "NONE", usage_log=records.append)
    n._client = anthropic.Anthropic(api_key="k", http_client=httpx2.Client(transport=httpx2.MockTransport(handler)))
    c = game.Character(1, "Mara", "d", "Gate", "ok", [], [], {}, True, "public", "nil")
    n.question("Premise", Scene("S", datetime(2035, 1, 1), "", [], c, {}, []), "Who?")
    assert records[0]["pass"] == "question" and records[0]["cache_read_tokens"] == 9000

    db.log_usage(conn, "demo", records[0])
    out = CliRunner().invoke(cli, ["--config", str(config.data_dir.parent / "coose.toml"), "usage"]).output
    assert "question" in out and "9000" in out
    # 120 input at $4, 30 output at $20, 9000 cache reads at $0.20 per million tokens
    assert records[0]["cache_ttl"] == "1h"
    assert "Estimated total: $0.00" in out
    row = conn.execute("SELECT cost_usd FROM ai_usage").fetchone()
    assert abs(row["cost_usd"] - (120 * 4 + 30 * 20 + 9000 * 0.2) / 1e6) < 1e-9


def test_cost_estimates():
    from coose import pricing

    assert pricing.estimate("claude-opus-5-5", 0, 0, 0, 1_000_000, "5m") == 5.0
    assert pricing.estimate("claude-opus-5-5", 0, 0, 0, 1_000_000, "1h") == 8.0
    assert pricing.estimate("some-future-model", 1, 1, 1, 1) is None

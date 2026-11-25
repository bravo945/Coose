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
    assert playtest.parse_prompt('"" hi there') == ("action", 'I say, "hi there"')


def test_playtest_runs_on_a_scratch_copy(config, story_dir, tmp_path):
    path = tmp_path / "scenario.yaml"
    path.write_text(SCENARIO)
    out = tmp_path / "transcript.md"
    narrator = FakeNarrator()
    playtest.run(config, "demo", playtest.Scenario.load(path), out, narrator=narrator, log=lambda *_: None)
    text = out.read_text()
    assert "Players: ana as Mara, ben as Jon (away)" in text
    assert "You do it: I look around." in text
    assert 'You do it: I say, "Hello, Jon."' in text
    assert "## Step 4: ben (action): I wave." in text
    assert "round 2" in text and "Logbook: Round 1.md" in text
    assert narrator.calls.count("round") == 2  # two Actions, then a vote by the only online player
    assert not (story_dir / ".coose").exists() or not list((story_dir / "Logbook").glob("Round [0-9]*.md"))


PARTS = ("responder", "author", "witnesses", "answer", "round", "backfill", "intervention")


def answer_json(text: str) -> str:
    out = {p: None for p in PARTS}
    out["answer"] = {"answer": text, "new_characters": [], "present": []}
    return json.dumps(out)


def test_api_usage_is_recorded_and_summarised(config, conn):
    def handler(request):
        return httpx2.Response(200, json={
            "id": "m", "type": "message", "role": "assistant", "model": "claude-opus-5-5",
            "content": [{"type": "text", "text": answer_json("You don't know.")}], "stop_reason": "end_turn",
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


def fake_api(replies: list[dict], sent: list[dict]):
    """A Claude API stand-in: records each request body and answers with the next reply."""
    import anthropic

    def handler(request):
        sent.append(json.loads(request.content))
        out = {p: None for p in PARTS}
        out.update(replies[len(sent) - 1])
        return httpx2.Response(200, json={
            "id": "m", "type": "message", "role": "assistant", "model": sent[-1]["model"],
            "content": [{"type": "text", "text": json.dumps(out)}], "stop_reason": "end_turn", "stop_sequence": None,
            "usage": {"input_tokens": 10, "output_tokens": 10, "cache_read_input_tokens": 0,
                      "cache_creation_input_tokens": 0},
        })

    return anthropic.Anthropic(api_key="k", http_client=httpx2.Client(transport=httpx2.MockTransport(handler)))


AUTHOR = {"narration": "You knock.", "minutes_elapsed": 1, "location": "", "condition": "", "inventory_add": [],
          "inventory_remove": [], "learned": [], "needs_met": [], "alive": True, "memory_add": [],
          "new_characters": [], "present": [], "parts": []}


def scene():
    from datetime import datetime

    from coose import game
    from coose.narrator import Scene

    c = game.Character(1, "Mara", "d", "Gate", "ok", [], [], {}, True, "public", "nil")
    return Scene("S", datetime(2035, 1, 1), "", [], c, {}, [], people=["- Dale | HERE with them | ok | ..."])


def test_every_pass_shares_one_cached_prefix():
    """The token bill from the first server test: each pass had its own schema, so each paid its own
    cache write of the whole Premise. Now every pass sends the same schema and system prompt."""
    from coose.narrator import ADDITIONS_MARK

    sent = []
    n = ClaudeNarrator("claude-opus-5-5", "medium", "NONE")
    n._client = fake_api([{"responder": {"outcome": "success", "reasoning": "r", "draft": "d"}},
                          {"author": AUTHOR}, {"answer": {"answer": "No.", "new_characters": [], "present": []}}],
                         sent)
    premise = "## Premise/World.md (Premise)\nA world." + ADDITIONS_MARK + "## Premise/AI Additions/Gate.md\nNew."
    result = n.action(premise, scene(), "I knock.")
    n.question(premise, scene(), "Who is here?")
    assert result.narration == "You knock."
    assert [r["system"] for r in sent[1:]] == [sent[0]["system"]] * 2
    assert all("format" not in r["output_config"] for r in sent)  # too large for the API to compile
    assert '"author"' in sent[0]["system"][0]["text"]  # the shared schema, inside the cached prefix
    system = sent[0]["system"]
    assert system[1]["cache_control"]["ttl"] == "1h" and "A world." in system[1]["text"]
    assert "New." not in system[1]["text"] and "New." in system[2]["text"]  # lore cached apart
    # The scene is cached for the Author to read; it is the same text in both passes.
    first, second = sent[0]["messages"][0]["content"][0], sent[1]["messages"][0]["content"][0]
    assert first == second and first["cache_control"]["ttl"] == "5m" and "Dale | HERE" in first["text"]


def test_models_and_effort_follow_the_story_settings():
    from coose import aisettings

    sent = []
    base = aisettings.defaults("claude-opus-5-5", "medium")
    chosen = aisettings.Profile({**base.tasks, "question": aisettings.TaskSettings("claude-haiku-5-5", "low", "off")},
                                action_passes=1)
    n = ClaudeNarrator("claude-opus-5-5", "medium", "NONE", profile=lambda: chosen)
    n._client = fake_api([{"author": AUTHOR}, {"answer": {"answer": "No.", "new_characters": [], "present": []}}],
                         sent)
    n.action("P", scene(), "I knock.")
    n.question("P", scene(), "Who?")
    assert len(sent) == 2  # one combined pass for the Action
    assert sent[0]["model"] == "claude-opus-5-5" and sent[0]["output_config"]["effort"] == "medium"
    assert sent[0]["fallbacks"] == "default" and "thinking" not in sent[0]
    assert sent[1]["model"] == "claude-haiku-5-5" and sent[1]["output_config"]["effort"] == "low"
    assert sent[1]["thinking"] == {"type": "disabled"} and "fallbacks" not in sent[1]


def test_a_refused_request_says_why(caplog):
    import anthropic

    from coose.narrator import NarratorError

    def handler(request):
        return httpx2.Response(400, json={"type": "error", "error": {
            "type": "invalid_request_error", "message": "thinking: something this model doesn't accept"}})

    n = ClaudeNarrator("claude-opus-5-5", "medium", "NONE")
    n._client = anthropic.Anthropic(api_key="k", max_retries=0,
                                    http_client=httpx2.Client(transport=httpx2.MockTransport(handler)))
    with pytest.raises(NarratorError) as e:
        n.question("P", scene(), "Who?")
    assert "something this model doesn't accept" in str(e.value) and "claude-opus-5-5" in str(e.value)
    assert "something this model doesn't accept" in caplog.text


def test_an_unreadable_reply_gets_one_retry():
    import anthropic

    sent, replies = [], ["Sure! Here you go: {not json", json.dumps({"answer": {"answer": "No.", "new_characters": []}})]

    def handler(request):
        sent.append(json.loads(request.content))
        return httpx2.Response(200, json={
            "id": "m", "type": "message", "role": "assistant", "model": "claude-opus-5-5",
            "content": [{"type": "text", "text": replies[len(sent) - 1]}], "stop_reason": "end_turn",
            "stop_sequence": None, "usage": {"input_tokens": 1, "output_tokens": 1}})

    n = ClaudeNarrator("claude-opus-5-5", "medium", "NONE")
    n._client = anthropic.Anthropic(api_key="k", http_client=httpx2.Client(transport=httpx2.MockTransport(handler)))
    assert n.question("P", scene(), "Who?").answer == "No."
    retry = sent[1]["messages"]
    assert retry[1] == {"role": "assistant", "content": "Sure! Here you go: {not json"}
    assert "couldn't be read" in retry[2]["content"] and sent[1]["system"] == sent[0]["system"]

"""The AI side of Coose: turning a character's prompt into what happens.

An Action takes two passes. The Responder judges whether and how the action
can happen (Rulebook, Premise mechanics, the character's capability). The
Author then decides the objective consequences and writes the narration the
acting player sees. (A story can merge the two into one cheaper pass.) Speech
is part of an Action: the Author voices the people who answer, within what
their players allow. When other characters exist, another pass judges who
witnessed the Action and what each of them perceived. A Question takes one
pass, answered only from what the character could know. A round change is
one pass over the whole world (see rounds.py), and a character returning
after a skip they sat out gets a backstory pass.

Cost: the story's Premise and Rulebook go first in every request as a cached
system prompt, and every pass replies in one shared JSON shape whose schema
is written into that system prompt, so they all share that one cache.
(Structured-output schemas are part of the cached prefix, so a schema per
pass meant a cache per pass, and one schema for everything is too large for
the API to compile.) Lore the AI adds goes after the
Premise behind its own cache breakpoint, so a new addition doesn't rewrite
the whole Premise. The scene (character, chat, world state) is cached for a
few minutes too, so the Author and witness passes read it instead of paying
for it again. Which model runs each task, and how hard it thinks, comes from
the story's AI settings (aisettings.py).
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Literal, Protocol

from pydantic import BaseModel, Field, ValidationError

from . import aisettings, game

log = logging.getLogger(__name__)

CORE_RULES = """\
You are the narrator of a Coose game: a multiplayer roleplay set in the world described below.

The governing rule: subjective roleplay in an objective world. You know the objective truth of the \
world. Players never do. Everything a player reads must come through their own character's senses \
and knowledge. Never reveal facts their character has no way of perceiving or knowing, never \
summarise the Premise to them, and never describe events elsewhere that their character cannot \
witness. It is fine, and often right, to say their character doesn't know something.

Judge every action by realism inside this world and by the Rulebook. Outcomes can be success, \
partial success, failure with a consequence, or impossible; a failed or impossible action still \
gets a reply that moves the story on rather than a flat refusal.

A collective character is a group played as one character, such as a militia or a town. It can split \
into parts in different places. Each part perceives only what is around it, and knowledge moves between \
parts only by realistic means (a runner, a radio, a signal). Catch and refuse anything that relies on \
unrealistic communication between parts. A collective dies only when its members are lost or reduced to \
one individual.

Write narration in the second person, present tense, addressed to the acting character. Keep it \
vivid but tight: a few short paragraphs at most. Do not invent the player character's own words, \
feelings or decisions beyond what the action states.

Players speak through Actions: anything their character says is part of what they attempt. Other people \
answer in the narration. You voice people nobody plays freely. For a character someone plays, follow the \
note beside them under "People": never write words or decisions for a character whose player is online, and \
for one whose player is away keep to what the note allows.

Characters are how the game keeps track of people. When a person who is likely to matter again enters the \
scene, add them as a new character, even if nobody knows their name yet: call them what the acting character \
would, such as "Front gate guard" or "The lookout". Never add someone who is already listed under People.
"""

PremiseText = str


# --- Structured outputs ------------------------------------------------------

class Part(BaseModel):
    name: str = Field(description="What this part of the collective is called, e.g. 'North patrol'.")
    location: str = Field(description="Where this part is.")
    members: str = Field(description="Who or how many are in it, briefly, e.g. '12 fighters'.")


class LoreEntry(BaseModel):
    title: str = Field(description="A short title for the piece of lore, e.g. 'Drastead gate procedure'.")
    text: str = Field(description="The lore itself, objective and concise, in Markdown.")


PARTS_FIELD = ("Only for a collective character: all of its parts after this, if they split, merge, move or "
               "lose members. Empty for anyone else, or if nothing changed.")
PRESENT_FIELD = ("Names, exactly as listed under People or as added in new_characters, of everyone who is right "
                 "there with the acting character afterwards, close enough to talk to. Empty if they are alone.")
LORE_FIELD = ("Lasting facts about the world you invented that the Premise doesn't already state and that would "
              "matter beyond this moment: places, groups, history, customs. Not events or character details. "
              "Usually empty.")


class ResponderDraft(BaseModel):
    outcome: Literal["success", "partial", "failure", "impossible"] = Field(
        description="How the attempt goes, judged by realism, the Rulebook and the character's capability."
    )
    reasoning: str = Field(description="Brief objective reasoning for the outcome. Never shown to players.")
    draft: str = Field(description="An objective account of what actually happens.")


class NewCharacter(BaseModel):
    name: str = Field(description="Their name as people in the world would say it, or, if nobody knows it yet, "
                      "a short description the acting character would use, e.g. 'Front gate guard'.")
    description: str = Field(description="Who they are: appearance, role, manner. A few sentences.")
    location: str = Field(description="Where they are now.")


class AuthorResult(BaseModel):
    narration: str = Field(
        description="What the acting character perceives, second person, present tense. Only what they could notice."
    )
    minutes_elapsed: int = Field(
        description="In-game minutes the whole action takes as the player stated it, 0 for something instant. "
        "A journey or long task counts in full (a walk of several hours is several hours), even if the "
        "narration only covers its start; long actions become a time skip."
    )
    location: str = Field(description="The character's location afterwards, or empty if unchanged.")
    condition: str = Field(description="The character's new condition, or empty if unchanged.")
    inventory_add: list[str] = Field(description="Items the character gains.")
    inventory_remove: list[str] = Field(description="Items the character loses or uses up, by exact name.")
    learned: list[str] = Field(description="New facts the character now knows, each a short sentence.")
    needs_met: list[str] = Field(description="Names of tracked needs this action satisfied.")
    alive: bool = Field(description="False only if the character died.")
    memory_add: list[str] = Field(
        description="Short objective notes for the narrator's working memory: witnesses, changes to the "
        "world, details to keep consistent this round."
    )
    new_characters: list[NewCharacter] = Field(
        description="People this action brings into the story who are likely to matter again (named or not), so "
        "they can be tracked and played. Leave empty for passing crowds and for anyone listed under People."
    )
    present: list[str] = Field(
        default_factory=list,
        description=PRESENT_FIELD,
    )
    parts: list[Part] = Field(default_factory=list, description=PARTS_FIELD)


class WitnessReport(BaseModel):
    name: str = Field(description="The witness's name, exactly as listed.")
    narration: str = Field(
        description="What this witness perceives, second person, present tense, only through their own senses."
    )
    learned: list[str] = Field(description="New facts the witness now knows, each a short sentence.")


class WitnessPass(BaseModel):
    reports: list[WitnessReport] = Field(
        description="One report for each listed character who would realistically notice any part of the "
        "action. Empty if nobody would."
    )


class CharacterRoundUpdate(BaseModel):
    name: str = Field(description="The character's name, exactly as listed.")
    notice: str = Field(
        description="A short in-character notice for this character's player: only what they noticed changing "
        "since the last round, second person, one to three sentences. Empty if they noticed nothing. For an "
        "offline character the narrator plays through a skip, say briefly what they spent the time doing."
    )
    condition: str = Field(description="Their condition now, or empty if unchanged.")
    location: str = Field(description="Their location now, or empty if unchanged.")
    learned: list[str] = Field(description="New facts they now know, each a short sentence.")
    alive: bool = Field(description="False only if they died.")
    parts: list[Part] = Field(default_factory=list, description=PARTS_FIELD)


class RoundResult(BaseModel):
    title: str = Field(description="A short title for the round that just ended, a few words.")
    summary: str = Field(description="An objective summary of the round for the Logbook. A short paragraph.")
    relevant_characters: str = Field(description="The characters who mattered this round and their status.")
    deaths: str = Field(description="Who died this round and how, or empty if nobody did.")
    world_state: str = Field(
        description="The complete new World State in Markdown: the objective condition of the world now, "
        "replacing the old one. Keep everything still true; fold in lasting changes from the working memory."
    )
    working_memory: list[str] = Field(
        description="The working memory notes still relevant for the next round. Drop what no longer matters "
        "and anything now recorded in the World State or a character."
    )
    characters: list[CharacterRoundUpdate] = Field(description="One entry per living character listed.")
    lore: list[LoreEntry] = Field(default_factory=list, description=LORE_FIELD)


class BackfillResult(BaseModel):
    narration: str = Field(
        description="What the character did and went through while their player was away, second person, past "
        "tense, a short paragraph."
    )
    location: str = Field(description="Where they are now, or empty if unchanged.")
    condition: str = Field(description="Their condition now, or empty if unchanged.")
    learned: list[str] = Field(description="New facts they now know, each a short sentence.")


class CharacterChange(BaseModel):
    name: str = Field(description="The character's name, exactly as listed.")
    notice: str = Field(
        description="What this character perceives of the change, second person, present tense, only through "
        "their own senses. Empty if they notice nothing or the change is invisible to them."
    )
    condition: str = Field(description="Their condition now, or empty if unchanged.")
    location: str = Field(description="Their location now, or empty if unchanged.")
    inventory_add: list[str] = Field(description="Items they gain.")
    inventory_remove: list[str] = Field(description="Items they lose, by exact name.")
    learned: list[str] = Field(description="New facts they now know, each a short sentence.")
    alive: bool = Field(description="False only if they are now dead.")
    parts: list[Part] = Field(default_factory=list, description=PARTS_FIELD)


class InterventionResult(BaseModel):
    reply: str = Field(description="A short objective report to the story admin of what you changed and why.")
    world_state: str = Field(
        description="The complete new World State in Markdown if it changes, or empty to leave it as it is."
    )
    memory_add: list[str] = Field(description="Objective notes for the working memory about the change.")
    characters: list[CharacterChange] = Field(description="Only the characters this changes.")
    lore: list[LoreEntry] = Field(default_factory=list, description=LORE_FIELD)


class QuestionAnswer(BaseModel):
    answer: str = Field(description="The answer for the player, brief, out of character.")
    new_characters: list[NewCharacter] = Field(
        description="Only if the player asks for people their character can perceive right now to be tracked as "
        "characters, or refers to such a person who isn't listed under People yet: those people. Usually empty."
    )
    present: list[str] = Field(default_factory=list, description=PRESENT_FIELD + " Only fill this in together "
                               "with new_characters; otherwise leave it empty.")


class NarratorOutput(BaseModel):
    """Every pass answers with this one shape and fills in only its own part.

    One shared shape keeps one shared prompt cache: its schema is written
    once into the cached system prompt (OUTPUT_RULES), the same for every
    pass. Per-pass structured-output schemas each meant their own cache."""

    responder: ResponderDraft | None = Field(default=None, description="Only for the Responder pass.")
    author: AuthorResult | None = Field(default=None, description="Only for the Author pass.")
    witnesses: WitnessPass | None = Field(default=None, description="Only for the witness pass.")
    answer: QuestionAnswer | None = Field(default=None, description="Only for a Question.")
    round: RoundResult | None = Field(default=None, description="Only for a round change.")
    backfill: BackfillResult | None = Field(default=None, description="Only for a backstory after a skip.")
    intervention: InterventionResult | None = Field(default=None, description="Only for a story admin command.")


def _compact_schema() -> str:
    def strip(node):
        if isinstance(node, dict):
            return {k: strip(v) for k, v in node.items() if k != "title"}
        if isinstance(node, list):
            return [strip(v) for v in node]
        return node
    return json.dumps(strip(NarratorOutput.model_json_schema()), separators=(",", ":"))


OUTPUT_RULES = (
    "\n\n# How to reply\nEvery reply is one JSON object and nothing else: no Markdown fences, no text before or "
    "after it. It follows this JSON Schema. Each request names the one part to fill in; leave every other part "
    "null. Give every field of that part, using empty strings or empty lists where nothing applies.\n"
    + _compact_schema() + "\n"
)


def read_output(text: str) -> NarratorOutput:
    """The JSON object in a reply, read against the shared shape."""
    text = (text or "").strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("there is no JSON object in the reply")
    return NarratorOutput.model_validate_json(text[start:end + 1])


# --- Context ------------------------------------------------------------------

@dataclass
class Scene:
    """Everything that changes from prompt to prompt."""

    story: str
    world_time: datetime
    world_state: str
    working_memory: list[str]
    character: game.Character
    need_levels: dict[str, str]
    recent: list[game.Message]
    round_number: int = 1
    people: list[str] = field(default_factory=list)  # one line per other character; see describe_people

    def render(self) -> str:
        c = self.character
        lines = [
            f"# Current moment\nIn-game time: {self.world_time:%A %d %B %Y, %H:%M} (round {self.round_number})",
            f"# World state (objective, never shown to players)\n{self.world_state.strip() or '(not yet written)'}",
            "# Working memory (objective notes for this round)\n"
            + ("\n".join(f"- {m}" for m in self.working_memory) or "(empty)"),
            "# The acting character",
            f"Name: {c.name}",
            f"Location: {c.location or 'unspecified'}",
            f"Condition: {c.condition}",
            f"Inventory: {', '.join(c.inventory) or 'nothing'}",
            f"Description: {c.description}",
            *([f"This is a collective character. Parts: {game.describe_parts(c.parts) or 'all together, as one body'}"]
              if c.collective else []),
            "What they know:\n" + ("\n".join(f"- {k}" for k in c.knowledge) or "- nothing beyond their description"),
        ]
        if self.need_levels:
            lines.append(
                "Hidden needs (never state these words or any numbers to the player; let them show through "
                "how the character feels):\n" + "\n".join(f"- {n}: {lvl}" for n, lvl in self.need_levels.items())
            )
        lines.append("# People (characters the game tracks; never add these again)\n"
                     + ("\n".join(self.people) or "(nobody else yet)"))
        if self.recent:
            lines.append("# Recent events in this character's chatroom (older ones shortened)")
            full_from = len(self.recent) - RECENT_IN_FULL
            lines += [f"[{m.kind}] {m.text if i >= full_from else shorten(m.text)}"
                      for i, m in enumerate(self.recent)]
        return "\n".join(lines)


RECENT_MESSAGES = 16  # chatroom messages each scene carries
RECENT_IN_FULL = 6    # the newest ones in full; older ones shortened
SHORTENED_CHARS = 500


def shorten(text: str, limit: int = SHORTENED_CHARS) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit].rsplit(" ", 1)[0] + " ..."


ADDITIONS_MARK = "\n\n# Unreviewed lore added by the narrator\n\n"


def load_premise(story_path: Path) -> PremiseText:
    """The story's Premise and Rulebook as one stable block of text, with the
    narrator's own unreviewed additions last so they can be cached apart."""
    parts, additions = [], []
    for folder, heading in (("Premise", "Premise"), ("Rulebook", "Rulebook")):
        root = story_path / folder
        if not root.is_dir():
            continue
        for f in sorted(root.rglob("*.md"), key=lambda p: str(p).lower()):
            rel = f.relative_to(story_path)
            if "AI Additions" in rel.parts:
                if f.name.lower() != "readme.md":
                    additions.append(f"## {rel}\n{f.read_text(encoding='utf-8').strip()}")
                continue
            parts.append(f"## {rel} ({heading})\n{f.read_text(encoding='utf-8').strip()}")
    text = "\n\n".join(parts)
    return text + ADDITIONS_MARK + "\n\n".join(additions) if additions else text


def describe_people(others: list[game.Character], here_ids: set[int], status: dict[int, str],
                    absent_mode: str = "speak_only") -> list[str]:
    """One line per other living character for the scene: who is right here, and who may be voiced.

    `status` maps a character id to "online", "away" or "ai"."""
    lines = []
    for o in sorted(others, key=lambda c: (c.id not in here_ids, c.name.lower())):
        words = o.description.split()
        short = " ".join(words[:40]) + (" ..." if len(words) > 40 else "")
        where = "HERE with them" if o.id in here_ids else f"at {o.location or 'unspecified'}"
        who = {"online": "played by an online player: never write their words or decisions",
               "away": f"player away: {ABSENT_INSTRUCTIONS.get(absent_mode, ABSENT_INSTRUCTIONS['speak_only'])}",
               }.get(status.get(o.id, "ai"), "nobody plays them: you voice them")
        group = f" | collective, parts: {game.describe_parts(o.parts) or 'one body'}" if o.collective else ""
        lines.append(f"- {o.name} | {where} | {o.condition} | {who}{group} | {short}")
    return lines


# --- Narrators ----------------------------------------------------------------

class NarratorError(RuntimeError):
    """The AI couldn't produce a result. Nothing was applied."""


def describe_others(others: list[game.Character]) -> str:
    lines = []
    for o in others:
        words = o.description.split()
        short = " ".join(words[:60]) + (" ..." if len(words) > 60 else "")
        group = f" | collective, parts: {game.describe_parts(o.parts) or 'one body'}" if o.collective else ""
        lines.append(f"- {o.name} | location: {o.location or 'unspecified'} | condition: {o.condition}{group} | {short}")
    return "\n".join(lines) or "(none)"


ABSENT_INSTRUCTIONS = {
    "speak_only": "you may have them answer briefly in words, in character, but they take no actions",
    "act_within_reason": "you may have them answer in character, with small harmless gestures, but nothing that "
    "commits them to anything their player would need to decide",
    "freeze": "they stay silent and still; they seem distracted",
}


class Narrator(Protocol):
    def action(self, premise: PremiseText, scene: Scene, text: str,
               existing: list[str] | None = None) -> AuthorResult: ...

    def question(self, premise: PremiseText, scene: Scene, text: str) -> QuestionAnswer: ...

    def witnesses(self, premise: PremiseText, scene: Scene, text: str, result: AuthorResult,
                  others: list[game.Character]) -> list[WitnessReport]: ...

    def round_change(self, premise: PremiseText, context: str) -> RoundResult: ...

    def backfill(self, premise: PremiseText, scene: Scene, minutes: int) -> BackfillResult: ...

    def intervene(self, premise: PremiseText, context: str, command: str, text: str) -> InterventionResult: ...


# Models that accept the server-side refusal fallback.
FALLBACK_MODELS = ("claude-fable-5-2", "claude-fable-5-1", "claude-fable-5", "claude-opus-5-5", "claude-opus-5",
                   "claude-sonnet-5-5")

ONLY = "Fill in only the `{field}` part of your reply; every other part is null."


class ClaudeNarrator:
    """Runs the passes on the Claude API."""

    def __init__(self, model: str, effort: str, api_key_env: str,
                 usage_log: Callable[[dict], None] | None = None, cache_ttl: str = "1h",
                 profile: Callable[[], aisettings.Profile] | None = None):
        self.model = model
        self.effort = effort
        self.cache_ttl = cache_ttl
        self.api_key_env = api_key_env
        self.usage_log = usage_log  # called with one record per API call, for `coose usage`
        # Which model and effort each task uses; by default the same for all.
        self.profile = profile or (lambda: aisettings.defaults(model, effort))
        self._client = None

    def _client_or_error(self):
        if self._client is None:
            import anthropic

            key = os.environ.get(self.api_key_env)
            self._client = anthropic.Anthropic(api_key=key) if key else anthropic.Anthropic()
        return self._client

    def _system(self, premise: PremiseText) -> list[dict]:
        cache = {"type": "ephemeral", "ttl": self.cache_ttl}
        base, _, additions = premise.partition(ADDITIONS_MARK)
        blocks = [
            {"type": "text", "text": CORE_RULES + OUTPUT_RULES},
            {"type": "text", "text": base, "cache_control": cache},
        ]
        if additions:  # its own breakpoint: new lore rewrites only this part of the cache
            blocks.append({"type": "text", "text": ADDITIONS_MARK.strip() + "\n\n" + additions,
                           "cache_control": cache})
        return blocks

    @staticmethod
    def _scene_block(scene_text: str) -> dict:
        """The scene, cached briefly so the next passes of the same prompt read it cheaply."""
        return {"type": "text", "text": scene_text, "cache_control": {"type": "ephemeral", "ttl": "5m"}}

    def _call(self, label: str, task: str, premise: PremiseText, content: list[dict] | str, part: str):
        """One pass. Returns the filled-in `part` of the shared output shape.

        The reply is JSON read against NarratorOutput. The API's strict
        structured outputs can't compile a schema this size, and a smaller
        schema per pass would mean a cache per pass again, so the schema is
        written once into the cached system prompt instead. A reply that
        doesn't parse gets one retry with the error shown to the model."""
        if isinstance(content, str):
            content = [{"type": "text", "text": content}]
        messages = [{"role": "user", "content": content + [{"type": "text", "text": ONLY.format(field=part)}]}]
        for attempt in (1, 2):
            text = self._request(label, task, premise, messages)
            try:
                result = getattr(read_output(text), part, None)
                if result is None:
                    raise ValueError(f"the `{part}` part is missing or null")
                return result
            except (ValueError, ValidationError) as e:
                log.warning("Unreadable %s reply (attempt %d): %s", label, attempt, str(e)[:300])
                messages = messages + [
                    {"role": "assistant", "content": text or "(empty)"},
                    {"role": "user", "content": f"That reply couldn't be read: {str(e)[:500]}\n\nReply again with "
                     f"only the JSON object, the `{part}` part filled in exactly as the schema says."},
                ]
        raise NarratorError("the narrator's reply couldn't be read")

    def _request(self, label: str, task: str, premise: PremiseText, messages: list[dict]) -> str:
        """Send one request and return the reply's text."""
        import anthropic

        settings = self.profile().for_task(task)
        kwargs = dict(
            model=settings.model,
            max_tokens=16000,
            system=self._system(premise),
            output_config={"effort": settings.effort},
            messages=messages,
        )
        thinking = aisettings.thinking_param(settings)
        if thinking:
            kwargs["thinking"] = thinking
        if settings.model in FALLBACK_MODELS:
            kwargs.update(betas=["server-side-fallback-2026-07-01"], fallbacks="default")
        started = time.monotonic()
        try:
            client = self._client_or_error()
            response = client.beta.messages.create(**kwargs)
        except anthropic.AuthenticationError:
            raise NarratorError(f"the server's API key is missing or invalid (set {self.api_key_env})") from None
        except anthropic.RateLimitError:
            raise NarratorError("the AI is busy right now; try again in a minute") from None
        except anthropic.APIStatusError as e:
            detail = api_error_detail(e)
            log.warning("Claude API error %s on the %s pass (%s, %s effort, thinking %s): %s", e.status_code, label,
                        settings.model, settings.effort, settings.thinking, detail)
            if e.status_code == 400:  # the request itself was refused: say why, so the owner can act on it
                raise NarratorError(f"the AI service refused the request ({detail}). Using {settings.model} for "
                                    f"{task}; the owner can change it with `coose ai`") from None
            raise NarratorError(f"the AI service returned an error ({e.status_code})") from None
        except anthropic.APIConnectionError:
            raise NarratorError("the server couldn't reach the AI service") from None
        except TypeError as e:  # e.g. no credentials configured at all
            raise NarratorError(f"the AI client isn't configured ({e})") from None
        self._log_usage(label, settings.model, response, time.monotonic() - started)
        if response.stop_reason == "refusal":
            raise NarratorError("the narrator declined to continue with that")
        if response.stop_reason == "max_tokens":
            raise NarratorError("the narrator's reply ran too long")
        return "".join(b.text for b in response.content if getattr(b, "type", "") == "text")

    def _log_usage(self, label: str, model: str, response, seconds: float) -> None:
        if self.usage_log is None:
            return
        try:
            u = response.usage
            created = getattr(u, "cache_creation", None)
            self.usage_log({
                "pass": label,
                "model": getattr(response, "model", None) or model,
                "input_tokens": u.input_tokens or 0,
                "output_tokens": u.output_tokens or 0,
                "cache_read_tokens": getattr(u, "cache_read_input_tokens", None) or 0,
                "cache_write_tokens": getattr(u, "cache_creation_input_tokens", None) or 0,
                "cache_write_1h_tokens": (getattr(created, "ephemeral_1h_input_tokens", None) or 0) if created
                else None,
                "seconds": round(seconds, 2),
                "stop_reason": response.stop_reason,
                "cache_ttl": self.cache_ttl,
            })
        except Exception:  # usage records must never break play
            pass

    def action(self, premise: PremiseText, scene: Scene, text: str,
               existing: list[str] | None = None) -> AuthorResult:
        scene_block = self._scene_block(scene.render())
        attempt = f"# Action\n{scene.character.name} attempts:\n{text}"
        author_task = (
            "You are the Author. Decide the objective consequences of this action and write the narration "
            "the acting character's player will read. The narration must contain only what this character "
            "perceives, including what the people around them say and do in response. Record what changed for "
            "the character, how much in-game time passed, who is with them now, anyone new who should become a "
            "character, and objective notes for the working memory (including anyone who would have noticed)."
        )
        if self.profile().action_passes == 1:
            return self._call(
                "action", "action", premise,
                [scene_block, {"type": "text", "text": (
                    f"{attempt}\n\nFirst judge the action as the Responder would: check it against the Rulebook, "
                    "the mechanics in the Premise, and whether this character is capable of it right now, and "
                    f"decide whether it succeeds, partly succeeds, fails or is impossible. Then: {author_task}")}],
                "author",
            )
        responder = self._call(
            "responder", "action", premise,
            [scene_block, {"type": "text", "text": (
                f"{attempt}\n\nYou are the Responder. Check the action against the Rulebook, the mechanics in the "
                "Premise, and whether this character is capable of it right now. Decide the outcome and write an "
                "objective draft of what actually happens.")}],
            "responder",
        )
        return self._call(
            "author", "action", premise,
            [scene_block, {"type": "text", "text": (
                f"{attempt}\n\n# Responder's judgement\nOutcome: {responder.outcome}\n"
                f"Reasoning: {responder.reasoning}\nDraft: {responder.draft}\n\n{author_task}")}],
            "author",
        )

    def question(self, premise: PremiseText, scene: Scene, text: str) -> QuestionAnswer:
        answer = self._call(
            "question", "question", premise,
            [self._scene_block(scene.render()), {"type": "text", "text": (
                f"# Question from {scene.character.name}'s player (out of character)\n{text}\n\n"
                "Answer only with what this character knows, has experienced in their chatroom, or would "
                "reasonably know as someone in their position. If they wouldn't know, say so plainly. Do not "
                "reveal objective facts from the Premise, world state or working memory that they couldn't "
                "know. Answer briefly, addressed to the player.\n\n"
                "You can add people to the game's list of characters. If the player asks for someone their "
                "character can perceive right now to be added (or asks why someone isn't listed), add them under "
                "new_characters and say so in your answer. Never add anyone already listed under People.")}],
            "answer",
        )
        if not answer.answer.strip():
            raise NarratorError("the narrator gave an empty answer")
        return answer

    def witnesses(self, premise: PremiseText, scene: Scene, text: str, result: AuthorResult,
                  others: list[game.Character]) -> list[WitnessReport]:
        actor = scene.character
        response = self._call(
            "witnesses", "witnesses", premise,
            [self._scene_block(scene.render()), {"type": "text", "text": (
                f"# Action\n{actor.name} attempted:\n{text}\n\n"
                f"# What objectively happened\nTime taken: {result.minutes_elapsed} minutes\n"
                f"{actor.name}'s location afterwards: {result.location or actor.location or 'unspecified'}\n"
                "Notes: " + ("; ".join(result.memory_add) or "(none)") + "\n"
                f"What {actor.name} perceived: {result.narration}\n\n"
                f"# Candidate witnesses\n{describe_others(others)}\n\n"
                "You are judging witnesses. Decide which of the candidate witnesses would realistically notice "
                "any part of this action right now, by sight, sound or other senses, given where they are. Being "
                "somewhere else usually means they notice nothing. For each witness, write what they perceive "
                "from their own point of view and what they now know. Witnesses see behaviour, not thoughts: "
                f"never reveal {actor.name}'s intentions, the wording of the attempt, or anything the witness "
                "couldn't perceive. If they don't know who someone is, describe them instead of naming them. "
                "Likewise never reveal anything about the actor's background, origin, past or profession that the "
                "witness hasn't been told or seen for themselves; a description is only what they can see. If "
                "the narration has a witness speak or act, that is what they did: tell them so in their report.")}],
            "witnesses",
        )
        return response.reports

    def round_change(self, premise: PremiseText, context: str) -> RoundResult:
        return self._call(
            "round", "round", premise,
            f"{context}\n\n"
            "You are closing this round and opening the next. Reconcile everything that happened into the "
            "objective World State. Review the working memory and keep only what the next round still needs. "
            "Update every living character, and write each one a short in-character notice of what they "
            "noticed changing. Notices follow the governing rule: only what that character could perceive or "
            "already knows, and never details about other characters (names, origins, pasts) they haven't "
            "learned in play. Also write the Logbook entry for the round that ended.",
            "round",
        )

    def backfill(self, premise: PremiseText, scene: Scene, minutes: int) -> BackfillResult:
        return self._call(
            "backfill", "backfill", premise,
            [self._scene_block(scene.render()), {"type": "text", "text": (
                f"# Time away\n{scene.character.name}'s player was offline while "
                f"{format_minutes(minutes)} passed in a time skip. The character was left untouched. Now that "
                "their player is back, write a plausible backstory for that time, consistent with who they are, "
                "where they were and the world state, and record what changed. Don't make big decisions for them.")}],
            "backfill",
        )

    def intervene(self, premise: PremiseText, context: str, command: str, text: str) -> InterventionResult:
        return self._call(
            command, "admin", premise,
            f"{context}\n\n# Story admin command (/{command})\n{text}\n\n{INTERVENTIONS[command]} Record every "
            "objective change. Players only learn of it through their own characters' senses: write each affected "
            "character a notice following the governing rule, and leave it empty if they wouldn't perceive it.",
            "intervention",
        )


INTERVENTIONS = {
    "intervene": "A story admin is intervening with this out-of-character instruction. Carry it out: it can "
    "rewrite, overrule, undo or warn about anything in the world. It overrides realism and the Rulebook.",
    "rewrite": "A story admin is rewriting a past event. Change the world so that the event happened as they "
    "describe instead, and adjust everything that follows from it.",
}


def api_error_detail(e) -> str:
    """The API's own explanation of an error, kept short."""
    body = getattr(e, "body", None)
    message = ""
    if isinstance(body, dict):
        err = body.get("error")
        message = err.get("message", "") if isinstance(err, dict) else ""
    message = " ".join(str(message or getattr(e, "message", "") or "").split())
    return message[:300] or "no details given"


def format_minutes(minutes: int) -> str:
    """In-game durations in words: 90 -> '1 hour 30 minutes'."""
    days, rest = divmod(int(minutes), 24 * 60)
    hours, mins = divmod(rest, 60)
    parts = [f"{n} {unit}{'s' if n != 1 else ''}" for n, unit in ((days, "day"), (hours, "hour"), (mins, "minute")) if n]
    return " ".join(parts) or "no time"

"""The AI side of Coose: turning a character's prompt into what happens.

An Action takes two passes. The Responder judges whether and how the action
can happen (Rulebook, Premise mechanics, the character's capability). The
Author then decides the objective consequences and writes the narration the
acting player sees. When other characters exist, a third pass judges who
witnessed the Action and what each of them perceived. A Question takes one
pass, answered only from what the character could know. When someone speaks
to a character nobody is playing right now, one pass answers in their voice.
A round change is one pass over the whole world (see rounds.py), and a
character returning after a skip they sat out gets a backstory pass.

The story's Premise and Rulebook go first in every request as a cached
system prompt; everything that changes per prompt comes after it.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Literal, Protocol

from pydantic import BaseModel, Field

from . import game

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

Write narration in the second person, present tense, addressed to the acting character. Keep it \
vivid but tight: a few short paragraphs at most. Do not invent the player character's own words, \
feelings or decisions beyond what the action states.
"""

PremiseText = str


# --- Structured outputs ------------------------------------------------------

class ResponderDraft(BaseModel):
    outcome: Literal["success", "partial", "failure", "impossible"] = Field(
        description="How the attempt goes, judged by realism, the Rulebook and the character's capability."
    )
    reasoning: str = Field(description="Brief objective reasoning for the outcome. Never shown to players.")
    draft: str = Field(description="An objective account of what actually happens.")


class NewCharacter(BaseModel):
    name: str = Field(description="Their name as people in the world would say it.")
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
        description="Named people this action brings into the story who are likely to matter again, so they "
        "can be spoken to and played. Leave empty for unnamed passers-by and for anyone listed under "
        "existing characters."
    )


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


class InterventionResult(BaseModel):
    reply: str = Field(description="A short objective report to the story admin of what you changed and why.")
    world_state: str = Field(
        description="The complete new World State in Markdown if it changes, or empty to leave it as it is."
    )
    memory_add: list[str] = Field(description="Objective notes for the working memory about the change.")
    characters: list[CharacterChange] = Field(description="Only the characters this changes.")


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
            "What they know:\n" + ("\n".join(f"- {k}" for k in c.knowledge) or "- nothing beyond their description"),
        ]
        if self.need_levels:
            lines.append(
                "Hidden needs (never state these words or any numbers to the player; let them show through "
                "how the character feels):\n" + "\n".join(f"- {n}: {lvl}" for n, lvl in self.need_levels.items())
            )
        if self.recent:
            lines.append("# Recent events in this character's chatroom")
            lines += [f"[{m.kind}] {m.text}" for m in self.recent]
        return "\n".join(lines)


def load_premise(story_path: Path) -> PremiseText:
    """The story's Premise and Rulebook as one stable block of text."""
    parts = []
    for folder, heading in (("Premise", "Premise"), ("Rulebook", "Rulebook")):
        root = story_path / folder
        if not root.is_dir():
            continue
        for f in sorted(root.rglob("*.md"), key=lambda p: str(p).lower()):
            rel = f.relative_to(story_path)
            label = "unreviewed lore added by the narrator" if "AI Additions" in rel.parts else heading
            parts.append(f"## {rel} ({label})\n{f.read_text(encoding='utf-8').strip()}")
    return "\n\n".join(parts)


# --- Narrators ----------------------------------------------------------------

class NarratorError(RuntimeError):
    """The AI couldn't produce a result. Nothing was applied."""


def describe_others(others: list[game.Character]) -> str:
    lines = []
    for o in others:
        words = o.description.split()
        short = " ".join(words[:60]) + (" ..." if len(words) > 60 else "")
        lines.append(f"- {o.name} | location: {o.location or 'unspecified'} | condition: {o.condition} | {short}")
    return "\n".join(lines) or "(none)"


ABSENT_INSTRUCTIONS = {
    "speak_only": "Reply only with words they say aloud. They take no actions while their player is away.",
    "act_within_reason": "Reply with what they say aloud. You may add a small, harmless gesture or movement in "
    "brackets if it is natural, but nothing that commits them to anything their player would need to decide.",
}


class Narrator(Protocol):
    def action(self, premise: PremiseText, scene: Scene, text: str,
               existing: list[str] | None = None) -> AuthorResult: ...

    def question(self, premise: PremiseText, scene: Scene, text: str) -> str: ...

    def witnesses(self, premise: PremiseText, scene: Scene, text: str, result: AuthorResult,
                  others: list[game.Character]) -> list[WitnessReport]: ...

    def reply_as(self, premise: PremiseText, scene: Scene, speaker: str, text: str, mode: str) -> str: ...

    def round_change(self, premise: PremiseText, context: str) -> RoundResult: ...

    def backfill(self, premise: PremiseText, scene: Scene, minutes: int) -> BackfillResult: ...

    def intervene(self, premise: PremiseText, context: str, command: str, text: str) -> InterventionResult: ...


class ClaudeNarrator:
    """Runs the passes on the Claude API."""

    def __init__(self, model: str, effort: str, api_key_env: str,
                 usage_log: Callable[[dict], None] | None = None, cache_ttl: str = "1h"):
        self.model = model
        self.effort = effort
        self.cache_ttl = cache_ttl
        self.api_key_env = api_key_env
        self.usage_log = usage_log  # called with one record per API call, for `coose usage`
        self._client = None

    def _client_or_error(self):
        if self._client is None:
            import anthropic

            key = os.environ.get(self.api_key_env)
            self._client = anthropic.Anthropic(api_key=key) if key else anthropic.Anthropic()
        return self._client

    def _system(self, premise: PremiseText) -> list[dict]:
        return [
            {"type": "text", "text": CORE_RULES},
            {"type": "text", "text": premise, "cache_control": {"type": "ephemeral", "ttl": self.cache_ttl}},
        ]

    def _call(self, label: str, premise: PremiseText, prompt: str, output_format=None):
        import anthropic

        kwargs = dict(
            model=self.model,
            max_tokens=16000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            system=self._system(premise),
            output_config={"effort": self.effort},
            messages=[{"role": "user", "content": prompt}],
        )
        started = time.monotonic()
        try:
            client = self._client_or_error()
            if output_format is not None:
                response = client.beta.messages.parse(output_format=output_format, **kwargs)
            else:
                response = client.beta.messages.create(**kwargs)
        except anthropic.AuthenticationError:
            raise NarratorError(f"the server's API key is missing or invalid (set {self.api_key_env})") from None
        except anthropic.RateLimitError:
            raise NarratorError("the AI is busy right now; try again in a minute") from None
        except anthropic.APIStatusError as e:
            raise NarratorError(f"the AI service returned an error ({e.status_code})") from None
        except anthropic.APIConnectionError:
            raise NarratorError("the server couldn't reach the AI service") from None
        except TypeError as e:  # e.g. no credentials configured at all
            raise NarratorError(f"the AI client isn't configured ({e})") from None
        self._log_usage(label, response, time.monotonic() - started)
        if response.stop_reason == "refusal":
            raise NarratorError("the narrator declined to continue with that")
        if response.stop_reason == "max_tokens":
            raise NarratorError("the narrator's reply ran too long")
        return response

    def _log_usage(self, label: str, response, seconds: float) -> None:
        if self.usage_log is None:
            return
        u = response.usage
        try:
            self.usage_log({
                "pass": label,
                "model": getattr(response, "model", None) or self.model,
                "input_tokens": u.input_tokens or 0,
                "output_tokens": u.output_tokens or 0,
                "cache_read_tokens": getattr(u, "cache_read_input_tokens", None) or 0,
                "cache_write_tokens": getattr(u, "cache_creation_input_tokens", None) or 0,
                "seconds": round(seconds, 2),
                "stop_reason": response.stop_reason,
                "cache_ttl": self.cache_ttl,
            })
        except Exception:  # usage records must never break play
            pass

    def action(self, premise: PremiseText, scene: Scene, text: str,
               existing: list[str] | None = None) -> AuthorResult:
        scene_text = scene.render()
        responder = self._call(
            "responder",
            premise,
            f"{scene_text}\n\n# Action\n{scene.character.name} attempts:\n{text}\n\n"
            "You are the Responder. Check the action against the Rulebook, the mechanics in the Premise, "
            "and whether this character is capable of it right now. Decide the outcome and write an "
            "objective draft of what actually happens.",
            output_format=ResponderDraft,
        ).parsed_output
        if responder is None:
            raise NarratorError("the narrator's reply couldn't be read")
        author = self._call(
            "author",
            premise,
            f"{scene_text}\n\n# Action\n{scene.character.name} attempts:\n{text}\n\n"
            f"# Responder's judgement\nOutcome: {responder.outcome}\nReasoning: {responder.reasoning}\n"
            f"Draft: {responder.draft}\n\n"
            "You are the Author. Decide the objective consequences of this action and write the narration "
            "the acting character's player will read. The narration must contain only what this character "
            "perceives. Record what changed for the character, how much in-game time passed, and objective "
            "notes for the working memory (including anyone who would have noticed).\n\n"
            "# Existing characters (never add these as new characters)\n"
            + ("\n".join(f"- {n}" for n in existing or []) or "(none)"),
            output_format=AuthorResult,
        ).parsed_output
        if author is None:
            raise NarratorError("the narrator's reply couldn't be read")
        return author

    def question(self, premise: PremiseText, scene: Scene, text: str) -> str:
        response = self._call(
            "question",
            premise,
            f"{scene.render()}\n\n# Question from {scene.character.name}'s player (out of character)\n{text}\n\n"
            "Answer only with what this character knows, has experienced in their chatroom, or would "
            "reasonably know as someone in their position. If they wouldn't know, say so plainly. Do not "
            "reveal objective facts from the Premise, world state or working memory that they couldn't "
            "know. Answer briefly, addressed to the player.",
        )
        answer = "".join(b.text for b in response.content if b.type == "text").strip()
        if not answer:
            raise NarratorError("the narrator gave an empty answer")
        return answer

    def witnesses(self, premise: PremiseText, scene: Scene, text: str, result: AuthorResult,
                  others: list[game.Character]) -> list[WitnessReport]:
        actor = scene.character
        response = self._call(
            "witnesses",
            premise,
            f"{scene.render()}\n\n# Action\n{actor.name} attempted:\n{text}\n\n"
            f"# What objectively happened\nTime taken: {result.minutes_elapsed} minutes\n"
            f"{actor.name}'s location afterwards: {result.location or actor.location or 'unspecified'}\n"
            "Notes: " + ("; ".join(result.memory_add) or "(none)") + "\n"
            f"What {actor.name} perceived: {result.narration}\n\n"
            f"# Other characters in the world\n{describe_others(others)}\n\n"
            "You are judging witnesses. Decide which of the other characters would realistically notice any "
            "part of this action right now, by sight, sound or other senses, given where they are. Being "
            "somewhere else usually means they notice nothing. For each witness, write what they perceive "
            "from their own point of view and what they now know. Witnesses see behaviour, not thoughts: "
            f"never reveal {actor.name}'s intentions, the wording of the attempt, or anything the witness "
            "couldn't perceive. If they don't know who someone is, describe them instead of naming them. "
            "Likewise never reveal anything about the actor's background, origin, past or profession that the "
            "witness hasn't been told or seen for themselves; a description is only what they can see.",
            output_format=WitnessPass,
        ).parsed_output
        if response is None:
            raise NarratorError("the narrator's reply couldn't be read")
        return response.reports

    def reply_as(self, premise: PremiseText, scene: Scene, speaker: str, text: str, mode: str) -> str:
        me = scene.character
        response = self._call(
            "reply",
            premise,
            f"{scene.render()}\n\n# Someone speaks to {me.name}\n{speaker} says to {me.name}: \"{text}\"\n\n"
            f"{me.name}'s player is away, so you answer for {me.name}, in character and in their own voice, "
            "consistent with their description, what they know and their recent chatroom. "
            f"{ABSENT_INSTRUCTIONS.get(mode, ABSENT_INSTRUCTIONS['speak_only'])} Keep it short, like a real "
            "reply in conversation. Write only the reply itself, with no quotation marks around it and no "
            "narration.",
        )
        reply = "".join(b.text for b in response.content if b.type == "text").strip().strip('"').strip()
        if not reply:
            raise NarratorError("the narrator gave an empty reply")
        return reply

    def round_change(self, premise: PremiseText, context: str) -> RoundResult:
        result = self._call(
            "round",
            premise,
            f"{context}\n\n"
            "You are closing this round and opening the next. Reconcile everything that happened into the "
            "objective World State. Review the working memory and keep only what the next round still needs. "
            "Update every living character, and write each one a short in-character notice of what they "
            "noticed changing. Notices follow the governing rule: only what that character could perceive or "
            "already knows, and never details about other characters (names, origins, pasts) they haven't "
            "learned in play. Also write the Logbook entry for the round that ended.",
            output_format=RoundResult,
        ).parsed_output
        if result is None:
            raise NarratorError("the narrator's reply couldn't be read")
        return result

    def backfill(self, premise: PremiseText, scene: Scene, minutes: int) -> BackfillResult:
        result = self._call(
            "backfill",
            premise,
            f"{scene.render()}\n\n# Time away\n{scene.character.name}'s player was offline while "
            f"{format_minutes(minutes)} passed in a time skip. The character was left untouched. Now that "
            "their player is back, write a plausible backstory for that time, consistent with who they are, "
            "where they were and the world state, and record what changed. Don't make big decisions for them.",
            output_format=BackfillResult,
        ).parsed_output
        if result is None:
            raise NarratorError("the narrator's reply couldn't be read")
        return result


INTERVENTIONS = {
    "intervene": "A story admin is intervening with this out-of-character instruction. Carry it out: it can "
    "rewrite, overrule, undo or warn about anything in the world. It overrides realism and the Rulebook.",
    "rewrite": "A story admin is rewriting a past event. Change the world so that the event happened as they "
    "describe instead, and adjust everything that follows from it.",
}


def _intervene(self, premise: PremiseText, context: str, command: str, text: str) -> InterventionResult:
    result = self._call(
        command,
        premise,
        f"{context}\n\n# Story admin command (/{command})\n{text}\n\n{INTERVENTIONS[command]} Record every "
        "objective change. Players only learn of it through their own characters' senses: write each affected "
        "character a notice following the governing rule, and leave it empty if they wouldn't perceive it.",
        output_format=InterventionResult,
    ).parsed_output
    if result is None:
        raise NarratorError("the narrator's reply couldn't be read")
    return result


ClaudeNarrator.intervene = _intervene


def format_minutes(minutes: int) -> str:
    """In-game durations in words: 90 -> '1 hour 30 minutes'."""
    days, rest = divmod(int(minutes), 24 * 60)
    hours, mins = divmod(rest, 60)
    parts = [f"{n} {unit}{'s' if n != 1 else ''}" for n, unit in ((days, "day"), (hours, "hour"), (mins, "minute")) if n]
    return " ".join(parts) or "no time"

"""The AI side of Coose: turning a character's prompt into what happens.

An Action takes two passes. The Responder judges whether and how the action
can happen (Rulebook, Premise mechanics, the character's capability). The
Author then decides the objective consequences and writes the narration the
acting player sees. A Question takes one pass, answered only from what the
character could know.

The story's Premise and Rulebook go first in every request as a cached
system prompt; everything that changes per prompt comes after it.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal, Protocol

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


class AuthorResult(BaseModel):
    narration: str = Field(
        description="What the acting character perceives, second person, present tense. Only what they could notice."
    )
    minutes_elapsed: int = Field(description="In-game minutes the action takes. 0 for something instant.")
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

    def render(self) -> str:
        c = self.character
        lines = [
            f"# Current moment\nIn-game time: {self.world_time:%A %d %B %Y, %H:%M}",
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


class Narrator(Protocol):
    def action(self, premise: PremiseText, scene: Scene, text: str) -> AuthorResult: ...

    def question(self, premise: PremiseText, scene: Scene, text: str) -> str: ...


class ClaudeNarrator:
    """Runs the passes on the Claude API."""

    def __init__(self, model: str, effort: str, api_key_env: str):
        self.model = model
        self.effort = effort
        self.api_key_env = api_key_env
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
            {"type": "text", "text": premise, "cache_control": {"type": "ephemeral"}},
        ]

    def _call(self, premise: PremiseText, prompt: str, output_format=None):
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
        if response.stop_reason == "refusal":
            raise NarratorError("the narrator declined to continue with that")
        if response.stop_reason == "max_tokens":
            raise NarratorError("the narrator's reply ran too long")
        return response

    def action(self, premise: PremiseText, scene: Scene, text: str) -> AuthorResult:
        scene_text = scene.render()
        responder = self._call(
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
            premise,
            f"{scene_text}\n\n# Action\n{scene.character.name} attempts:\n{text}\n\n"
            f"# Responder's judgement\nOutcome: {responder.outcome}\nReasoning: {responder.reasoning}\n"
            f"Draft: {responder.draft}\n\n"
            "You are the Author. Decide the objective consequences of this action and write the narration "
            "the acting character's player will read. The narration must contain only what this character "
            "perceives. Record what changed for the character, how much in-game time passed, and objective "
            "notes for the working memory (including anyone who would have noticed).",
            output_format=AuthorResult,
        ).parsed_output
        if author is None:
            raise NarratorError("the narrator's reply couldn't be read")
        return author

    def question(self, premise: PremiseText, scene: Scene, text: str) -> str:
        response = self._call(
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

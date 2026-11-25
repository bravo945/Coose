"""Lore the AI invents. It only ever adds files to Premise/AI Additions, where
story admins approve them into the Premise or delete them (/approve, /reject).
The author's own Premise is never touched."""

from __future__ import annotations

import re
from pathlib import Path

AI_ADDITIONS = Path("Premise") / "AI Additions"


def _filename(title: str) -> str:
    name = re.sub(r"[^\w\s'-]", "", title).strip()
    name = re.sub(r"\s+", " ", name)[:80]
    return f"{name or 'Untitled'}.md"


def write_lore(story_path: Path, entries: list, source: str) -> list[str]:
    """Write each {title, text} entry as its own file, appending to one with the same title.
    Returns the file names written."""
    folder = story_path / AI_ADDITIONS
    written = []
    for entry in entries:
        e = entry if isinstance(entry, dict) else entry.model_dump()
        title, text = str(e.get("title", "")).strip(), str(e.get("text", "")).strip()
        if not title or not text:
            continue
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / _filename(title)
        if path.exists():
            path.write_text(path.read_text(encoding="utf-8").rstrip() + f"\n\n*Added {source}:*\n{text}\n",
                            encoding="utf-8")
        else:
            path.write_text(f"# {title}\n\n*Written by the AI {source}. Unreviewed.*\n\n{text}\n", encoding="utf-8")
        written.append(path.name)
    return written


def pending_lore(story_path: Path) -> list[str]:
    folder = story_path / AI_ADDITIONS
    if not folder.is_dir():
        return []
    return sorted(p.name for p in folder.glob("*.md") if p.name.lower() != "readme.md")

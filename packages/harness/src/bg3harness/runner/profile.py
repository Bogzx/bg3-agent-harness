"""A playthrough's profile.json (moved from bg3harness.runner.policy, Phase D.2; stdlib only, so the app's config
reads it without the runner): {"character": {"brief", "name"?, "constraints"?, "roleplay"}, "difficulty"?}, the
roleplay modes and their kickoff texts, new_game's difficulties. bg3play.config's profile.json migration and the
runner's load_profile are this one parse."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

ROLEPLAY_MODES = ("strict", "light")
DIFFICULTIES = ("Explorer", "Balanced", "Tactician")  # new_game's
ROLEPLAY_TEXT = {
    "strict": "Roleplay: strict. Play as this character for the whole game: dialogue, companions, moral choices and "
    "combat style follow CHARACTER.md; odds, safety and approval only break ties between in-character "
    "options.",
    "light": "Roleplay: light. Keep the persona in dialogue and big choices; odds, safety and approval weigh as much "
    "as the character does.",
}


def parse_profile(raw: object) -> dict[str, Any]:
    """profile.json's parsed JSON -> the profile. Not an object, or a character without a brief, means today's flow
    (no character block); an unknown roleplay is strict, an unknown difficulty is left out."""
    if not isinstance(raw, dict):
        return {}
    out = dict(cast(dict[str, Any], raw))
    c: object = out.get("character")
    if isinstance(c, dict):
        char = cast(dict[str, Any], c)
        if isinstance(char.get("brief"), str) and char["brief"].strip():
            mode = char.get("roleplay")
            out["character"] = dict(char, roleplay=mode if mode in ROLEPLAY_MODES else "strict")
        else:
            out.pop("character", None)
    else:
        out.pop("character", None)
    if out.get("difficulty") not in DIFFICULTIES:
        out.pop("difficulty", None)
    return out


def load_profile(path: Path) -> dict[str, Any]:
    """playthroughs/<pt>/profile.json; a missing or broken file is the empty profile (parse_profile)."""
    try:
        raw = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return parse_profile(raw)


def update_profile(
    profile: dict[str, Any], brief: str | None = None, roleplay: str | None = None, difficulty: str | None = None
) -> dict[str, Any]:
    """The profile with run --character-brief/--roleplay/--difficulty applied; other keys are kept."""
    out: dict[str, Any] = json.loads(json.dumps(profile))
    c: dict[str, Any] = dict(out.get("character") or {})
    if brief and brief.strip():
        c["brief"] = brief.strip()
    if roleplay:
        c["roleplay"] = roleplay
    if c.get("brief"):
        c.setdefault("roleplay", "strict")
        out["character"] = c
    if difficulty:
        out["difficulty"] = difficulty
    return out

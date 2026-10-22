"""The game's difficulty names: the Options name a player picks (and scenarios use), the ruleset name the mod's
scn.difficulty reads (Ruleset.Name), and the name in a save's SaveInfo.json "Difficulty" list. live6 §2: Balanced
read as DIFFICULTY_MEDIUM live and DifficultyMedium in the save, so make_start_save refused every save."""

from __future__ import annotations

# Options name -> (ruleset name, SaveInfo.json name). Only Balanced's pair was seen live (live6).
NAMES = {
    "Explorer": ("DIFFICULTY_EASY", "DifficultyEasy"),
    "Balanced": ("DIFFICULTY_MEDIUM", "DifficultyMedium"),
    "Tactician": ("DIFFICULTY_HARD", "DifficultyHard"),
}
_BY_ANY = {n.lower(): opt for opt, names in NAMES.items() for n in (opt, *names)}


def option_name(name: object) -> str | None:
    """'DIFFICULTY_MEDIUM' / 'DifficultyMedium' / 'Balanced' -> 'Balanced'; None for anything else."""
    return _BY_ANY.get(name.lower()) if isinstance(name, str) else None

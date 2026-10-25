"""The game's settings as a run played them (the run record's `game_settings`, for the bench manifest) and the
scenario setup through the mod's scn.* RPCs (mod/.../Server/Scenario.lua: refused unless the game runs with
BG3AI_SCENARIO_SETUP=1).

Sources, each read-only: the resolution from the Larian profile's graphicSettings.lsx, the UI scale from its
PlayerProfiles/Public/config.lsf (the game writes only non-default entries: null = the default), the language from
Steam's app manifest (UserConfig.language), Karmic Dice from the mod's roll.snapshot (RollManager.RollModeType:
WeightedRandom = karmic), the difficulty from the mod's scn.difficulty (the loaded game's Ruleset, as its Options name:
bg3ai.difficulty; null at the main menu or without the scn.* switch).

    python3 -m unittest runner/test_game_settings.py
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol, cast

from bg3ai.difficulty import option_name

SETUP_ENV = "BG3AI_SCENARIO_SETUP"  # the game process's env switch for every scn.* op
_ENTRY = re.compile(
    r'value="(?P<key>[A-Za-z0-9_]+)"\s*/>\s*<attribute id="Type"[^>]*/>\s*'
    r'<attribute id="Value" type="[a-z0-9]+" value="(?P<value>[^"]*)"'
)
_UI_SCALE = re.compile(r"(UI|HUD).*Scale", re.I)

Rpc = Callable[[str, dict[str, Any]], Any]


class Attr(Protocol):
    """One LSF attribute (tools/saves/lsf.Attr)."""

    name: str
    value: Any


def graphic_entries(lsx: str) -> dict[str, str]:
    """graphicSettings.lsx's ConfigEntry MapKey -> Value."""
    return {m["key"]: m["value"] for m in _ENTRY.finditer(lsx)}


def resolution(entries: dict[str, str]) -> str | None:
    w, h = entries.get("ScreenWidth"), entries.get("ScreenHeight")
    return f"{w}x{h}" if w and h else None


def config_entries(attrs: list[Attr]) -> dict[str, Any]:
    """config.lsf's attributes (tools/saves/lsf.attributes, in file order) -> MapKey -> Value."""
    out: dict[str, Any] = {}
    key: str | None = None
    for a in attrs:
        if a.name == "MapKey":
            key = str(a.value)
        elif a.name == "Value" and key is not None:
            out[key], key = a.value, None
    return out


def ui_scale(entries: dict[str, Any]) -> Any:
    return next((v for k, v in entries.items() if _UI_SCALE.search(k)), None)


def _obj(v: Any) -> dict[str, Any]:
    return cast(dict[str, Any], v) if isinstance(v, dict) else {}


def language(manifest_vdf: dict[str, Any]) -> str | None:
    """steam_paths.parse_vdf(appmanifest_*.acf) -> AppState.UserConfig.language."""
    lang = _obj(_obj(manifest_vdf.get("AppState")).get("UserConfig")).get("language")
    return str(lang) if lang else None


def karmic(rpc: Rpc) -> tuple[str | None, bool | None]:
    """(RollModeType, karmic) from roll.snapshot; (None, None) when the mod doesn't answer."""
    try:
        mode = _obj(rpc("roll.snapshot", {})).get("mode")
    except Exception:  # an old mod or a loading screen: unknown
        return None, None
    return (str(mode), str(mode) == "WeightedRandom") if mode else (None, None)


def difficulty(rpc: Rpc) -> str | None:
    """The loaded game's difficulty as its Options name (Balanced) from scn.difficulty's ruleset name
    (DIFFICULTY_MEDIUM); a ruleset name the table doesn't know is returned as is; None when the mod doesn't answer."""
    try:
        name = _obj(rpc("scn.difficulty", {})).get("difficulty")
    except Exception:  # no level loaded, an old mod, or no scn.* switch: unknown
        return None
    return (option_name(name) or str(name)) if name else None


def apply_karmic(rpc: Rpc, karmic: bool) -> dict[str, Any]:
    """scn.karmic {set}: Karmic Dice on or off for the run (scn.undo puts the player's back)."""
    return _obj(rpc("scn.karmic", {"set": bool(karmic)}))


def apply_avatar_name(rpc: Rpc, name: str) -> dict[str, Any]:
    """scn.rename_avatar {name}: the start save's avatar plays under the scenario's character name (every field the
    game and the bridge read it from; the mod re-applies it after each load, scn.undo puts the save's back)."""
    return _obj(rpc("scn.rename_avatar", {"name": name}))


def readout(files: dict[str, Callable[[], Any]], rpc: Rpc) -> dict[str, Any]:
    """{resolution, ui_scale, language, difficulty, roll_mode, karmic_dice}. files: "graphics" -> the lsx text,
    "config" -> config.lsf's attributes, "manifest" -> the parsed app manifest; a reader that fails gives null."""

    def get(name: str, parse: Callable[[Any], Any]) -> Any:
        try:
            return parse(files[name]())
        except Exception:  # a missing or unreadable file: unknown
            return None

    mode, is_karmic = karmic(rpc)
    return {
        "resolution": get("graphics", lambda t: resolution(graphic_entries(t))),
        "ui_scale": get("config", lambda a: ui_scale(config_entries(a))),
        "language": get("manifest", language),
        "difficulty": difficulty(rpc),
        "roll_mode": mode,
        "karmic_dice": is_karmic,
    }


def apply_seed(rpc: Rpc, seed: int) -> dict[str, Any]:
    """scn.seed once at the run's start: the mod records it and writes it again after every load."""
    return _obj(rpc("scn.seed", {"seed": int(seed)}))


def undo(rpc: Rpc) -> dict[str, Any]:
    """scn.undo at the run's end: the seed record, renames and the avatar's name go (also after a load)."""
    return _obj(rpc("scn.undo", {}))


def profile_files(
    profile_dir: Path,
    manifest: Path,
    attributes: Callable[[bytes], list[Any]],
    parse_vdf: Callable[[str], dict[str, Any]],
) -> dict[str, Callable[[], Any]]:
    """The readers readout() takes, from the Larian profile folder and Steam's app manifest (tools/saves/lsf's
    attributes and tools/steam_paths's parse_vdf parse them)."""
    return {
        "graphics": lambda: (profile_dir / "graphicSettings.lsx").read_text(errors="replace"),
        "config": lambda: attributes((profile_dir / "PlayerProfiles" / "Public" / "config.lsf").read_bytes()),
        "manifest": lambda: parse_vdf(manifest.read_text(errors="replace")),
    }

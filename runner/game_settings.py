"""The game's settings as a run played them (the run record's `game_settings`, for the bench manifest) and the
scenario setup through the mod's scn.* RPCs (mod/.../Server/Scenario.lua: refused unless the game runs with
BG3AI_SCENARIO_SETUP=1).

Sources, each read-only: the resolution from the Larian profile's graphicSettings.lsx, the UI scale from its
PlayerProfiles/Public/config.lsf (the game writes only non-default entries: null = the default), the language from
Steam's app manifest (UserConfig.language), Karmic Dice from the mod's roll.snapshot (RollManager.RollModeType:
WeightedRandom = karmic). The difficulty has no reader yet (it lives in the save; null).

    python3 -m unittest runner/test_game_settings.py
"""
from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

SETUP_ENV = "BG3AI_SCENARIO_SETUP"   # the game process's env switch for every scn.* op
_ENTRY = re.compile(r'value="(?P<key>[A-Za-z0-9_]+)"\s*/>\s*<attribute id="Type"[^>]*/>\s*'
                    r'<attribute id="Value" type="[a-z0-9]+" value="(?P<value>[^"]*)"')
_UI_SCALE = re.compile(r"(UI|HUD).*Scale", re.I)

Rpc = Callable[[str, dict], Any]


def graphic_entries(lsx: str) -> dict[str, str]:
    """graphicSettings.lsx's ConfigEntry MapKey -> Value."""
    return {m["key"]: m["value"] for m in _ENTRY.finditer(lsx)}


def resolution(entries: dict[str, str]) -> str | None:
    w, h = entries.get("ScreenWidth"), entries.get("ScreenHeight")
    return f"{w}x{h}" if w and h else None


def config_entries(attrs: list) -> dict[str, Any]:
    """config.lsf's attributes (tools/saves/lsf.attributes: path, name, value in file order) -> MapKey -> Value."""
    out, key = {}, None
    for a in attrs:
        if a.name == "MapKey":
            key = a.value
        elif a.name == "Value" and key is not None:
            out[str(key)], key = a.value, None
    return out


def ui_scale(entries: dict[str, Any]) -> Any:
    return next((v for k, v in entries.items() if _UI_SCALE.search(k)), None)


def language(manifest_vdf: dict) -> str | None:
    """steam_paths.parse_vdf(appmanifest_*.acf) -> AppState.UserConfig.language."""
    state = manifest_vdf.get("AppState") or {}
    return ((state.get("UserConfig") or {}).get("language")) or None


def karmic(rpc: Rpc) -> tuple[str | None, bool | None]:
    """(RollModeType, karmic) from roll.snapshot; (None, None) when the mod doesn't answer."""
    try:
        mode = (rpc("roll.snapshot", {}) or {}).get("mode")
    except Exception:  # noqa: BLE001 - an old mod or a loading screen: unknown
        return None, None
    return (str(mode), str(mode) == "WeightedRandom") if mode else (None, None)


def readout(files: dict[str, Callable[[], Any]], rpc: Rpc) -> dict:
    """{resolution, ui_scale, language, difficulty, roll_mode, karmic_dice}. files: "graphics" -> the lsx text,
    "config" -> config.lsf's attributes, "manifest" -> the parsed app manifest; a reader that fails gives null."""
    def get(name: str, parse: Callable[[Any], Any]) -> Any:
        try:
            return parse(files[name]())
        except Exception:  # noqa: BLE001 - a missing or unreadable file: unknown
            return None
    mode, is_karmic = karmic(rpc)
    return {"resolution": get("graphics", lambda t: resolution(graphic_entries(t))),
            "ui_scale": get("config", lambda a: ui_scale(config_entries(a))),
            "language": get("manifest", language), "difficulty": None, "roll_mode": mode, "karmic_dice": is_karmic}


def apply_seed(rpc: Rpc, seed: int) -> dict:
    """scn.seed once at the run's start: the mod records it and writes it again after every load."""
    return rpc("scn.seed", {"seed": int(seed)}) or {}


def undo(rpc: Rpc) -> dict:
    """scn.undo at the run's end: the seed record and renames go (also after a load)."""
    return rpc("scn.undo", {}) or {}


def profile_files(profile_dir: Path, manifest: Path, attributes: Callable[[bytes], list]) -> dict:
    """The readers readout() takes, from the Larian profile folder and Steam's app manifest."""
    import steam_paths
    return {"graphics": lambda: (profile_dir / "graphicSettings.lsx").read_text(errors="replace"),
            "config": lambda: attributes((profile_dir / "PlayerProfiles" / "Public" / "config.lsf").read_bytes()),
            "manifest": lambda: steam_paths.parse_vdf(manifest.read_text(errors="replace"))}

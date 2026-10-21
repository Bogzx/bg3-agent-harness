"""playthroughs/<pt>/playthrough.toml: one playthrough's settings, the single file the app edits.

    v = 1
    name = "ilse"
    difficulty = "Explorer"            # "" (the game's default, Explorer) | Explorer | Balanced | Tactician
    [agent]
    profile = ""                       # a client profile by name (profiles/<name>.toml); "" = client/model below
    client = "claude"                  # claude | codex | gemini | pi
    model = "opus"
    effort = ""                        # "" = the client's default | low | medium | high | xhigh | max
    [character]                        # today's profile.json "character", same keys
    brief = "a pious, short-tempered dwarf cleric"   # "" = no character phase
    roleplay = "strict"                # strict | light
    name = ""
    constraints = ""
    [session]
    minutes = 60.0                     # per play session
    hours = 0.0                        # total budget; 0 = until stopped or the game ends
    max_cost_usd = 0.0                 # cost cap per session run (play.py --max-cost); required for pi profiles
    [keeper]
    enabled = true
    model = ""                         # "" = the play model
    effort = "low"
    minutes = 5.0
    [recording]
    enabled = true
    fps = 15
    height = 720

Secrets never go here (keys come from the machine's .env). Parsing, validation and writing are pure functions over
text and dicts; load/save/migrate are the only file I/O. Unknown keys are errors, so a typo is not silently ignored.

Migration: profile.json (runner/play.py load_profile) maps onto `difficulty` and `[character]`; `to_profile` gives
back exactly what load_profile returns for it. `parse_profile_json` is the single source of that parse;
runner/play.py load_profile still has its own copy until the harness owner points it here.
"""

from __future__ import annotations

import json
import math
import os
import re
import tomllib
from dataclasses import asdict, dataclass, field, fields, replace
from pathlib import Path
from typing import Any

from .jsontypes import is_obj

VERSION = 1
FILENAME = "playthrough.toml"
PROFILE_FILENAME = "profile.json"

CLIENTS = ("claude", "codex", "gemini", "pi")
EFFORTS = ("low", "medium", "high", "xhigh", "max")  # runner/play.py EFFORTS
ROLEPLAY_MODES = ("strict", "light")  # runner/play.py ROLEPLAY_MODES
DIFFICULTIES = ("Explorer", "Balanced", "Tactician")  # runner/play.py DIFFICULTIES (new_game's)
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class ConfigError(ValueError):
    """A playthrough.toml (or profile.json) that can't be used; the message names the key."""


@dataclass(frozen=True)
class Agent:
    profile: str = ""
    client: str = "claude"
    model: str = "opus"
    effort: str = ""


@dataclass(frozen=True)
class Character:
    brief: str = ""
    roleplay: str = "strict"
    name: str = ""
    constraints: str = ""


@dataclass(frozen=True)
class Session:
    minutes: float = 60.0
    hours: float = 0.0
    max_cost_usd: float = 0.0


@dataclass(frozen=True)
class Keeper:
    enabled: bool = True
    model: str = ""
    effort: str = "low"
    minutes: float = 5.0


@dataclass(frozen=True)
class Recording:
    enabled: bool = True
    fps: int = 15
    height: int = 720


@dataclass(frozen=True)
class PlaythroughConfig:
    name: str
    difficulty: str = ""
    agent: Agent = field(default_factory=Agent)
    character: Character = field(default_factory=Character)
    session: Session = field(default_factory=Session)
    keeper: Keeper = field(default_factory=Keeper)
    recording: Recording = field(default_factory=Recording)
    v: int = VERSION


SECTIONS: dict[str, type] = {
    "agent": Agent,
    "character": Character,
    "session": Session,
    "keeper": Keeper,
    "recording": Recording,
}
ENUMS: dict[str, tuple[str, ...]] = {
    "difficulty": ("", *DIFFICULTIES),
    "agent.client": CLIENTS,
    "agent.effort": ("", *EFFORTS),
    "character.roleplay": ROLEPLAY_MODES,
    "keeper.effort": EFFORTS,
}


# ---------------------------------------------------------------- validate


def _typed(key: str, value: Any, default: Any) -> Any:
    """`value` checked against the type of the field's default (bool is not an int; an int is a float)."""
    if isinstance(default, bool):
        ok = isinstance(value, bool)
    elif isinstance(default, int):
        ok = isinstance(value, int) and not isinstance(value, bool)
    elif isinstance(default, float):
        ok = isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
        value = float(value) if ok else value
    else:
        ok = isinstance(value, str)
    if not ok:
        raise ConfigError(f"{key}: expected {type(default).__name__}, got {value!r}")
    if key in ENUMS and value not in ENUMS[key]:
        raise ConfigError(f"{key}: {value!r} is not one of {', '.join(repr(e) for e in ENUMS[key])}")
    return value


def _section(prefix: str, cls: type, raw: Any) -> Any:
    if not is_obj(raw):
        raise ConfigError(f"{prefix}: expected a table, got {raw!r}")
    names = {f.name for f in fields(cls)}
    unknown = sorted(set(raw) - names)
    if unknown:
        raise ConfigError(f"{prefix}: unknown key(s) {', '.join(unknown)}")
    default = cls()
    return replace(default, **{k: _typed(f"{prefix}.{k}", v, getattr(default, k)) for k, v in raw.items()})


def validate(data: dict[str, Any]) -> PlaythroughConfig:
    """A parsed TOML document -> a checked config. Missing keys take their defaults; `v` and `name` are required."""
    if data.get("v") != VERSION:
        raise ConfigError(f"v: expected {VERSION}, got {data.get('v')!r}")
    name = data.get("name")
    if not isinstance(name, str) or not NAME_RE.match(name):
        raise ConfigError(f"name: expected a playthrough name ({NAME_RE.pattern}), got {name!r}")
    unknown = sorted(set(data) - {"v", "name", "difficulty", *SECTIONS})
    if unknown:
        raise ConfigError(f"unknown key(s) {', '.join(unknown)}")
    cfg = PlaythroughConfig(
        name=name,
        difficulty=_typed("difficulty", data.get("difficulty", ""), ""),
        **{s: _section(s, cls, data.get(s, {})) for s, cls in SECTIONS.items()},
    )
    if cfg.agent.profile and not NAME_RE.match(cfg.agent.profile):
        raise ConfigError(f"agent.profile: expected a profile name, got {cfg.agent.profile!r}")
    if not cfg.agent.model.strip() and not cfg.agent.profile:
        raise ConfigError("agent.model: empty (set a model or a client profile)")
    if cfg.session.minutes <= 0:
        raise ConfigError(f"session.minutes: must be > 0, got {cfg.session.minutes}")
    if cfg.session.hours < 0:
        raise ConfigError(f"session.hours: must be >= 0 (0 = until stopped), got {cfg.session.hours}")
    if cfg.session.max_cost_usd < 0:
        raise ConfigError(f"session.max_cost_usd: must be >= 0 (0 = no cap), got {cfg.session.max_cost_usd}")
    if cfg.keeper.minutes <= 0:
        raise ConfigError(f"keeper.minutes: must be > 0, got {cfg.keeper.minutes}")
    if not 1 <= cfg.recording.fps <= 60:
        raise ConfigError(f"recording.fps: must be 1-60, got {cfg.recording.fps}")
    if not 144 <= cfg.recording.height <= 2160:
        raise ConfigError(f"recording.height: must be 144-2160, got {cfg.recording.height}")
    return cfg


def loads(text: str) -> PlaythroughConfig:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"not valid TOML: {e}") from e
    return validate(data)


# ---------------------------------------------------------------- write


def _toml_value(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    return json.dumps(v, ensure_ascii=False)  # JSON string escapes are valid TOML basic-string escapes


def dumps(cfg: PlaythroughConfig) -> str:
    """The config as TOML, every key written (so the file documents itself); loads(dumps(c)) == c."""
    validate(to_dict(cfg))
    lines = [f"v = {cfg.v}", f"name = {_toml_value(cfg.name)}", f"difficulty = {_toml_value(cfg.difficulty)}"]
    for s in SECTIONS:
        lines += ["", f"[{s}]"] + [f"{k} = {_toml_value(v)}" for k, v in asdict(getattr(cfg, s)).items()]
    return "\n".join(lines) + "\n"


def to_dict(cfg: PlaythroughConfig) -> dict[str, Any]:
    return asdict(cfg)


# ---------------------------------------------------------------- profile.json (migration)


def parse_profile_json(text: str) -> dict[str, Any]:
    """runner/play.py load_profile on the file's text (play.py still has a copy: harness-owned):
    {"character": {"brief", "name"?, "constraints"?, "roleplay"}, "difficulty"?}.
    Broken JSON, or a character without a brief, means no character."""
    try:
        raw = json.loads(text)
    except ValueError:
        return {}
    if not is_obj(raw):
        return {}
    out = dict(raw)
    c = raw.get("character")
    if is_obj(c) and isinstance(c.get("brief"), str) and c["brief"].strip():
        out["character"] = dict(c, roleplay=c.get("roleplay") if c.get("roleplay") in ROLEPLAY_MODES else "strict")
    else:
        out.pop("character", None)
    if out.get("difficulty") not in DIFFICULTIES:
        out.pop("difficulty", None)
    return out


def from_profile(profile: dict[str, Any], name: str) -> PlaythroughConfig:
    """A load_profile dict -> a config with that character and difficulty and defaults elsewhere. Keys the config
    has no place for are an error rather than dropped."""
    unknown = sorted(set(profile) - {"character", "difficulty"})
    if unknown:
        raise ConfigError(f"profile.json: unknown key(s) {', '.join(unknown)}")
    c: dict[str, Any] = profile.get("character") or {}

    unknown = sorted(set(c) - {f.name for f in fields(Character)})
    if unknown:
        raise ConfigError(f"profile.json character: unknown key(s) {', '.join(unknown)}")
    return validate({"v": VERSION, "name": name, "difficulty": profile.get("difficulty", ""), "character": c})


def to_profile(cfg: PlaythroughConfig) -> dict[str, Any]:
    """The config's character and difficulty in load_profile's shape (what play.py's prompt builders take)."""
    out: dict[str, Any] = {}
    c = cfg.character
    if c.brief.strip():
        out["character"] = {"brief": c.brief, "roleplay": c.roleplay}
        out["character"].update({k: v for k, v in (("name", c.name), ("constraints", c.constraints)) if v})
    if cfg.difficulty:
        out["difficulty"] = cfg.difficulty
    return out


# ---------------------------------------------------------------- files


def load(pt_dir: Path) -> PlaythroughConfig:
    """playthrough.toml, else profile.json migrated in memory (nothing written), else the defaults."""
    toml_path = pt_dir / FILENAME
    if toml_path.exists():
        return loads(toml_path.read_text(encoding="utf-8"))
    prof_path = pt_dir / PROFILE_FILENAME
    if prof_path.exists():
        return from_profile(parse_profile_json(prof_path.read_text(encoding="utf-8")), pt_dir.name)
    return validate({"v": VERSION, "name": pt_dir.name})


def save(pt_dir: Path, cfg: PlaythroughConfig) -> Path:
    """Write playthrough.toml atomically (temp file + rename) and return its path."""
    text = dumps(cfg)
    path = pt_dir / FILENAME
    tmp = path.with_name(f".{FILENAME}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
    return path


def migrate(pt_dir: Path) -> Path | None:
    """Write playthrough.toml from profile.json when only profile.json exists; profile.json is left in place (the
    runner still reads it). Returns the new file, or None when there was nothing to do."""
    if (pt_dir / FILENAME).exists() or not (pt_dir / PROFILE_FILENAME).exists():
        return None
    return save(pt_dir, load(pt_dir))

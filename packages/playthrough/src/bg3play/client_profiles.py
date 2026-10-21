"""The client profiles a playthrough can play with: profiles/*.toml in the format of runner/profiles.py (the pi
worker's: client, model, effort; for pi also the endpoint and model definition). The Launcher lists them; the
session runner turns the chosen one into play.py flags (adapters/legacy_cli.py).

Parsing and validation stay runner/profiles.py's (reached through adapters/legacy_paths.py until the harness package
exposes it); this module only lists and summarises. A profile that fails to parse is listed with its error, so the
Launcher can say why it can't be picked. Keys never live in a profile, only the name of the env var holding one.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from .adapters import legacy_paths


def runner_load(path: Path) -> Any:
    """runner/profiles.py load (raises its ProfileError, a ValueError, for a bad profile)."""
    return legacy_paths.load("profiles").load(path)


def summary(name: str, p: Any) -> dict[str, Any]:
    ep = p.endpoint
    return {
        "name": name,
        "client": p.client,
        "model": p.model,
        "effort": p.effort or "",
        "provider": ep.provider if ep else None,
        "api_key_env": ep.api_key_env if ep else None,
        "error": None,
    }


def read_profiles(directory: Path | None, load: Callable[[Path], Any] = runner_load) -> list[dict[str, Any]]:
    """One row per profiles/*.toml, by name: client/model/effort/provider, or the parse error."""
    if directory is None or not directory.is_dir():
        return []
    rows: list[dict[str, Any]] = []
    for f in sorted(directory.glob("*.toml")):
        try:
            rows.append(summary(f.stem, load(f)))
        except (OSError, ValueError) as e:
            rows.append(
                {
                    "name": f.stem,
                    "client": None,
                    "model": None,
                    "effort": "",
                    "provider": None,
                    "api_key_env": None,
                    "error": str(e).replace(str(f), f.name),
                }
            )
    return rows


def load_one(directory: Path, name: str, load: Callable[[Path], Any] = runner_load) -> Any:
    """The parsed profile `name` (raises ValueError when it is missing or bad)."""
    path = directory / f"{name}.toml"
    if not path.is_file():
        raise ValueError(f"no client profile {name!r} in {directory}")
    return load(path)

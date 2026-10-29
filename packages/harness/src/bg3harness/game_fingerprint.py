"""The pinned BG3 build's file fingerprint: how a game copy that Steam didn't install (download_depot, docs/
GAME-UPDATES.md) is recognised as the pinned build. Stdlib only, read-only.

    python3 tools/game_fingerprint.py make  [--game DIR] [--file REL]...   # print this install's fingerprint
    python3 tools/game_fingerprint.py check [--game DIR]                   # does DIR match the pinned build's?

A fingerprint is {"sha256": <digest>, "files": [{"path", "bytes", "sha256"}]}: the game executable and the main game
data paks, by path relative to the game dir, with the digest a sha256 over the canonical JSON of the files sorted by
path. tools/dev/game_fingerprints.json keeps one per pinned buildid (old ones stay, like the depot table). The build
check (preflight, tools/ci/game_build.py, the run pins) accepts a game whose appmanifest names the pinned buildid, or
whose files match the pinned buildid's fingerprint.

The large asset paks (textures, sounds, models: ~140 GB) are left out so a check reads ~2 GB, not the whole game; a
build change touches the executable and the game data paks. A copy whose sizes differ is refused before any hashing.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

from bg3harness import steam_paths

REPO = Path(__file__).resolve().parents[4]
PINS = REPO / "tools" / "dev" / "game_fingerprints.json"
EXE = "bin/bg3"
DATA_PAKS = ("Data/Game.pak", "Data/GustavX.pak", "Data/Shared.pak")  # plus every Data/Patch*.pak (the hotfixes)


def default_files(game: Path) -> list[str]:
    """What `make` fingerprints: the executable, the main data paks and every hotfix pak the install has."""
    patches = sorted(f"Data/{p.name}" for p in (game / "Data").glob("Patch*.pak"))
    return [EXE, *DATA_PAKS, *patches]


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def digest(files: list[dict[str, Any]]) -> str:
    entries = sorted(({"path": f["path"], "bytes": f["bytes"], "sha256": f["sha256"]} for f in files),
                     key=lambda f: f["path"])  # fmt: skip
    return hashlib.sha256(json.dumps(entries, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def make(game: Path, rels: list[str]) -> dict[str, Any]:
    files = [{"path": r, "bytes": (game / r).stat().st_size, "sha256": sha256_file(game / r)} for r in sorted(rels)]
    return {"sha256": digest(files), "files": files}


def load(path: Path = PINS) -> dict[str, dict[str, Any]]:
    """{buildid: fingerprint}; {} when the file is missing or unreadable."""
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return cast(dict[str, dict[str, Any]], data) if isinstance(data, dict) else {}


def problems(game: Path, want: Mapping[str, Any]) -> list[str]:
    """Why game's files are not `want`'s; [] when they match. Sizes first: a different copy is refused unhashed."""
    files = cast(list[dict[str, Any]], want.get("files") or [])
    if not files or digest(files) != want.get("sha256"):
        return ["the recorded fingerprint is malformed (its sha256 isn't the digest of its files)"]
    out: list[str] = []
    for f in files:
        path = game / str(f["path"])
        try:
            size = path.stat().st_size
        except OSError:
            out.append(f"no {f['path']}")
            continue
        if size != f["bytes"]:
            out.append(f"{f['path']} is {size} bytes, the pinned build's {f['bytes']}")
    if out:
        return out
    return [f"{f['path']} differs (sha256)" for f in files if sha256_file(game / str(f["path"])) != f["sha256"]]


def installed_build(pinned: str, game: Path, manifest_build: str, pins: Path = PINS) -> tuple[str, str]:
    """(the build the game dir holds, how it was told): the appmanifest's buildid when it is the pinned one, else the
    pinned buildid when the files match its fingerprint, else the appmanifest's (maybe "")."""
    if manifest_build == pinned:
        return manifest_build, "appmanifest"
    want = load(pins).get(pinned)
    if want and not problems(game, want):
        return pinned, "fingerprint"
    return manifest_build, "appmanifest"


def machine_build(env: Mapping[str, str], repo: Path = REPO) -> tuple[str, str]:
    """installed_build for the game a run on this machine plays: the pin in repo's tools/dev/first_launch.sh, the game
    dir and appmanifest tools/steam_paths.py finds from env, repo's tools/dev/game_fingerprints.json."""
    from bg3harness import preflight  # it imports this module

    pinned = preflight.pinned_build(repo)
    game = steam_paths.game_dir(env)  # pyright: ignore[reportUnknownMemberType]
    listed = steam_paths.buildid(env)  # pyright: ignore[reportUnknownMemberType]
    return installed_build(pinned, game, listed, repo / "tools" / "dev" / "game_fingerprints.json")


def main(argv: list[str] | None = None, env: Mapping[str, str] = os.environ) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=("make", "check"))
    ap.add_argument("--game", type=Path, help="the game dir (default: tools/steam_paths.py game)")
    ap.add_argument("--file", action="append", help="make: a path relative to the game dir (default: exe + main paks)")
    ap.add_argument("--build", help="check: the buildid whose fingerprint to compare (default: the pin)")
    ap.add_argument("--pins", type=Path, default=PINS)
    a = ap.parse_args(argv)
    game = cast(Path | None, a.game) or steam_paths.game_dir(env)  # pyright: ignore[reportUnknownMemberType]
    if a.cmd == "make":
        print(json.dumps(make(game, a.file or default_files(game)), indent=1))
        return 0
    from bg3harness import preflight  # its pinned_build reads tools/dev/first_launch.sh's WANT_BUILD

    build = a.build or preflight.pinned_build(REPO)
    want = load(a.pins).get(build)
    if not want:
        print(f"no fingerprint for build {build} in {a.pins}")
        return 3
    bad = problems(game, want)
    print(f"{game}: " + ("; ".join(bad) if bad else f"matches build {build}'s fingerprint {want['sha256'][:16]}"))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())

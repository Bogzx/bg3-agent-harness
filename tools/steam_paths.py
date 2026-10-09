#!/usr/bin/env python3
"""Finds the BG3 install (and Steam's sniper runtime) on this machine. Stdlib only, read-only.

    python3 tools/steam_paths.py game        # <library>/steamapps/common/Baldurs Gate 3
    python3 tools/steam_paths.py data        # .../Baldurs Gate 3/Data
    python3 tools/steam_paths.py sniper      # .../SteamLinuxRuntime_sniper
    python3 tools/steam_paths.py manifest    # .../steamapps/appmanifest_1086940.acf
    python3 tools/steam_paths.py buildid     # the installed buildid from that manifest

Order, for the game: $BG3_DIR, then $BG3AI_GAME_DIR (older name), then the Steam library that
libraryfolders.vdf lists for app 1086940, then the old default ~/.local/share/Steam/steamapps/common.
The sniper runtime (app 1628350) can sit in a different library than the game: $BG3AI_SNIPER_DIR, then
libraryfolders.vdf, then next to the game. Prints the path even when it does not exist (the caller
reports what is missing); exits 1 only for an unknown key.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

GAME_APPID = "1086940"
GAME_DIRNAME = "Baldurs Gate 3"
SNIPER_APPID = "1628350"
SNIPER_DIRNAME = "SteamLinuxRuntime_sniper"


def steam_roots(home: Path) -> list[Path]:
    """Steam install roots in the order Steam's own symlinks and common distros use."""
    roots = [home / ".steam/steam", home / ".steam/root", home / ".steam/debian-installation",
             home / ".local/share/Steam", home / ".var/app/com.valvesoftware.Steam/.local/share/Steam"]
    if os.environ.get("STEAM_ROOT"):
        roots.insert(0, Path(os.environ["STEAM_ROOT"]))
    return roots


def vdf_files(home: Path) -> list[Path]:
    out: list[Path] = []
    for root in steam_roots(home):
        for rel in ("config/libraryfolders.vdf", "steamapps/libraryfolders.vdf"):
            p = root / rel
            if p.is_file() and p.resolve() not in {q.resolve() for q in out}:
                out.append(p)
    return out


def parse_vdf(text: str) -> dict:
    """Minimal KeyValues (text VDF) parser: quoted keys/values and nested braces."""
    tokens = re.findall(r'"((?:[^"\\]|\\.)*)"|([{}])', text)
    stack: list[dict] = [{}]
    key = None
    for quoted, brace in tokens:
        if brace == "{":
            child: dict = {}
            stack[-1][key if key is not None else ""] = child
            stack.append(child)
            key = None
        elif brace == "}":
            if len(stack) > 1:
                stack.pop()
            key = None
        elif key is None:
            key = quoted
        else:
            stack[-1][key] = quoted.replace("\\\\", "\\")
            key = None
    return stack[0]


def libraries(home: Path) -> list[Path]:
    """Library folders from every libraryfolders.vdf found, plus the Steam roots themselves."""
    libs: list[Path] = []
    for vdf in vdf_files(home):
        try:
            data = parse_vdf(vdf.read_text(errors="replace"))
        except OSError:
            continue
        folders = data.get("libraryfolders") or data.get("LibraryFolders") or {}
        for v in folders.values():
            path = v.get("path") if isinstance(v, dict) else v  # old format: "1" "/path"
            if isinstance(path, str) and path.startswith("/"):
                libs.append(Path(path))
    libs += [r for r in steam_roots(home) if (r / "steamapps").is_dir()]
    seen, out = set(), []
    for lib in libs:
        key = str(lib.resolve()) if lib.exists() else str(lib)
        if key not in seen:
            seen.add(key)
            out.append(lib)
    return out


def installdir(manifest: Path) -> str | None:
    try:
        return parse_vdf(manifest.read_text(errors="replace")).get("AppState", {}).get("installdir")
    except OSError:
        return None


def find_app(appid: str, dirname: str, home: Path) -> Path | None:
    """The app's folder in the library whose appmanifest lists it, else any library holding dirname."""
    libs = libraries(home)
    for lib in libs:
        m = lib / "steamapps" / f"appmanifest_{appid}.acf"
        if m.is_file():
            return lib / "steamapps/common" / (installdir(m) or dirname)
    for lib in libs:
        if (lib / "steamapps/common" / dirname).is_dir():
            return lib / "steamapps/common" / dirname
    return None


def game_dir(env=os.environ, home: Path | None = None) -> Path:
    home = home or Path.home()
    for var in ("BG3_DIR", "BG3AI_GAME_DIR"):
        if env.get(var):
            return Path(env[var])
    return find_app(GAME_APPID, GAME_DIRNAME, home) or home / ".local/share/Steam/steamapps/common" / GAME_DIRNAME


def sniper_dir(env=os.environ, home: Path | None = None) -> Path:
    home = home or Path.home()
    if env.get("BG3AI_SNIPER_DIR"):
        return Path(env["BG3AI_SNIPER_DIR"])
    found = find_app(SNIPER_APPID, SNIPER_DIRNAME, home)
    return found or game_dir(env, home).parent / SNIPER_DIRNAME


def manifest(env=os.environ, home: Path | None = None) -> Path:
    return game_dir(env, home).parent.parent / f"appmanifest_{GAME_APPID}.acf"


def buildid(env=os.environ, home: Path | None = None) -> str:
    try:
        return parse_vdf(manifest(env, home).read_text(errors="replace")).get("AppState", {}).get("buildid", "")
    except OSError:
        return ""


def main(argv: list[str]) -> int:
    what = argv[1] if len(argv) > 1 else "game"
    fn = {"game": game_dir, "data": lambda: game_dir() / "Data", "sniper": sniper_dir,
          "manifest": manifest, "buildid": buildid}.get(what)
    if fn is None:
        print(f"usage: {argv[0]} game|data|sniper|manifest|buildid", file=sys.stderr)
        return 1
    print(fn())
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

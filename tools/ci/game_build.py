#!/usr/bin/env python3
"""Is the installed BG3 build the pinned one? Read only, offline: it reads Steam's appmanifest and the pin, nothing
else (no Steam, no network). docs/GAME-UPDATES.md says when to run it and what to do on each answer.

    python3 tools/ci/game_build.py [--manifest PATH] [--pin-file PATH]

The installed build is AppState.buildid in appmanifest_1086940.acf (tools/steam_paths.py finds it; --manifest names
one). The pin is WANT_BUILD in tools/dev/first_launch.sh (--pin-file names another file with that line). Exit codes:

    0  the installed build is the pin, and no update is pending
    1  the installed build is not the pin (a new build is installed: run the procedure before moving the pin)
    2  the installed build is the pin, but Steam has an update pending (StateFlags UpdateRequired, or a TargetBuildID
       that differs): Steam will change the build when it next updates, so keep it offline
    3  can't tell: no manifest, no buildid in it, or no WANT_BUILD in the pin file
    64 bad arguments
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
import steam_paths  # noqa: E402

PIN_FILE = ROOT / "tools/dev/first_launch.sh"
STATE_UPDATE_REQUIRED = 2  # EAppState bit (packages/harness/src/bg3harness/preflight.py has the same)
SAME, OTHER_BUILD, UPDATE_PENDING, UNKNOWN, USAGE = 0, 1, 2, 3, 64


@dataclass(frozen=True)
class Verdict:
    code: int
    line: str


def read_pin(text: str) -> str | None:
    """WANT_BUILD=<digits> at the start of a line, else None (no fallback: a lost pin is an answer of its own)."""
    m = re.search(r"^WANT_BUILD=(\d+)\s*$", text, re.MULTILINE)
    return m.group(1) if m else None


def compare(app_state: dict[str, Any], pin: str | None) -> Verdict:
    """The verdict for a manifest's AppState against the pin. Pure."""
    if pin is None:
        return Verdict(UNKNOWN, "no WANT_BUILD pin found")
    build = str(app_state.get("buildid", "") or "")
    if not build:
        return Verdict(UNKNOWN, "no buildid in the manifest")
    if build != pin:
        return Verdict(OTHER_BUILD, f"installed buildid {build} is not the pinned {pin}")
    target = str(app_state.get("TargetBuildID", "") or "")
    try:
        flags = int(app_state.get("StateFlags", "0") or 0)
    except ValueError:
        flags = 0
    if flags & STATE_UPDATE_REQUIRED or target not in ("", "0", build):
        return Verdict(
            UPDATE_PENDING,
            f"installed buildid {build} = the pin, but an update is pending (StateFlags {flags}, TargetBuildID "
            f"{target or '-'}): keep Steam offline",
        )
    return Verdict(SAME, f"installed buildid {build} = the pin")


def check(manifest: Path, pin_file: Path) -> Verdict:
    try:
        pin = read_pin(pin_file.read_text(errors="replace"))
    except OSError as e:
        return Verdict(UNKNOWN, f"can't read the pin file {pin_file}: {e.strerror}")
    try:
        text = manifest.read_text(errors="replace")
    except OSError as e:
        return Verdict(UNKNOWN, f"can't read the manifest {manifest}: {e.strerror}")
    vdf: dict[str, Any] = steam_paths.parse_vdf(text)  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
    state = vdf.get("AppState")
    return compare(cast(dict[str, Any], state) if isinstance(state, dict) else {}, pin)


class _Parser(argparse.ArgumentParser):
    def error(self, message: str):  # argparse exits 2, which is UPDATE_PENDING here
        self.print_usage(sys.stderr)
        print(f"{self.prog}: error: {message}", file=sys.stderr)
        sys.exit(USAGE)


def main(argv: list[str]) -> int:
    ap = _Parser(prog="game_build.py", description="Compare the installed BG3 buildid with the pin (read only).")
    ap.add_argument("--manifest", type=Path, help="appmanifest_1086940.acf (default: found by tools/steam_paths.py)")
    ap.add_argument("--pin-file", type=Path, default=PIN_FILE, help="file with the WANT_BUILD= line")
    a = ap.parse_args(argv)
    manifest = cast(Path | None, a.manifest) or steam_paths.manifest()  # pyright: ignore[reportUnknownMemberType]
    v = check(manifest, cast(Path, a.pin_file))
    print(v.line)
    return v.code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

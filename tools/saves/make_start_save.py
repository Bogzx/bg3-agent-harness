"""Make a neutral start save (Phase E, research/ovh/bench.md "Start saves to make") from the game that is up.

    uv run --project bridge python tools/saves/make_start_save.py <name> [--owner Tav] [--store ~/bg3-start-saves]

For the live worker. The game runs with BG3AI_SCENARIO_SETUP=1 and has a PROBE- copy loaded (never an original,
STRESS-TEST.md), already moved to the scenario's start spot. Then, through the mod's scn.* ops (Server/Scenario.lua):

  1. checks: scenario setup on, out of combat, no dialogue, the game screen up, difficulty Balanced (read only: the
     mod can't set it; change it in Options > Gameplay and run again);
  2. Karmic Dice off (scn.karmic {set=false}, read back), the avatar renamed to --owner (scn.rename_avatar);
  3. saves as SCN-<name> through the bridge's save path (bg3ai.tools.saving: the save menus, the disk wait) and
     checks the new folder's owner is --owner (the folder name comes from the avatar's name);
  4. registers the folder: `python -m bg3bench.authoring register <folder> <store> <name>` (a subprocess, in the
     bench package's project).
Karmic Dice goes back to what it was once the save is written (or the run fails). The avatar's new name stays in
the loaded game: it's a throwaway PROBE copy. Exit 0 registered, 1 refused or failed (the reason on stderr).
"""

from __future__ import annotations

import sys

# Run as a script, this folder is sys.path[0], and its inspect.py would shadow the stdlib's (dataclasses imports it).
if sys.path and sys.path[0] and __file__.startswith(sys.path[0]) and __name__ == "__main__":
    del sys.path[0]

import argparse
import os
import re
import subprocess
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, Protocol

REPO = Path(__file__).resolve().parents[2]
NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,35}$")  # SCN-<name> stays within the bridge's 40-char save label
OWNER_RE = re.compile(r"^[A-Z][a-z]{1,15}$")  # bg3bench.authoring.neutral_name_problems: one capitalised word
DIFFICULTY = "Balanced"


class Rpc(Protocol):
    def __call__(self, op: str, args: dict[str, Any] | None = None, context: str = "server") -> Any: ...


class RefusedError(Exception):
    """The game isn't in a state to make the save, or a step didn't take."""


def check_ready(rpc: Rpc) -> None:
    """Scenario setup on, out of combat and dialogue, the game screen up, Balanced."""
    rpc("scn.status")  # refused without BG3AI_SCENARIO_SETUP=1: that error says so
    st = rpc("state.status")
    if st.get("in_combat"):
        raise RefusedError("in combat: end the fight (or reload) and run again")
    if st.get("in_dialogue"):
        raise RefusedError("a dialogue is open: finish it and run again")
    screen = (rpc("ui.state", context="client") or {}).get("screen")
    if screen != "game":
        raise RefusedError(f"{screen!r} on screen: close it so the game screen is up")
    diff = rpc("scn.difficulty")
    if diff.get("difficulty") != DIFFICULTY:
        raise RefusedError(
            f"difficulty is {diff.get('difficulty')!r} ({diff.get('rulesets')}): set {DIFFICULTY} in Options > "
            "Gameplay (the mod can't set it)"
        )


def make(
    rpc: Rpc,
    save: Callable[[str], Path],
    register: Callable[[Path], None],
    name: str,
    owner: str = "Tav",
    log: Callable[[str], None] = print,
) -> Path:
    """Steps 1-4 (module docstring); returns the save folder written. Karmic is restored on every way out."""
    if not NAME_RE.match(name):
        raise RefusedError(f"scenario name {name!r}: lowercase letters, digits and _ (start with a letter, <= 36)")
    if not OWNER_RE.match(owner):
        raise RefusedError(f"owner {owner!r}: one plain capitalised word (Tav, Ana)")
    check_ready(rpc)
    label = f"SCN-{name}"
    k = rpc("scn.karmic", {"set": False})
    if k.get("karmic") is not False:
        raise RefusedError(f"Karmic Dice still on after scn.karmic set=false: {k}")
    try:
        log(f"karmic off (was {k.get('before')})")
        a = rpc("scn.rename_avatar", {"name": owner})
        log(f"avatar {a.get('before')!r} -> {a.get('name')!r}")
        folder = save(label)
        if not folder.name.startswith(f"{owner}-") or not folder.name.endswith(f"__{label}"):
            raise RefusedError(
                f"the save was written as {folder.name!r}, not {owner}-<id>__{label}: the rename didn't reach the "
                "save folder's owner (start this scenario from a new game with a Tav instead)"
            )
        log(f"saved {folder}")
    finally:
        if k.get("before") == "WeightedRandom":
            rpc("scn.karmic", {"set": True})
            log("karmic back on")
    register(folder)
    return folder


def register_cmd(folder: Path, store: Path, name: str) -> list[str]:
    return [
        "uv", "run", "--project", str(REPO / "packages" / "bench"),
        "python", "-m", "bg3bench.authoring", "register", str(folder), str(store), name,
    ]  # fmt: skip


def registrar(
    store: Path, name: str, run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run
) -> Callable[[Path], None]:
    def register(folder: Path) -> None:
        r = run(register_cmd(folder, store, name), capture_output=True, text=True, cwd=REPO)
        out = (r.stdout + r.stderr).strip()
        if r.returncode != 0:
            raise RefusedError(f"register failed (exit {r.returncode}): {out}")
        print(out)

    return register


def bridge_save(saving: Any, saver: Any, core: Any) -> Callable[[str], Path]:
    """The bridge's save_game path (save gate open for the save only, the menus, the disk wait), synchronous."""

    def save(label: str) -> Path:
        t0 = time.time()
        core.game.rpc("ui.config", {"save_enabled": True}, context="client")
        try:
            note = saving.SAVER.save(label, sync=True)
        finally:
            core.game.try_rpc("ui.config", {"save_enabled": False}, context="client")
        folder = saver.written_save(core.SAVE_DIR, label, t0)
        if folder is None:
            raise RefusedError(f"no save folder *__{label} in {core.SAVE_DIR}: {note}")
        return folder

    return save


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Make and register a neutral SCN-<name> start save.")
    ap.add_argument("name", help="the scenario name (packages/bench/scenarios/<name>.json)")
    ap.add_argument("--owner", default="Tav", help="the avatar's neutral name (the save folder's owner)")
    ap.add_argument("--store", type=Path, default=Path.home() / "bg3-start-saves", help="the start-save store")
    args = ap.parse_args(argv)
    # The bridge's modules read these at import: its action log goes here, not into a playthrough's runtime/.
    os.environ.setdefault("BG3AI_RUNTIME", str(REPO / "runtime" / "make_start_save"))
    sys.path.insert(0, str(REPO / "bridge" / "src"))
    from bg3ai import saver  # after the env above
    from bg3ai.game import GameError
    from bg3ai.tools import core, saving

    def rpc(op: str, args: dict[str, Any] | None = None, context: str = "server") -> Any:
        return core.game.rpc(op, args, context=context)

    try:
        make(rpc, bridge_save(saving, saver, core), registrar(args.store, args.name), args.name, args.owner)
    except (RefusedError, GameError) as e:
        print(f"make_start_save: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

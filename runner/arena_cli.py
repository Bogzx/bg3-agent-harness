# pyright: basic
"""`runner/play.py arena --arena <name|path> --a <profile> --b <profile> [--out NAME]`: one match on this machine's
game. The arena file is the bench's (bg3bench.arena.load: packages/bench/arenas/<name>.json or a path), each side's
player is the bench's name for its profile (bg3bench.arena_matrix.player: "<client>/<model slug>"); the harness plays
it (bg3harness.arena.live.play -> run_arena), never importing the bench. --out names the run dir
(runtime/sessions/<out>/; default arena-<arena>-<time>). The seed is the arena file's: a match on another seed is a
match of another arena file. Takes runtime/runner.lock like `run`. Exit 0 when the match ended valid, 1 invalid, 2
refused before the game started.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime
from pathlib import Path

import _harness  # noqa: F401  (packages/harness/src, packages/bench/src and bridge/src on sys.path)
from bg3bench import arena as barena
from bg3bench import arena_matrix
from bg3bench.matrix_cli import MatrixFileError, load_profile
from bg3harness.arena import ArenaSpec, ArenaSpecError, Side, scripted_client
from bg3harness.arena.match import ArenaSetupError
from bg3harness.runner import env as renv


def arena_path(ref: str) -> Path:
    """A path as given, else packages/bench/arenas/<name>.json."""
    p = Path(ref)
    return p if p.suffix == ".json" or p.exists() else barena.ARENA_DIR / f"{ref}.json"


def sides(a: str, b: str, allow_unverified: bool) -> dict[str, Side]:
    """Each side's player; the profile `scripted` is the scripted client (no model, player scripted/none, never
    rated)."""
    out = {}
    for side, path in (("A", a), ("B", b)):
        if path == scripted_client.SCRIPTED:
            out[side] = Side(side, scripted_client.PLAYER, path, scripted_client.SCRIPTED, "none")
            continue
        m = load_profile(Path(path), allow_unverified)
        out[side] = Side(side, arena_matrix.player(m), str(path), m["client"], m["slug"])
    return out


def parse(argv: list[str]) -> argparse.Namespace:
    ap = argparse.ArgumentParser(prog="play.py arena", description=(__doc__ or "").split("\n\n")[0])
    ap.add_argument("--arena", required=True, help="an arena name (packages/bench/arenas/<name>.json) or a file")
    ap.add_argument(
        "--a",
        required=True,
        metavar="PROFILE",
        help="side A's client profile (profiles/*.toml, pi), or 'scripted' (no model)",
    )
    ap.add_argument(
        "--b",
        required=True,
        metavar="PROFILE",
        help="side B's client profile (profiles/*.toml, pi), or 'scripted' (no model)",
    )
    ap.add_argument("--out", help="the run's name: runtime/sessions/<out>/ (default arena-<arena>-<time>)")
    ap.add_argument("--allow-unverified", action="store_true", help="play profiles whose status is offline")
    return ap.parse_args(argv)


def main(argv: list[str]) -> int:
    a = parse(argv)
    try:
        spec = ArenaSpec.from_dict(barena.load(arena_path(a.arena)))
        players = sides(a.a, a.b, a.allow_unverified)
    except (barena.ArenaError, ArenaSpecError, MatrixFileError) as e:
        print(f"play.py arena refused: {e}", file=sys.stderr)
        return 2
    run_name = a.out or f"arena-{spec.name}-{datetime.now():%Y%m%d-%H%M%S}"
    import legacy_cli as lcli  # the runner's lock and its Runner (the playthrough parts)
    import legacy_playthrough as lpt
    from bg3harness.arena import live

    lock = lcli.acquire_lock()  # noqa: F841 - held (runtime/runner.lock) until the process exits
    try:
        outcome, run_dir = live.play(spec, players, lpt.Runner, run_name, dict(os.environ))
    except ArenaSetupError as e:
        print(f"play.py arena refused: {e}", file=sys.stderr)
        return 2
    renv.log(f"arena {spec.name}: {run_dir}: end {outcome.end}, invalid {outcome.invalid}")
    return 0 if outcome.invalid is None and outcome.end is not None else 1

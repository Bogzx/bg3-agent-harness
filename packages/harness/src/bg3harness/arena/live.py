# pyright: basic
"""A match on the live game: the runner brings the game up at the arena's save, seeded with Karmic Dice off and the
arena switch on (Options.arena: BG3AI_ARENA=1; the seed and the settings need BG3AI_SCENARIO_SETUP=1), each side gets
its pi client with its own bg3 server env and system text, and run_arena plays the match into the run dir
(runtime/sessions/<run>/): facts.jsonl, rolls.jsonl, A.events.jsonl, B.events.jsonl, the streams and
arena.json (the match's manifest: the arena, both sides' players and profiles, the pins, the outcome).

`make_runner(opt)` builds the run's runner (runner/play.py's Runner, a SessionRunner with the playthrough parts), as
LiveRunHost's does. Cost: both sides' processes are charged to runtime/budget.json (spend.add_spend) as the match
ends.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from bg3harness import client_setup, clients, profiles, spend
from bg3harness.arena import agents as aagents
from bg3harness.arena.match import ArenaOutcome, ArenaSetupError, run_arena
from bg3harness.arena.spec import SIDES, ArenaSpec, Side
from bg3harness.game import io as gio
from bg3harness.game.io import EVAL_TIMEOUT
from bg3harness.runner import agent_group, run_host
from bg3harness.runner import env as renv
from bg3harness.runner import state as rstate

Json = dict[str, Any]


class LiveGame:
    """ArenaGame on the runner's game: mod ops over the debugger connection (a side's go in its envelope)."""

    def __init__(self, r: Any):
        self.r = r

    def rpc(self, op: str, args: dict[str, Any], side: str | None = None) -> Any:
        return gio.rpc(op, args, timeout=EVAL_TIMEOUT, side=side)

    def alive(self) -> bool:
        return bool(self.r.game_alive())


class WallClock:
    def now(self) -> float:
        return time.time()

    def sleep(self, s: float) -> None:
        time.sleep(s)


def options(spec: ArenaSpec, run_name: str) -> rstate.Options:
    """The match's runner options: the arena's save, its seed, Karmic Dice off (checked), the arena switch; no
    keeper, no judge. The players' clients are the match's own (LiveAgents), never the runner's."""
    return rstate.Options(
        run_name,
        None,
        spec.wall_cap_min,
        "arena",
        allow_short=True,
        keeper=False,
        start_save=spec.save,
        client="pi",
        seed=spec.seed,
        scenario_settings={"difficulty": None, "karmic_dice": False, "timing": "turn_based", "avatar_name": None},
        arena=True,
    )


def side_env(spec: ArenaSpec, side: str, prof: profiles.Profile, run_dir: Path) -> dict[str, str]:
    """The env a side's bg3 server gets on top of the runner's: its side, the arena's tools-off list (plus what the
    profile's model can't use), the arena's prompt condition and its own memory dir (nothing fills it)."""
    env = {
        "BG3AI_SIDE": side,
        "BG3AI_TOOLS_OFF": spec.tools_off,
        "BG3AI_PROMPT_CONDITION": str(spec.arena.get("condition") or "rules"),
        "BG3AI_MEMORY_DIR": str(run_dir / f"memory-{side}"),
        "BG3AI_PLAYTHROUGH": f"{run_dir.name}-{side}",
        "BG3AI_KEEPER": "0",
        "BG3_SESSION_MINUTES": f"{spec.wall_cap_min:g}",
    }
    return profiles.tool_env(prof, env)


def side_client(
    spec: ArenaSpec,
    side: str,
    profile_path: str,
    run_dir: Path,
    base_env: Mapping[str, str],
    log: Callable[[str], None],
) -> tuple[aagents.SideClient, Json]:
    """A side's client: its pi profile, its system text and tool list dumped in its env (the arena rules come with
    BG3AI_SIDE), the API key env. Returns it and the side's manifest fields."""
    prof = profiles.load(profile_path)
    if prof.client != "pi":
        raise ArenaSetupError(f"{profile_path}: a match plays pi profiles ({prof.client} given)")
    env = side_env(spec, side, prof, run_dir)
    environ = {**base_env, **env}
    files = client_setup.run_files(run_dir / side, "pi", spec.seed, "turn_based", renv.REPO, log, environ=environ)
    env.update(client_setup.api_key_env(prof, environ, renv.REPO / ".env"))
    client = clients.make("pi", profile=prof, system_file=files["system_file"])
    sc = aagents.SideClient(client, prof.model, prof.effort, env, files["tools_expected"])
    return sc, {
        "profile": profile_path,
        "digest": profiles.digest(prof),
        "model": prof.model,
        "tools_expected": files["tools_expected"],
        "manifest": files["manifest"],
    }


def start_game(r: Any, spec: ArenaSpec) -> None:
    """The game up with the arena's save loaded, the seed and the settings applied and checked."""
    agent_group.reap_leftover(renv.RUNTIME / agent_group.FILE, renv.log)
    try:
        r.refresh_mod_snapshot()
    except OSError as e:
        renv.log(f"mod snapshot refresh failed: {e}; the game keeps {r.mod_rev}'s mod")
    r.ensure_game(first=True)
    if r.state["loaded_save"] != spec.save:
        raise ArenaSetupError(f"the arena's save {spec.save} didn't load (loaded: {r.state['loaded_save']})")
    r.hot_reload_mod()
    r.check_settings()


def play(
    spec: ArenaSpec,
    sides: Mapping[str, Side],
    make_runner: Callable[[rstate.Options], Any],
    run_name: str,
    base_env: Mapping[str, str],
    log: Callable[[str], None] = renv.log,
) -> tuple[ArenaOutcome, Path]:
    """One match on the live game (see the module doc); returns the outcome and the run dir."""
    r = make_runner(options(spec, run_name))
    run_dir = Path(r.state.dir)
    side_clients: dict[str, aagents.SideClient] = {}
    manifest: Json = {
        "arena": spec.name,
        "save": {"name": spec.save, "sha1": spec.save_sha1},
        "seed": spec.seed,
        "sides": {},
    }
    for s in SIDES:
        side_clients[s], fields = side_client(spec, s, sides[s].profile, run_dir, base_env, log)
        manifest["sides"][s] = {"player": sides[s].player, **fields}
    try:
        start_game(r, spec)
        try:
            manifest["pins"] = run_host.machine_pins(base_env)
        except Exception as e:  # the match plays; its manifest says the pins are unknown
            manifest["pins"] = {"error": f"{type(e).__name__}: {e}"}
        agents = aagents.LiveAgents(side_clients, run_dir, base_env, spec.wall_cap_min)
        outcome = run_arena(
            spec,
            sides,
            LiveGame(r),
            agents,
            run_dir,
            WallClock(),
            log=log,
            aborted=lambda: bool(getattr(r, "abort", False)),
            writer=r.facts_writer(),
        )
    finally:
        r.scenario_undo()
    for s in SIDES:
        usd = outcome.cost(s)
        if usd:
            try:
                spend.add_spend(renv.RUNTIME / "budget.json", usd, playthrough=run_name, session=f"arena-{s}")
            except (OSError, spend.BudgetError) as e:
                log(f"arena side {s}: budget update failed ({e}); cost ${usd:.4f}")
    manifest.update(
        end=outcome.end,
        invalid=outcome.invalid,
        costs={s: outcome.cost(s) for s in SIDES},
        exits=aagents.summary(outcome.exits),
    )
    (run_dir / "arena.json").write_text(json.dumps(manifest, indent=1, default=str) + "\n")
    return outcome, run_dir

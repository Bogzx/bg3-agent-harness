# pyright: basic
"""A match on the live game: the runner brings the game up at the arena's save, seeded with Karmic Dice off and the
arena switch on (Options.arena: BG3AI_ARENA=1; the seed and the settings need BG3AI_SCENARIO_SETUP=1), each side gets
its pi client with its own bg3 server env and system text, and run_arena plays the match into the run dir
(runtime/sessions/<run>/): facts.jsonl, rolls.jsonl, A.events.jsonl, B.events.jsonl, the streams and
arena.json (the match's manifest: the arena, both sides' players and profiles, the pins, the outcome).

`make_runner(opt)` builds the run's runner (runner/play.py's Runner, a SessionRunner with the playthrough parts), as
LiveRunHost's does. Cost: a match with a paid side has a max_cost (both sides together; run_arena ends it
over_budget there), and both sides' processes are charged to runtime/budget.json (spend.add_spend) as the match ends.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from bg3harness import client_setup, clients, profiles, spend
from bg3harness import recorder as hrec
from bg3harness.arena import agents as aagents
from bg3harness.arena import scripted_client
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

    def rpc(self, op: str, args: dict[str, Any], side: str | None = None, context: str = "server") -> Any:
        return gio.rpc(op, args, context=context, timeout=EVAL_TIMEOUT, side=side)

    def alive(self) -> bool:
        return bool(self.r.game_alive())


class WallClock:
    def now(self) -> float:
        return time.time()

    def sleep(self, s: float) -> None:
        time.sleep(s)


def options(spec: ArenaSpec, run_name: str) -> rstate.Options:
    """The match's runner options: the arena's save, its seed, Karmic Dice off (checked), the arena switch; no
    keeper, no judge, and no client: the players' clients are the match's own (LiveAgents), the runner plays none."""
    return rstate.Options(
        run_name,
        None,
        spec.wall_cap_min,
        "arena",
        allow_short=True,
        keeper=False,
        start_save=spec.save,
        client=None,
        seed=spec.seed,
        scenario_settings={"difficulty": None, "karmic_dice": False, "timing": "turn_based", "avatar_name": None},
        arena=True,
    )


def side_env(spec: ArenaSpec, side: str, prof: profiles.Profile | None, run_dir: Path) -> dict[str, str]:
    """The env a side's bg3 server gets on top of the runner's: its side, the arena's tools-off list (plus what the
    profile's model can't use; no profile: the scripted client), the arena's prompt condition and its own memory dir
    (nothing fills it)."""
    env = {
        "BG3AI_SIDE": side,
        "BG3AI_TOOLS_OFF": spec.tools_off,
        "BG3AI_PROMPT_CONDITION": str(spec.arena.get("condition") or "rules"),
        "BG3AI_MEMORY_DIR": str(run_dir / f"memory-{side}"),
        "BG3AI_PLAYTHROUGH": f"{run_dir.name}-{side}",
        "BG3AI_KEEPER": "0",
        "BG3_SESSION_MINUTES": f"{spec.wall_cap_min:g}",
    }
    return profiles.tool_env(prof, env) if prof is not None else env


def side_client(
    spec: ArenaSpec,
    side: str,
    profile_path: str,
    run_dir: Path,
    base_env: Mapping[str, str],
    log: Callable[[str], None],
) -> tuple[aagents.SideClient, Json]:
    """A side's client: its pi profile, its system text and tool list dumped in its env (the arena rules come with
    BG3AI_SIDE), the API key env. Returns it and the side's manifest fields. The profile SCRIPTED is the scripted
    client (bg3harness.arena.scripted_client): the side's bg3 server and no model, no system text, no key."""
    if profile_path == scripted_client.SCRIPTED:
        env = side_env(spec, side, None, run_dir)
        sc = aagents.SideClient(scripted_client.ScriptedClient(), "none", None, env)
        return sc, {"profile": profile_path, "client": scripted_client.SCRIPTED, "model": "none"}
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


def budget_refusal(sides: Mapping[str, Side], max_cost: float | None = None) -> str | None:
    """A side on a paid client (spend.PAID_CLIENTS): why the match can't start, else None. It needs a max_cost (the
    match ends over_budget there), and both sides' costs go to runtime/budget.json at the end, so it must be there
    with at least max_cost left (the match's reservation)."""
    paid = [s for s in SIDES if spend.is_paid(sides[s].client)]
    if not paid:
        return None
    if max_cost is None:
        return f"side {' and '.join(paid)} spends money: a match needs a money cap (play.py arena --max-cost USD)"
    try:
        budget = spend.read_budget(renv.RUNTIME / "budget.json")
    except spend.BudgetError as e:
        return str(e)
    if budget is None:
        return (
            f"side {' and '.join(paid)} spends money: a match needs a global budget, runtime/budget.json "
            '({"cap": USD, "spent": 0})'
        )
    left = round(budget["cap"] - budget["spent"], 6)
    if left <= 0:
        return f"the global budget is used up (${budget['spent']:.2f} of ${budget['cap']:g})"
    if left < max_cost:
        return f"the global budget has ${left:.2f} left, less than the match's max_cost ${max_cost:g}"
    return None


def play(
    spec: ArenaSpec,
    sides: Mapping[str, Side],
    make_runner: Callable[[rstate.Options], Any],
    run_name: str,
    base_env: Mapping[str, str],
    log: Callable[[str], None] = renv.log,
    max_cost: float | None = None,
    side_caps: Mapping[str, float] | None = None,
) -> tuple[ArenaOutcome, Path]:
    """One match on the live game (see the module doc); returns the outcome and the run dir. Refused
    (ArenaSetupError) before anything starts when a paid side has no max_cost or the budget hasn't max_cost left
    (budget_refusal), and before the agents start when
    the recording (run_host.bench_recording: on unless BG3AI_BENCH_RECORD=0) can't record: it starts with the game
    up, into <run_dir>/recording/, and stops after the match's run_end with the game still up. side_caps: each side's
    own money cap where it has one (a matrix's per-side reserve; run_arena)."""
    if why := budget_refusal(sides, max_cost):
        raise ArenaSetupError(why)
    try:
        recording = run_host.bench_recording(base_env)
    except ValueError as e:  # an unknown encoder
        raise ArenaSetupError(str(e)) from e
    r = make_runner(options(spec, run_name))
    run_dir = Path(r.state.dir)
    side_clients: dict[str, aagents.SideClient] = {}
    manifest: Json = {
        "arena": spec.name,
        "save": {"name": spec.save, "sha1": spec.save_sha1},
        "seed": spec.seed,
        "max_cost": max_cost,
        "side_caps": dict(side_caps) if side_caps else None,
        "sides": {},
    }
    for s in SIDES:
        try:
            side_clients[s], fields = side_client(spec, s, sides[s].profile, run_dir, base_env, log)
        except client_setup.SetupRefused as e:  # its API key isn't set, its tool list can't be dumped, ...
            raise ArenaSetupError(f"side {s}: {e}") from e
        manifest["sides"][s] = {"player": sides[s].player, **fields}
    rec: hrec.RunRecording | None = None
    try:
        start_game(r, spec)
        if recording is not None:  # with the game up (the recorder records its window)
            rec = recording(run_dir / "recording")
            try:
                rec.start()
            except hrec.RecordingError as e:
                rec = None
                raise ArenaSetupError(str(e)) from e
        try:
            manifest["pins"] = run_host.machine_pins(base_env)
        except Exception as e:  # the match plays; its manifest says the pins are unknown
            manifest["pins"] = {"error": f"{type(e).__name__}: {e}"}
        if rec is not None:
            try:
                rec.check()  # ffmpeg still up: the match plays recorded
            except hrec.RecordingError as e:
                raise ArenaSetupError(str(e)) from e
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
            max_cost=max_cost,
            side_caps=side_caps,
        )
    finally:
        try:
            if rec is not None:
                rec.stop()  # after the match's run_end, with the game still up: the recording covers the match
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

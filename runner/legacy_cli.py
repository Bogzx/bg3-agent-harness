"""The legacy runner's commands (moved from runner/play.py, Phase D.3): run, stop, status, runs, judge,
scenarios, and run's plan preview. runner/play.py parses the arguments and calls these.
"""

from __future__ import annotations

import fcntl
import json
import os
import signal
import subprocess
import sys
import time
import types
import uuid
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import _harness  # noqa: F401  (packages/harness/src and bridge/src on sys.path)

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools" / "memory"))  # memlib, new_playthrough

import legacy_playthrough as lpt
import legacy_series as lser
from bg3bench import pins as bpins
from bg3bench import records as brecords
from bg3bench import resets as bresets
from bg3bench import scenario_v1 as bscenario
from bg3bench import series as bseries
from bg3harness import (
    bg3le_buildinfo,
    client_setup,
    clients,
    display_env,
    judge,
    model_time,
    ramwatch,
    runs,
    sandbox,
    spend,
    steam_paths,
)
from bg3harness.game import io as game
from bg3harness.game.io import DEBUG_PORT
from bg3harness.game.saves import (
    foreign_saves,
    parse_handoff_save,
    pick_save,
    pick_save_why,
    play_windows,
    session_ends,
)
from bg3harness.runner import codewatch as rcode
from bg3harness.runner import env as renv
from bg3harness.runner import judging
from bg3harness.runner import policy as rpolicy
from bg3harness.runner import state as rstate


def acquire_lock():
    renv.RUNTIME.mkdir(parents=True, exist_ok=True)
    f = open(renv.RUNTIME / "runner.lock", "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit("another runner is already active (runtime/runner.lock)") from None
    f.write(str(os.getpid()))
    f.flush()
    return f


def cmd_run(a) -> int:
    scenario = save_sha1 = scenario_settings = None
    if a.scenario and a.scenario_file:
        print("refusing --scenario with --scenario-file: one scenario, not both", file=sys.stderr)
        return 2
    if a.scenario or a.scenario_file:
        flag = "--scenario" if a.scenario else "--scenario-file"
        try:
            if a.scenario:
                scenario = lser.load_scenario_file(a.scenario)
            else:
                scenario, scenario_settings = lser.load_scenario_path(Path(a.scenario_file))
        except bscenario.ScenarioError as e:
            print(f"refusing {flag}: {e}", file=sys.stderr)
            return 2
        refusal, save_sha1 = lser.apply_scenario(a, scenario)
        if refusal:
            print(f"refusing {flag} {a.scenario or a.scenario_file}: {refusal}", file=sys.stderr)
            return 2
    elif not a.playthrough:
        print("run needs --playthrough (or --scenario NAME)", file=sys.stderr)
        return 2
    if a.session_minutes < renv.MIN_SESSION_MINUTES and not a.allow_short_sessions:
        # S001/S002 ran 15-min sessions and both wrapped up within 3 min: start and wrap-up playbooks
        # alone take ~13 min, so a short session is all overhead.
        print(f"refusing --session-minutes {a.session_minutes:g}: sessions under {renv.MIN_SESSION_MINUTES} min are "
              "all start-up and wrap-up (use 60; --allow-short-sessions for harness tests)", file=sys.stderr)
        return 2
    if a.autocompact is not None and not rstate.AUTOCOMPACT_MIN <= a.autocompact <= rstate.AUTOCOMPACT_MAX:
        print(f"refusing --autocompact {a.autocompact}: claude takes {rstate.AUTOCOMPACT_MIN}-{rstate.AUTOCOMPACT_MAX} tokens")
        return 2
    if a.runs is not None and (a.runs < 1 or not a.until):
        print("refusing --runs: it needs N >= 1 and --until MILESTONE (each run ends there)", file=sys.stderr)
        return 2
    if a.restart_run and not a.runs:
        print("refusing --restart-run: it restarts a series run (--runs N)", file=sys.stderr)
        return 2
    if a.run_hours is not None and (a.run_hours <= 0 or not a.until):
        print("refusing --run-hours: it needs a positive value and --until MILESTONE", file=sys.stderr)
        return 2
    if a.max_cost is not None and a.max_cost <= 0:
        print("refusing --max-cost: it needs a positive value", file=sys.stderr)
        return 2
    if a.max_tokens is not None and (a.max_tokens <= 0 or not a.until):
        print("refusing --max-tokens: it needs a positive value and --until MILESTONE", file=sys.stderr)
        return 2
    try:   # --profile: the client, model and effort; pi's API key into this process's env (never logged)
        choice = client_setup.resolve(a.client, a.profile, a.model, a.effort, a.fix_model, not a.no_memory_keeper)
        os.environ.update(client_setup.api_key_env(choice.profile, os.environ, renv.REPO / ".env"))
    except client_setup.SetupRefused as e:
        print(f"refusing: {e}", file=sys.stderr)
        return 2
    a.client = a.client or (choice.client if choice.profile else None)
    a.model, a.effort = choice.model, choice.effort
    if a.max_cost_per_day is not None and a.max_cost_per_day <= 0:
        print("refusing --max-cost-per-day: it needs a positive value", file=sys.stderr)
        return 2
    caps = {"run_hours": a.run_hours, "max_cost": a.max_cost, "max_tokens": a.max_tokens}
    frozen = (lser.series_meta(a.playthrough).get("caps") or (scenario["caps"] if scenario else None)) if a.runs else None
    caps, spend_cap, err, cap_changes = bseries.series_caps(frozen, caps, a.allow_cap_change)   # run 1's or the scenario's
    if err:
        print(f"refusing to run series {a.playthrough}: {err}", file=sys.stderr)
        return 2
    use_judge = bool(a.runs) if a.judge is None else a.judge   # on for a series (--runs / --scenario)
    if use_judge and not a.until:
        print("refusing --judge: it scores a run's record, which needs --until MILESTONE", file=sys.stderr)
        return 2
    judge_model, judge_change = a.judge_model or os.environ.get("BG3AI_JUDGE_MODEL") or judging.DEFAULT_MODEL, None
    if use_judge and a.runs:   # a series' judge is fixed like its caps: scores by another judge don't compare
        judge_model, err, judge_change = bseries.series_judge_model(
            lser.series_meta(a.playthrough).get("judge_model"), a.judge_model or os.environ.get("BG3AI_JUDGE_MODEL"),
            judging.DEFAULT_MODEL, a.allow_judge_change)
        if err:
            print(f"refusing to run series {a.playthrough}: {err}", file=sys.stderr)
            return 2
    client, client_change = a.client or "claude", None
    if a.runs:   # a series plays with one CLI, fixed like its judge; a series from before --client played claude
        meta = lser.series_meta(a.playthrough)
        client, err, client_change = bseries.series_client(meta.get("client") or ("claude" if meta else None),
                                                        a.client, "claude", a.allow_client_change)
        if err:
            print(f"refusing to run series {a.playthrough}: {err}", file=sys.stderr)
            return 2
    try:   # a paid client: a global budget (--budget creates it) and a per-run cap, cut to what it has left
        usd = getattr(a, "budget", None)
        budget = spend.ensure_budget(renv.RUNTIME / "budget.json", usd, write=False) if spend.is_paid(client) else None
        cap, err = spend.run_cap(client, spend_cap or caps["max_cost"], budget)
        if not err and budget is not None and not a.plan:
            spend.ensure_budget(renv.RUNTIME / "budget.json", usd)   # written only once nothing else refuses it
    except spend.BudgetError as e:
        cap, err = None, str(e)
    if err and spend.is_paid(client):
        print(f"refusing: {err}", file=sys.stderr)
        return 2
    caps, spend_cap = (caps, cap) if spend_cap else (dict(caps, max_cost=cap), None)
    if use_judge and judge.same_model(judge_model, a.model):
        print(f"refusing --judge: the judge model {judge_model} is the model under test ({a.model}); pick another "
              "with --judge-model (fixed for the whole benchmark) or pass --no-judge", file=sys.stderr)
        return 2
    if a.session_minutes < renv.MIN_SESSION_MINUTES:
        renv.log(f"WARNING: {a.session_minutes:g}-min sessions (wrap-up at {rpolicy.wrap_minutes(a.session_minutes):.1f} min); "
            "expect little play")
    if a.sandbox_unfirewalled is True and a.sandbox != "container":
        print("refusing --sandbox-unfirewalled: it goes with --sandbox container", file=sys.stderr)
        return 2
    sandbox_change = None
    if a.runs:   # a series plays in one sandbox mode, fixed like its client; checked before docker is touched
        err, sandbox_change = bseries.series_sandbox(lser.series_meta(a.playthrough),
                                                  bseries.sandbox_mode(a.sandbox, a.sandbox_unfirewalled),
                                                  a.allow_sandbox_change)
        if err:
            msg = f"series {a.playthrough}: REFUSING the run: {err}"
            print(msg, file=sys.stderr)
            renv.log(msg)
            renv.log("runner stopped (sandbox mode differs from the series: run refused)")
            return 2
    # a multiplexer/ssh shell has no display env: the game (launch_game.sh inherits os.environ) gets the desktop's
    filled = display_env.fill(os.environ)
    if a.plan:   # after every refusal above, before the lock: nothing written, nothing launched
        return plan_run(a, scenario, save_sha1, caps, use_judge, judge_model, client, choice.profile)
    lock = acquire_lock()  # held for the process lifetime (released only to exec the new runner code)
    os.environ.update(client_setup.session_env(client, choice.profile, scenario is not None, os.environ))
    pi_files: dict = {"pace": client_setup.kickoff_pace(client, os.environ)}
    if client == "claude" or (choice.profile is not None and client == "pi"):   # once per runner
        try:
            pi_files |= client_setup.run_files(renv.RUNTIME / "sessions" / a.playthrough, client, getattr(a, "seed", None),
                                              (scenario_settings or {}).get("timing"), renv.REPO, renv.log)
        except (OSError, ValueError, subprocess.SubprocessError) as e:
            print(f"refusing --client {client}: the system text / tool list dump failed: {e}", file=sys.stderr)
            return 2
    if filled:
        renv.log("display env filled from the desktop session: " + " ".join(f"{k}={v}" for k, v in filled.items()))
    if not os.environ.get("XAUTHORITY"):
        renv.log("WARNING: no XAUTHORITY and no Xwayland -auth: the game may fail to open a window")
    box = None
    if a.sandbox == "container":   # once per runner: docker, network, images, firewall, egress proxy
        try:
            box = sandbox.prepare(client, None, a.sandbox_unfirewalled, renv.log, renv.RUNTIME / "sandbox-server.pid")
        except sandbox.SandboxError as e:
            print(f"refusing --sandbox container: {e}", file=sys.stderr)
            renv.log(f"runner stopped (sandbox not ready: {str(e).splitlines()[0][:200]})")
            return 2
    (renv.RUNTIME / "STOP").unlink(missing_ok=True)
    opt = rstate.Options(a.playthrough, a.hours, a.session_minutes, a.model, a.take_over, a.dry_run,
                  a.save_owner, a.effort, a.fix_model, a.fix_effort, a.fallback_model,
                  a.allow_short_sessions, not a.no_memory_keeper, a.keeper_model, a.keeper_effort,
                  a.keeper_minutes, a.autocompact, until=a.until, character_brief=a.character_brief,
                  roleplay=a.roleplay, difficulty=a.difficulty, scenario=scenario["name"] if scenario else None,
                  start_save=scenario["start"].get("save") if scenario else None)
    opt = replace(opt, run_hours=caps["run_hours"], max_cost=caps["max_cost"], max_tokens=caps["max_tokens"],
                  cap_changes=cap_changes or None, pins=lser.current_pins() if a.until and not a.runs else None,
                  judge=use_judge, judge_model=judge_model if use_judge else None, judge_change=judge_change,
                  client=client, client_change=client_change, sandbox=a.sandbox, box=box,
                  sandbox_change=sandbox_change, restart_run=a.restart_run, spend_cap=spend_cap,
                  keep_game=a.keep_game, max_cost_per_day=a.max_cost_per_day, profile=choice.profile,
                  seed=getattr(a, "seed", None), scenario_settings=scenario_settings, **pi_files)
    if a.runs:   # --hours is the whole series' budget; each run gets a fresh playthrough
        opt = replace(opt, hours=None, series=a.playthrough,
                      series_end=time.time() + a.hours * 3600 if a.hours else None)
    current: dict = {"runner": None, "stop": False}   # the run playing now (a series has one Runner per run)
    # Cutscenes are always skipped, also while the runner waits on menus and loads (the New Game intro
    # plays during StartLoading) and between sessions; the play session's MCP server idles meanwhile
    # (one skipper per game, runtime/skipper.lock).
    from bg3ai.skipper import start_background
    skip_stop = start_background(DEBUG_PORT, lambda m: renv.log(f"autoskip: {m}"), renv.RUNTIME / "skipper.lock")
    ram_stop = ramwatch.start_background(renv.log)
    renv.log("ramwatch: started (free RAM per minute → runtime/ram.log)" if ram_stop is not None else
        "ramwatch: not started here: another watchdog holds runtime/ramwatch.lock (that one guards the game)")

    def on_signal(signum, _frame):
        runner = current["runner"]
        if runner is not None and runner.stop_requested:
            renv.log("second signal: interrupting the current session")
            runner.abort = True
            return
        renv.log(f"signal {signum}: stopping after the current session (send again to interrupt it)")
        current["stop"] = True
        if runner is not None:
            runner.stop_requested = True

    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)
    try:
        if a.runs and os.environ.get("BG3AI_KNOWLEDGE_LOCK", "1") != "0":
            renv.log(f"play/knowledge read-only for the series: {bresets.lock_tree(renv.KNOWLEDGE_DIR, renv.KNOWLEDGE_LOCK)} files "
                f"(modes in {renv.KNOWLEDGE_LOCK}; BG3AI_KNOWLEDGE_LOCK=0 skips)")
        if a.runs:
            return lser.run_series(opt, a.runs, current, a.allow_version_drift, scenario, save_sha1)
        if bresets.unlock_tree(renv.KNOWLEDGE_DIR, renv.KNOWLEDGE_LOCK):   # a series runner that died while locked
            renv.log("play/knowledge: a stale series lock was undone")
        current["runner"] = lpt.Runner(opt)
        rc = current["runner"].run()
        lser.stop_game_at_end(current["runner"], opt, "run end")
        return rc
    except rcode.RestartRunner:
        lser.exec_new_runner(lock, skip_stop)
        raise  # not reached
    finally:   # not reached on a self-restart (exec_new_runner): the successor keeps the series' lock
        if skip_stop is not None:
            skip_stop.set()
        if box is not None:
            sandbox.shutdown()   # the egress proxy and any container left (a self-restart's successor recreates them)
        if ram_stop is not None:
            ram_stop.set()
        bresets.unlock_tree(renv.KNOWLEDGE_DIR, renv.KNOWLEDGE_LOCK)


PLAN_PROMPT = "<KICKOFF: built at the session start from the run's memory briefing>"


def resume_preview(pt: str, save_owner: str | None, session_minutes: float, allow_short: bool) -> dict | None:
    """run --plan for a playthrough with memory, read-only: what Runner.next_sid, choose_save, save_env and
    session_minutes would give its next session (without it --plan printed S001 and no save for a resume). None: a
    new playthrough."""
    import memlib as M
    mem = renv.PLAYTHROUGHS / pt / "memory"
    if not mem.is_dir():
        return None
    d = renv.RUNTIME / "sessions" / pt
    try:
        rs = json.loads((d / "runner.json").read_text())
    except (OSError, ValueError):
        rs = {}
    reuse = M.reusable_session(mem)
    sid = rpolicy.session_id(reuse) if reuse else rpolicy.session_id(
        rpolicy.predict_session_number(M.session_numbers(mem), M.load_state(mem).get("current")))
    handoff = mem / "HANDOFF.md"
    text = handoff.read_text() if handoff.exists() else ""
    label = parse_handoff_save(text)
    ours = set(rs.get("runner_saves") or []) | set(rs.get("agent_saves") or [])
    windows = play_windows(rs.get("history") or [], d)
    saves, after = rcode.list_saves(), rs.get("saves_after")
    own = [s for s in saves if s.mtime >= after] if after else saves
    save, why = pick_save_why(own, label, rs.get("save_owner"), ours, windows=windows,
                              handoff_session=lpt.parse_handoff_session(text), ends=session_ends(rs.get("history"), d))
    owner, prefix = rpolicy.save_identity(save_owner or rs.get("save_owner"))
    foreign = foreign_saves(saves, owner, ours, windows, prefix) if not after else []
    over = rstate.session_overrides(d / "session-config.json")
    minutes = over.get("minutes", session_minutes)
    if minutes < renv.MIN_SESSION_MINUTES and not allow_short:
        minutes = session_minutes
    return {"sid": sid, "handoff_label": label, "save": save.folder if save else None, "save_why": why,
            "oom_blocked": bool(save and save.folder in (rs.get("oom_blocked") or [])), "exclude": foreign,
            "minutes": minutes, "model": over.get("model"), "effort": over.get("effort"),
            "memory_linked": (renv.PLAY_DIR / "memory").resolve() == mem.resolve()}


def plan_run(a, scenario: dict | None, save_sha1: str | None, caps: dict, use_judge: bool, judge_model: str,
             client_name: str, profile=None) -> int:
    """run --plan: what `run` would do next, read-only (no runner lock, no game, no playthrough/memory/series files;
    the Runner isn't built: its RunnerState makes the session dir and read_handover consumes a pending handover)."""
    base = a.playthrough
    print(f"plan for `run` (nothing launched or written; {time.strftime('%Y-%m-%d %H:%M')})")
    k, pt = 1, base
    if a.runs:
        recs = bseries.series_records(renv.RUNTIME / "sessions" / base / "runs.jsonl", base)
        k = bseries.next_run_index(recs)
        pt = bseries.run_name(base, k)
        print(f"series:         {base}: run {k}/{a.runs} next ({pt}), {len(recs)} recorded"
              + ("; ALL DONE: run would exit at once" if k > a.runs else ""))
        half = lser.half_run_state(pt) if k <= a.runs else None
        active = (renv.RUNTIME / "runner.lock").exists() and not _lock_free(renv.RUNTIME / "runner.lock")
        for line in lser.half_run_lines(half, forced=a.restart_run, active=active) if half else []:
            print(line)
        prof = lser.series_profile(base) or (scenario["profile"] if scenario else None)
        if prof is None:
            prof = rpolicy.load_profile(renv.PLAYTHROUGHS / (base if k <= 1 else pt) / "profile.json")
        prof = rpolicy.update_profile(prof, a.character_brief, a.roleplay, a.difficulty) \
            if (a.character_brief or a.roleplay or a.difficulty) else prof
        pins = lser.current_pins()
        meta = lser.series_freeze(base, a.runs, a.until, prof, pins, caps,
                             bscenario.scenario_freeze(scenario, save_sha1) if scenario else None,
                             judge_model if use_judge else None, client_name,
                             bseries.sandbox_mode(a.sandbox, bool(a.sandbox_unfirewalled)))
        path = renv.RUNTIME / "sessions" / base / "series.json"
        if meta is None:
            print(f"series.json:    {path} exists with every key (frozen; this run is held to it):")
            print(json.dumps(lser.series_meta(base), indent=1, ensure_ascii=False))
        else:
            print(f"series.json:    {'NEW ' if not path.exists() else 'keys added to '}{path} would be:")
            print(json.dumps(meta, indent=1, ensure_ascii=False))
        drift = bpins.pin_drift(lser.series_meta(base).get("pins"), pins) if path.exists() else []
        if drift:
            print(f"version drift:  {'; '.join(drift)} -> run {k} "
                  + ("plays with it (recorded)" if a.allow_version_drift else "REFUSED (--allow-version-drift)"))
        if os.environ.get("BG3AI_KNOWLEDGE_LOCK", "1") != "0":
            print(f"knowledge:      {renv.KNOWLEDGE_DIR} read-only for the series (BG3AI_KNOWLEDGE_LOCK=0 skips)")
    else:
        print(f"playthrough:    {pt} (not a series)")
    req_off, why = steam_paths.offline_required()
    off, owhy = steam_paths.steam_offline()
    print(f"steam:          offline required: {req_off} ({why}); offline now: {off} ({owhy})"
          + ("  -> THE RUNNER WOULD REFUSE TO LAUNCH" if req_off and not off else ""))
    no_display = display_env.problem(os.environ)
    print(f"display:        {display_env.describe(os.environ)} (unset ones found from the desktop session)"
          + ("" if os.environ.get("XAUTHORITY") else "  -> NO XAUTHORITY")
          + (f"  -> THE RUNNER WOULD REFUSE TO LAUNCH: {no_display}" if no_display else ""))
    bad = bg3le_buildinfo.problems()
    print("bg3le:          " +("STALE: " + "; ".join(bad) + " -> the runner would stop" if bad else "dist has every patch"))
    ours = game.pids_of_game()
    print(f"game:           {'harness bg3 up: ' + str(ours) if ours else 'not running: the runner launches it'}"
          + (f"; a user's own bg3 runs {game.foreign_bg3_pids()}: the runner won't launch beside it"
             if game.foreign_bg3_pids() else ""))
    holder = (renv.RUNTIME / "runner.lock").exists() and not _lock_free(renv.RUNTIME / "runner.lock")
    print(f"runner lock:    {'HELD: another runner is active (run would refuse)' if holder else 'free'}")
    keeper = rpolicy.keeper_model(a.keeper_model, a.model) if not a.no_memory_keeper else "OFF"
    print(f"play:           client {client_name}, model {a.model}, effort {a.effort or 'default'}, "
          f"{a.session_minutes:g}-min sessions, keeper {keeper}, judge {judge_model if use_judge else 'off'}, "
          f"sandbox {a.sandbox}, caps {caps}, --hours {a.hours or 'none (until stopped or the game ends)'}"
          + (f", spend cap ${a.max_cost:g}" if a.max_cost and a.max_cost != caps["max_cost"] else "")
          + (f", --max-cost-per-day ${a.max_cost_per_day:g}" if a.max_cost_per_day else ""))
    sid, minutes, model, effort = "S001", a.session_minutes, a.model, a.effort
    res = resume_preview(pt, a.save_owner or os.environ.get("BG3AI_SAVE_OWNER"), a.session_minutes,
                         a.allow_short_sessions)
    if res:
        sid, minutes = res["sid"], res["minutes"]
        model, effort = res["model"] or model, res["effort"] or effort
        print(f"resume:         {sid}; loads {res['save'] or 'NO SAVE (new game)'} (HANDOFF names "
              f"{res['handoff_label'] or '-'})" + ("  -> OOM-BLOCKED: the runner loads an older save" if
                                                    res["oom_blocked"] else ""))
        print(f"save choice:    {res['save_why']}")
        print(f"saves hidden:   {', '.join(res['exclude']) or 'none'} (BG3AI_SAVES_EXCLUDE)")
        print(f"session-config: {minutes:g} min, model {model}, effort {effort or 'default'} (session-config.json "
              "wins over the flags)" + ("" if res["memory_linked"] else f"; play/memory is relinked to {pt}"))
    owner, prefix = rpolicy.save_identity(a.save_owner or os.environ.get("BG3AI_SAVE_OWNER"))
    env = {"BG3AI_PLAYTHROUGH": pt, "BG3AI_SESSION": sid, "BG3_SESSION_MINUTES": str(minutes),
           "BG3_WRAP_MINUTES": str(rpolicy.wrap_minutes(minutes)), "BG3AI_SAVE_OWNER": owner,
           "BG3AI_SAVE_PREFIX": prefix, "BG3AI_KEEPER": "0" if a.no_memory_keeper else "1",
           **({"BG3AI_SAVES_EXCLUDE": "\n".join(res["exclude"])} if res and res["exclude"] else {}),
           **({"BG3AI_START_SAVE": scenario["start"]["save"]} if scenario and scenario["start"].get("save") else {})}
    opt = types.SimpleNamespace(sandbox=a.sandbox, client=client_name, profile=profile,   # pi: where run_files
                                system_file=renv.RUNTIME / "sessions" / pt / "pi-system.txt")  # would write it
    cl = rstate.make_client(opt)
    req = clients.Request(PLAN_PROMPT, model, renv.RUNTIME / "sessions" / pt, session=str(uuid.uuid4()), resume=False,
                          effort=effort, fallback_model=a.fallback_model, autocompact=a.autocompact, env=env,
                          tag=sid)
    cmd = cl.command(req)
    print(f"{'next session: ' if res else 'first session:'}  {sid} of {pt} in {renv.PLAY_DIR} (deadline in {minutes:g} min; the "
          "session env adds the deadlines, metrics path and BG3AI_* of the runner's env)")
    print(json.dumps({"cmd": cmd, "cwd": str(renv.PLAY_DIR), "env": env}, indent=1, ensure_ascii=False))
    if a.sandbox == "container":
        print("sandbox:        the command runs inside the container (box.run_argv), the bg3 server over http")
    return 0


def _lock_free(path: Path) -> bool:
    """Nobody holds an flock on path (probed with a separate open; released at once)."""
    with open(path, "a") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        fcntl.flock(f, fcntl.LOCK_UN)
    return True


def persona_line(p: dict) -> str:
    """One log line for a record's persona."""
    what = f"score {p['score']:g}/10" if p.get("score") is not None else p.get("status", "?")
    return (f"{what} by {p.get('judge_model')} on {p.get('decisions', 0)} decision(s), ${p.get('cost_usd') or 0:.2f}"
            + (f": {p['error']}" if p.get("error") else ""))


def cmd_stop(_a) -> int:
    renv.RUNTIME.mkdir(parents=True, exist_ok=True)
    (renv.RUNTIME / "STOP").write_text(str(time.time()))
    print("stop requested: the runner exits after the current session")
    return 0


def _ago(t: float | None) -> str:
    if not t:
        return "-"
    d = t - time.time()
    m = abs(d) / 60
    when = f"{m:.0f} min" if m < 120 else f"{m / 60:.1f} h"
    return f"{datetime.fromtimestamp(t):%H:%M} ({'in ' + when if d > 0 else when + ' ago'})"


def cmd_status(a) -> int:
    try:
        st = json.loads((renv.RUNTIME / "runner.status.json").read_text())
    except (OSError, ValueError):
        st = {}
    pt = a.playthrough or st.get("playthrough")
    alive_pid = st.get("runner_pid")
    if alive_pid:
        try:
            os.kill(alive_pid, 0)
        except OSError:
            alive_pid = None
    up = game.port_open()
    gs = game.game_state() if up else None
    print(f"runner:   {'running (pid %d)' % alive_pid if alive_pid else 'not running'}"
          + (", STOP requested" if (renv.RUNTIME / 'STOP').exists() else ""))
    print(f"game:     {'up, state ' + str(gs) if up else 'not running (no debug port)'}")
    print(f"playthrough: {pt or '-'}")
    if alive_pid and st.get("session"):
        print(f"session:  {st['session']} started {_ago(st.get('started'))}, deadline {_ago(st.get('deadline'))}, "
              f"hard stop {_ago(st.get('hard_stop'))}")
    if st.get("run_end"):
        print(f"run ends: {_ago(st['run_end'])}")
    if st.get("status_line"):
        print(f"status:   {st['status_line']}")
    th = ramwatch.last_thermal(renv.RUNTIME / "thermal.log")
    print(f"thermal:  {th[1] + ' (' + _ago(th[0]) + ')' if th else '- (no runtime/thermal.log)'}")
    if pt:
        rs = rstate.RunnerState(pt)
        hist = rs["history"] or []
        if hist:
            h = hist[-1]
            print(f"last:     {h['session']} {h['reason']} after {h.get('minutes')} min, progress={h.get('progress')}")
        if rpolicy.is_stuck(hist):
            print("stuck:    YES, the next kickoff carries a change-approach directive")
        save = pick_save(rcode.list_saves(), None, rs["save_owner"], set(rs["runner_saves"]) | set(rs["agent_saves"] or []),
                         windows=play_windows(hist, rs.dir))
        print(f"last save: {save.folder + ' (' + _ago(save.mtime) + ')' if save else '-'}")
        logs = sorted(rs.dir.glob("S*.log"), key=lambda p: p.stat().st_mtime)
        logfile = Path(st["log"]) if st.get("log") and Path(st["log"]).exists() else (logs[-1] if logs else None)
        if logfile:
            print(f"\n--- {logfile} (last 10 lines)")
            print("\n".join(logfile.read_text(errors="replace").splitlines()[-10:]))
    return 0


def cmd_runs(a) -> int:
    """The runs table of a series (or of one playthrough's --until runs) and its clean streak. --backfill first
    adds the benchmark fields (runs.METRIC_FIELDS) records written before them lack, and the model_time keys
    (api_floor, the headline) an older model_time lacks, in place; present values stay. --vs OTHER: the two series'
    means side by side, "API floor differs" when their API floors are > 0.3 s apart."""
    path = renv.RUNTIME / "sessions" / a.playthrough / "runs.jsonl"
    recs = bseries.series_records(path, a.playthrough)
    if not recs:
        print(f"no run records for {a.playthrough} ({path})")
        return 1
    if getattr(a, "backfill", False):
        for r in recs:
            state_dir = renv.RUNTIME / "sessions" / (r.get("playthrough") or a.playthrough)
            fields = {**runs.backfill_metrics(state_dir, r), **runs.backfill_model_time(state_dir, r)}
            if fields:
                ok = bseries.update_record(path, r, fields)
                print(f"run {r.get('run') or 1} ({r.get('playthrough')}): "
                      f"{'backfilled' if ok else 'NOT written'} {', '.join(fields)}")
        recs = bseries.series_records(path, a.playthrough)
    print(brecords.runs_table(recs))
    print("\n" + brecords.series_summary(recs))
    if getattr(a, "vs", None):
        other = bseries.series_records(renv.RUNTIME / "sessions" / a.vs / "runs.jsonl", a.vs)
        print("\n" + (brecords.series_compare(a.vs, other, a.playthrough, recs) if other else
                      f"no run records for {a.vs}"))
    if a.failures:
        for r in recs:
            print(f"\nrun {r.get('run') or 1} ({r.get('playthrough')}): {r.get('outcome')}"
                  + (f" ({r['why']})" if r.get("why") else ""))
            for f in r.get("failures") or []:
                when = datetime.fromtimestamp(f["t"]).strftime("%H:%M:%S") if f.get("t") else "--:--:--"
                print(f"  {when} {f.get('class', '?'):8} {f.get('kind')}{' x%d' % f['n'] if f.get('n') else ''}: "
                      f"{f.get('detail')}")
            for o in r.get("operator") or []:
                print(f"  operator: {o.get('what')}")
            k = r.get("knowledge") or {}
            if k.get("agent_edits"):
                print(f"  knowledge/ edits put back: {', '.join(k['agent_edits'])}")
    return 0


def cmd_judge(a) -> int:
    """Re-judge records of a series (or a playthrough's --until runs) in place: --run K, else every record without
    a scored persona. Also fills a missing `model_time`. The judge model: series.json's, else --judge-model /
    $BG3AI_JUDGE_MODEL / judging.DEFAULT_MODEL (then frozen into an existing series.json)."""
    path = renv.RUNTIME / "sessions" / a.playthrough / "runs.jsonl"
    recs = bseries.series_records(path, a.playthrough)
    if a.run is not None:
        recs = [r for r in recs if int(r.get("run") or 1) == a.run]
    elif not a.all:
        recs = [r for r in recs if brecords.record_persona(r) is None]
    if not recs:
        print(f"no run {'%d ' % a.run if a.run is not None else 'to judge '}in {path}", file=sys.stderr)
        return 1
    frozen = lser.series_meta(a.playthrough).get("judge_model")
    model, err, change = bseries.series_judge_model(frozen, a.judge_model or os.environ.get("BG3AI_JUDGE_MODEL"),
                                                 judging.DEFAULT_MODEL, a.allow_judge_change)
    if err:
        print(f"refusing to judge {a.playthrough}: {err}", file=sys.stderr)
        return 2
    if frozen is None and (renv.RUNTIME / "sessions" / a.playthrough / "series.json").exists():
        lser.freeze_series(a.playthrough, 0, None, {}, judge_model=model)
    rc = 0
    for r in recs:
        state_dir = renv.RUNTIME / "sessions" / (r.get("playthrough") or a.playthrough)
        fields = {"persona": lser.judge_record(state_dir, r, model, a.timeout)}
        if change:
            fields["judge_change"] = change
        if "model_time" not in r:
            end = r.get("t")
            fields["model_time"] = runs.model_time_field(model_time.model_time(
                state_dir, since=r.get("started"), until=end + model_time.WINDOW_SLACK_S if end else None))
        ok = bseries.update_record(path, r, fields)
        print(f"run {r.get('run') or 1} ({r.get('playthrough')}): {persona_line(fields['persona'])}"
              + ("" if ok else f" (NOT written: the record is gone from {path})"))
        rc = rc if ok and fields["persona"].get("status") in (judge.OK, judge.NO_DECISIONS) else 1
    return rc


def cmd_scenarios(_a) -> int:
    """scenarios/*.json with their status: ready, needs a start save, or invalid."""
    paths = sorted(renv.SCENARIOS.glob("*.json"))
    if not paths:
        print(f"no scenarios in {renv.SCENARIOS}")
        return 1
    saves = rcode.list_saves()
    for path in paths:
        try:
            sc = lser.load_scenario_file(path.stem)
        except bscenario.ScenarioError as e:
            print(f"{path.stem:20} {bscenario.INVALID}: {e}")
            continue
        status, detail = lser.scenario_status(sc, saves)
        caps = ", ".join(f"{k}={v:g}" for k, v in sc["caps"].items() if v is not None)
        print(f"{sc['name']:20} {status:18} --until {sc['until']}, {sc['runs']} run(s), {caps}; {detail}"
              + (f"\n{'':20} {sc['title']}" if sc.get("title") else ""))
    return 0


def cmd_bench(argv: list[str]) -> int:
    """play.py bench plan|run|status|resume --matrix <file.toml> [--runner session|legacy]: bg3bench's matrix CLI
    (python -m bg3bench.matrix) with the harness's live RunHost, so `--runner session` plays each job as one
    run_session on this machine's game (bg3harness.runner.run_host). run and resume take runtime/runner.lock like
    `run`, and the cutscene skipper and the RAM watchdog run meanwhile."""
    from bg3bench import matrix_cli  # _harness puts packages/bench/src on sys.path
    from bg3harness.runner.run_host import LiveRunHost

    host = LiveRunHost(lpt.Runner)
    if not argv or argv[0] not in ("run", "resume"):
        return matrix_cli.main(argv, repo=renv.REPO, host=host, out=sys.stdout)
    lock = acquire_lock()  # noqa: F841 - held (runtime/runner.lock) until the process exits
    from bg3ai.skipper import start_background
    skip_stop = start_background(DEBUG_PORT, lambda m: renv.log(f"autoskip: {m}"), renv.RUNTIME / "skipper.lock")
    ram_stop = ramwatch.start_background(renv.log)
    try:
        return matrix_cli.main(argv, repo=renv.REPO, host=host, out=sys.stdout)
    finally:
        for stop in (skip_stop, ram_stop):
            if stop is not None:
                stop.set()

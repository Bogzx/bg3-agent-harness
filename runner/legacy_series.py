"""The legacy series parts of `play.py run` (moved from runner/play.py, Phase D.3): run records
(runs.jsonl), the persona judge, the fresh-run reset and knowledge restore, scenario files, the frozen series
settings, half runs and run_series. They use the bench's code (bg3bench, on sys.path through _harness); bg3bench's
matrix replaces them (`bg3bench run --runner session`).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import _harness  # noqa: F401  (packages/harness/src, packages/bench/src and bridge/src on sys.path)

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools" / "memory"))  # memlib, new_playthrough

import legacy_cli as lcli
import legacy_playthrough as lpt
from bg3ai import milestones
from bg3bench import pins as bpins
from bg3bench import records as brecords
from bg3bench import resets as bresets
from bg3bench import scenario_v1 as bscenario
from bg3bench import series as bseries
from bg3harness import bg3le_buildinfo, client_setup, judge, model_time, runs, scenario_file, steam_paths
from bg3harness.game.io import Save
from bg3harness.runner import codewatch as rcode
from bg3harness.runner import env as renv
from bg3harness.runner import judging
from bg3harness.runner import policy as rpolicy
from bg3harness.runner import state as rstate
from bg3harness.session import GAME_FAULTS


def run_record(state_dir: Path, run_started: float, now: float, hit: dict, playthrough: str,
               history: list[dict], outcome: str | None = None, why: str = "", take_over: bool = False,
               cap: str | None = None) -> dict:
    """One runs.jsonl line for an --until run: wall time, sessions, claude cost and tokens (the stream's result
    events, the [end] lines), crashes/hangs and recoveries (runner_events.jsonl), agent reloads (actions.jsonl);
    the outcome (reached | timeout | over_budget | gave_up | runner_error) and the cap that ended it (time | cost |
    tokens), every failure tagged harness/agent/unknown (bg3harness.runs), the operator actions, `clean` (reached, no
    harness failure, no operator action), and the benchmark metrics (runs.run_metrics: time to the milestone,
    deaths, fairness flags, latency, story points)."""
    outcome = outcome or ("reached" if hit.get("id") else "timeout")
    if outcome == "timeout":
        cap = "time"
    sessions = [h for h in history if h.get("kind") == "play" and (h.get("started") or 0) >= run_started]
    totals = runs.claude_totals(state_dir, run_started)
    cost, turns, tokens = totals["cost_usd"], totals["turns"], totals["tokens"]
    events = []
    try:
        for ln in (state_dir / "runner_events.jsonl").read_text().splitlines():
            try:
                e = json.loads(ln)
            except ValueError:
                continue
            if e.get("t", 0) >= run_started - 1:   # event times are rounded to 0.1 s
                events.append(e)
    except OSError:
        pass
    actions = [a for a in rpolicy.read_actions_since(run_started) if a.get("t", now) <= now + 1]
    reloads = [a for a in actions if a.get("tool") == "load_game"]
    failures = runs.classify_run(state_dir, run_started, now, sessions, events, actions, outcome, why, cap)
    _, operator = runs.event_failures(events)
    if take_over:
        operator.append({"t": round(run_started, 1), "what": "--take-over (the run may have started from a game "
                                                              "it didn't set up)"})
    counts = brecords.class_counts(failures)
    return {"t": round(now, 1), "playthrough": playthrough, "outcome": outcome, "why": why[:300],
            "clean": runs.is_clean(outcome, failures, operator),
            "harness_failures": counts[runs.HARNESS], "agent_failures": counts[runs.AGENT],
            "unknown_failures": counts[runs.UNKNOWN],
            "milestone": hit.get("id"), "label": hit.get("label"),
            "reached_in": hit.get("session"), "reached_at": hit.get("t"),
            "started": round(run_started, 1), "wall_min": round((now - run_started) / 60, 1),
            "sessions": len(sessions), "play_min": round(sum(h.get("minutes") or 0 for h in sessions), 1),
            "cost_usd": round(cost, 4), "turns": turns, "tokens": tokens,
            "crashes": sum(1 for e in events if e.get("kind") == "fault" and e.get("fault") in GAME_FAULTS),
            "agent_hangs": sum(1 for e in events if e.get("kind") == "fault" and e.get("fault") == "agent_hang"),
            "recoveries": sum(1 for e in events if e.get("kind") == "recovered"),
            "setup_failures": sum(1 for e in events if e.get("kind") == "setup_failed"),
            "reloads": len(reloads), "failures": failures, "operator": operator, "cap": cap,
            **runs.run_metrics(state_dir, run_started, hit.get("t"), failures),
            "milestones_reached": milestones.status(rpolicy.read_seen(state_dir))["reached"]}


def judge_record(state_dir: Path, rec: dict, model: str, timeout: float = renv.JUDGE_TIMEOUT_S, client=None,
                 stop=lambda: False) -> dict:
    """A run record's `persona` (judge.persona_field of judging.judge_run) within `timeout` seconds overall; stop():
    give up now (a second operator signal). Never raises: a failed or late judge is a judge_failed persona. Its
    cost is persona.cost_usd, apart from the run's capped cost_usd (the judge runs in a temp dir, not state_dir)."""
    deadline = time.time() + timeout

    def call(c, prompt):
        left = deadline - time.time()
        if stop():
            raise RuntimeError("judge interrupted (operator signal)")
        if left < 1:
            raise RuntimeError(f"judge timed out ({timeout:g} s)")
        return judge.call_client(c, prompt, timeout=min(judge.TIMEOUT_S, left))
    try:
        res = judging.judge_run(state_dir, rec, client=client or judge.ClaudeCLI(model), call=call)
    except Exception as e:
        res = {"status": judge.FAILED, "score": None, "judge_model": model, "error": f"{type(e).__name__}: {e}"}
    return judge.persona_field(res)


def load_scenario_file(name: str) -> dict:
    """scenarios/<name>.json, validated (bscenario.load_scenario); bscenario.ScenarioError when missing or invalid."""
    path = renv.SCENARIOS / f"{name}.json"
    if not path.exists():
        have = ", ".join(sorted(p.stem for p in renv.SCENARIOS.glob("*.json"))) or "none"
        raise bscenario.ScenarioError(f"no scenario {name!r} in {renv.SCENARIOS} (have: {have})")
    return bscenario.load_scenario(path, [m[0] for m in milestones.MILESTONES], rpolicy.ROLEPLAY_MODES, rpolicy.DIFFICULTIES)


def load_scenario_path(path: Path) -> tuple[dict, dict]:
    """run --scenario-file: a resolved scenario (bg3harness.scenario_file) -> (the v1 scenario, validated and filled
    like scenarios/<name>.json, its name from the file's content; the expected {difficulty, karmic_dice, timing}).
    bscenario.ScenarioError when unreadable or invalid."""
    try:
        sc = json.loads(path.read_text())
    except (OSError, ValueError) as e:
        raise bscenario.ScenarioError(f"{path}: {e}") from None
    v1, want, errs = scenario_file.split(sc)
    if errs:
        raise bscenario.ScenarioError(f"{path}: " + "; ".join(errs))
    name = str(v1.get("name") or "")
    if not bscenario.SCENARIO_NAME_RE.fullmatch(name):
        raise bscenario.ScenarioError(f"{path}: name {name!r}: use a-z, 0-9, '_' or '-'")
    with tempfile.TemporaryDirectory(prefix="bg3ai-scenario-") as td:   # bscenario.load_scenario names it by its file
        v1_path = Path(td) / f"{name}.json"
        v1_path.write_text(json.dumps(v1))
        try:
            return bscenario.load_scenario(v1_path, [m[0] for m in milestones.MILESTONES], rpolicy.ROLEPLAY_MODES,
                                      rpolicy.DIFFICULTIES), want
        except bscenario.ScenarioError as e:
            raise bscenario.ScenarioError(str(e).replace(str(v1_path), str(path))) from None


def scenario_status(sc: dict, saves: list[Save] | None = None) -> tuple[str, str]:
    """bscenario.scenario_status against the save folder (read only: the start save's .lsv is hashed, never written)."""
    folders = {s.folder for s in (rcode.list_saves() if saves is None else saves)}
    return bscenario.scenario_status(sc, folders, lambda f: bscenario.save_digest(renv.SAVE_DIR, f))


def apply_scenario(a, sc: dict) -> tuple[str | None, str | None]:
    """run --scenario: the scenario's values into the parsed args (a flag left out takes the scenario's; one that
    contradicts it is refused). -> (refusal, the start save's sha1)."""
    conflicts = bscenario.scenario_cli_conflicts(sc, {"seed": getattr(a, "seed", None), "until": a.until,
                                                 "character_brief": a.character_brief, "roleplay": a.roleplay, "difficulty": a.difficulty,
                                                 "save_owner": a.save_owner})
    if conflicts:
        return "; ".join(conflicts) + " (drop the flag: the scenario fixes it)", None
    status, detail = scenario_status(sc)
    if status != bscenario.READY:
        return f"{status}: {detail}", None
    a.playthrough = a.playthrough or sc["name"]
    a.until, a.runs = sc["until"], a.runs or sc["runs"]
    a.seed = (sc.get("game_settings") or {}).get("seed")
    a.character_brief = a.roleplay = a.difficulty = None   # profile.json comes from the scenario (prepare_series_run)
    sha1 = None
    if sc["start"]["kind"] == "save":
        a.save_owner = sc["profile"]["character"]["name"]
        sha1 = bscenario.save_digest(renv.SAVE_DIR, sc["start"]["save"])
    return bscenario.scenario_series_check(series_meta(a.playthrough), sc, a.playthrough, sha1), sha1


def series_meta(base: str) -> dict:
    """runtime/sessions/<base>/series.json ({} when there is none yet)."""
    try:
        d = json.loads((renv.RUNTIME / "sessions" / base / "series.json").read_text())
    except (OSError, ValueError):
        return {}
    return d if isinstance(d, dict) else {}


def series_profile(base: str) -> dict | None:
    """The character brief every run of a series plays (runtime/sessions/<base>/series.json "profile"), frozen when
    the series started: run 1 later adds the name its agent chose to its own profile.json, and run 2 must not get
    that name forced on it."""
    return series_meta(base).get("profile")


def freeze_series(base: str, n: int, until: str | None, profile: dict, pins: dict | None = None,
                  caps: dict | None = None, scenario: dict | None = None, judge_model: str | None = None,
                  client: str | None = None, box: dict | None = None) -> None:
    """series.json, written once by the first run: the profile, N, the milestone, the version pins, the caps, the
    persona judge's model, the client (--client) and the sandbox (box: bseries.sandbox_mode) every later run is held
    to, and the scenario (bscenario.scenario_freeze) of a run --scenario series. A series from before
    pins/caps/judge/client gets them from the first run that has them; one from before the sandbox key never gets
    it (it played without a sandbox: bseries.series_sandbox reads a missing key as "none")."""
    meta = series_freeze(base, n, until, profile, pins, caps, scenario, judge_model, client, box)
    if meta is None:
        return
    path = renv.RUNTIME / "sessions" / base / "series.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(meta, indent=1, ensure_ascii=False) + "\n")


def series_freeze(base: str, n: int, until: str | None, profile: dict, pins: dict | None = None,
                  caps: dict | None = None, scenario: dict | None = None, judge_model: str | None = None,
                  client: str | None = None, box: dict | None = None) -> dict | None:
    """What freeze_series would write to series.json (None: nothing, it has every key already). Writes nothing
    (run --plan prints it)."""
    path = renv.RUNTIME / "sessions" / base / "series.json"
    meta = series_meta(base)
    if path.exists() and ("pins" in meta or pins is None) and ("caps" in meta or caps is None) and \
            ("judge_model" in meta or judge_model is None) and ("client" in meta or client is None):
        return None
    new = not path.exists()
    if not path.exists():
        meta = {"series": base, "runs": n, "until": until, "profile": profile, "created": round(time.time(), 1)}
        if scenario is not None:
            meta["scenario"] = scenario
    if pins is not None:
        meta.setdefault("pins", pins)
    if caps is not None:
        meta.setdefault("caps", caps)
    if judge_model is not None:
        meta.setdefault("judge_model", judge_model)
    if client is not None:
        meta.setdefault("client", client)
    if new and box is not None:
        meta.update(box)
    return meta


def current_pins() -> dict:
    """The versions a run starting now plays with (bpins.version_pins): the BG3 Steam buildid, the bg3le build the
    launcher loads, the mod tree and the repo."""
    lib = Path(os.environ.get("BG3AI_BG3LE_LIB", bg3le_buildinfo.DIST / "libbg3le.so"))
    return bpins.version_pins(renv.REPO, steam_paths.buildid(), lib.parent, rcode.MOD_DIR / "Mods" / "BG3AI" / "meta.lsx",
                             rcode.mod_tree_hashes())


def gate_series_run(base: str, r: lpt.Runner, pins: dict, allow_drift: bool) -> str | None:
    """Before run r of the series: a refusal (str) when a run that hasn't started yet would play with other
    versions than the series' first run (series.json "pins") and drift isn't allowed; else None, with the run's
    pins (and the drift, when allowed) set on r.opt. A run already under way keeps the pins it started with."""
    run = r.state["run"] or {}
    if run.get("started") or r.state["history"]:
        r.opt.pins = run.get("pins") or pins
        return None
    drift = bpins.pin_drift(series_meta(base).get("pins"), pins)
    if drift and not allow_drift:
        return (f"series {base}: REFUSING run {r.opt.run_index}: versions differ from the series' first run "
                f"({'; '.join(drift)}); restore them, or pass --allow-version-drift to record the drift")
    r.opt.pins, r.opt.version_drift = pins, drift or None
    if drift:
        renv.log(f"series {base}: run {r.opt.run_index} plays with version drift (allowed): {'; '.join(drift)}")
    return None


def prepare_series_run(base: str, pt: str, k: int, scenario: dict | None = None) -> None:
    """Run k's playthrough before its Runner exists: the series' profile.json (k > 1, or every run of a scenario:
    its profile) and a fresh memory from memory-template/ (tools/memory/new_playthrough.py), unless they're there
    already (a resumed run). A scenario that starts from a save also gets its start note as HANDOFF.md
    (bscenario.start_note: the harness's neutral text, never another playthrough's memory)."""
    import memlib
    from new_playthrough import create_memory
    prof_path = renv.PLAYTHROUGHS / pt / "profile.json"
    prof = series_profile(base)
    if prof is None and scenario is not None:
        prof = scenario["profile"]
    if prof is None and k > 1:
        prof = rpolicy.load_profile(renv.PLAYTHROUGHS / base / "profile.json") or None
    # run 1 restarted (restart_half_run moved its profile.json aside): the series' frozen profile
    if (k > 1 or scenario is not None or series_profile(base) is not None) and prof and not prof_path.exists():
        prof_path.parent.mkdir(parents=True, exist_ok=True)
        prof_path.write_text(json.dumps(prof, indent=1, ensure_ascii=False) + "\n")
        renv.log(f"{pt}: profile.json " + (f"from scenario {scenario['name']}" if scenario else f"copied from series {base}"))
    mem = renv.PLAYTHROUGHS / pt / "memory"
    if not mem.is_dir() or not any(mem.iterdir()):
        h = create_memory(pt, mem, renv.MEMORY_TEMPLATE)
        renv.log(f"{pt}: new memory {mem} from memory-template (commit {h[:8]})")
        if scenario is not None and scenario["start"]["kind"] == "save":
            (mem / "HANDOFF.md").write_text(bscenario.start_note(scenario))
            h = memlib.git_commit_all(mem, f"S000 scenario {scenario['name']}: start note") or h
            renv.log(f"{pt}: HANDOFF.md = the start note of scenario {scenario['name']} (commit {h[:8]})")


SERIES_DIR_KEEP = ("series.json", "runs.jsonl", "runner_events.jsonl")   # run 1 shares the series' session dir


def _save_record_path(pt: str) -> Path:
    """runtime/saves/<pt>.txt: the folders the play session's save_game wrote (bridge server._save_record)."""
    return renv.RUNTIME / "saves" / (re.sub(r"[^A-Za-z0-9_.-]", "_", pt) + ".txt")


def _git_lines(mem: Path, *args: str) -> list[str]:
    try:
        r = subprocess.run(["git", "--no-optional-locks", "-C", str(mem), *args], capture_output=True, text=True,
                           timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return []
    return [ln for ln in r.stdout.splitlines() if ln.strip()] if r.returncode == 0 else []


def half_run_state(pt: str) -> dict | None:
    """What a series run with no record left behind (a runner or machine crash mid-run; naut2 run 2, 2026-10-16
    20:10): runner.json's run start and its finished play sessions, its claude streams, memory edits past the
    template (an open session, uncommitted changes, session commits), the saves/<pt>.txt entries and the saves
    written since its start. None: nothing, a fresh run. Read-only (run --plan prints it)."""
    sdir = renv.RUNTIME / "sessions" / pt
    try:
        st = json.loads((sdir / "runner.json").read_text())
    except (OSError, ValueError):
        st = {}
    st = st if isinstance(st, dict) else {}
    started = (st.get("run") or {}).get("started")
    finished = [h.get("session") for h in st.get("history") or []
                if h.get("kind") == "play" and (h.get("started") or 0) >= (started or 0)]
    streams = sorted(f.name for f in sdir.glob("S*.jsonl") if runs.CLAUDE_STREAM_RE.match(f.name)) \
        if sdir.is_dir() else []
    mem = renv.PLAYTHROUGHS / pt / "memory"
    try:
        ms = json.loads((mem / ".session.json").read_text())
    except (OSError, ValueError):
        ms = {}
    open_sid = rpolicy.session_id(ms["current"]) if isinstance(ms, dict) and ms.get("open") and ms.get("current") else None
    dirty = _git_lines(mem, "status", "--porcelain") if (mem / ".git").exists() else []
    commits = [c for c in _git_lines(mem, "log", "--format=%h %s") if not c.split(" ", 1)[-1].startswith("S000")] \
        if (mem / ".git").exists() else []
    try:
        recorded = [x for x in _save_record_path(pt).read_text().splitlines() if x.strip()]
    except OSError:
        recorded = []
    after = st.get("saves_after")
    try:
        saves = [s.folder for s in sorted(rcode.list_saves(), key=lambda s: s.mtime) if after and s.mtime >= after]
    except Exception:
        saves = []
    if not (started or finished or streams or open_sid or dirty or commits or recorded):
        return None
    return {"playthrough": pt, "started": started, "finished_sessions": finished, "streams": streams,
            "open_session": open_sid, "uncommitted": dirty, "commits": commits, "recorded_saves": recorded,
            "saves_since_start": saves, "restartable": not finished}


def half_run_lines(h: dict, forced: bool = False, active: bool = False) -> list[str]:
    """run --plan's warning about a half-played next run, and what `run` would do with it (active: a runner holds
    the lock, so this is most likely the run it is playing right now)."""
    when = datetime.fromtimestamp(h["started"]).strftime("%Y-%m-%d %H:%M:%S") if h.get("started") else "?"
    mem = ", ".join(x for x in (
        f"session {h['open_session']} open" if h.get("open_session") else "",
        f"{len(h['uncommitted'])} uncommitted change(s)" if h.get("uncommitted") else "",
        f"{len(h['commits'])} commit(s) past S000" if h.get("commits") else "") if x) or "template only"
    out = [f"HALF-PLAYED:    {h['playthrough']} started {when} and has no record: "
           f"{len(h['finished_sessions'])} finished play session(s) {h['finished_sessions'] or ''}".rstrip(),
           f"                streams {', '.join(h['streams']) or 'none'}; memory: {mem}",
           f"                saves/{h['playthrough']}.txt: {len(h['recorded_saves'])} entr(y/ies)"
           + (f" ({', '.join(h['recorded_saves'][-3:])})" if h["recorded_saves"] else "")
           + f"; {len(h['saves_since_start'])} save(s) since its start"
           + (f" ({', '.join(h['saves_since_start'][-4:])})" if h["saves_since_start"] else "")]
    if active:
        out.append("                (a runner holds the lock: this is likely the run it is playing now; nothing to do)")
    elif forced or h["restartable"]:
        out.append(f"                -> run RESTARTS it ({'--restart-run' if forced else 'no finished play session'}): "
                   f"moves runtime/sessions/{h['playthrough']}, playthroughs/{h['playthrough']} and "
                   f"saves/{h['playthrough']}.txt aside to *.restarted-<time>, then a fresh start")
    else:
        out.append("                -> run CONTINUES it from its last save (its wall time counts from the start "
                   "above); --restart-run starts it over")
    return out


def archive_half_run(base: str, pt: str, stamp: str | None = None) -> list[str]:
    """Moves a half-played run aside so `run` starts it from scratch: runtime/sessions/<pt>, playthroughs/<pt> and
    runtime/saves/<pt>.txt → <name>.restarted-<stamp> (nothing deleted). Run 1 shares the series' session dir: its
    series.json, runs.jsonl and runner_events.jsonl stay. Returns what moved (as 'src -> dst')."""
    stamp = stamp or time.strftime("%Y%m%d-%H%M%S")
    moved = []
    for root in (renv.RUNTIME / "sessions", renv.PLAYTHROUGHS):
        src = root / pt
        if not src.exists():
            continue
        dst = root / f"{pt}.restarted-{stamp}"
        if root == renv.RUNTIME / "sessions" and pt == base:
            dst.mkdir(parents=True, exist_ok=True)
            for f in sorted(src.iterdir()):
                if f.name not in SERIES_DIR_KEEP:
                    f.rename(dst / f.name)
        else:
            src.rename(dst)
        moved.append(f"{src} -> {dst}")
    rec = _save_record_path(pt)
    if rec.exists():
        dst = rec.with_name(f"{rec.stem}.restarted-{stamp}.txt")
        rec.rename(dst)
        moved.append(f"{rec} -> {dst}")
    return moved


def restart_half_run(opt: rstate.Options, base: str, pt: str, k: int, forced: bool) -> dict | None:
    """Before run k's Runner exists: a half-played run whose runner died without a handover (a machine or runner
    crash) and that has no finished play session is started over (forced: --restart-run, whatever it has). Its
    game, when this harness still owns it, is stopped first. Never for this process's own execv successor (a
    pending handover: same run, new code) or a dry run. Returns the half-run state it archived, or None."""
    h = half_run_state(pt)
    if h is None or opt.dry_run or rcode.peek_handover() is not None or not (forced or h["restartable"]):
        return None
    try:
        old = lpt.Runner(replace(opt, playthrough=pt, run_index=k))
        if old.owns_game():
            old.kill_game()
    except Exception as e:
        renv.log(f"series {base}: stopping run {k}'s old game failed: {e!r}")
    moved = archive_half_run(base, pt)
    when = datetime.fromtimestamp(h["started"]).strftime("%Y-%m-%d %H:%M:%S") if h.get("started") else "?"
    renv.log(f"series {base}: run {k} ({pt}) RESTARTED from scratch "
        f"({'--restart-run' if forced else 'its runner died without a handover and no play session finished'}; "
        f"started {when}, {len(h['finished_sessions'])} finished session(s), "
        f"streams {', '.join(h['streams']) or 'none'}, "
        f"memory session {h['open_session'] or 'closed'}, {len(h['recorded_saves'])} recorded save(s)); moved aside: "
        + "; ".join(moved))
    h["moved"] = moved
    return h


def stop_game_at_end(r: lpt.Runner | None, opt: rstate.Options, when: str) -> bool:
    """An --until run or a series that is over stops its harness game, as between runs (--keep-game: left up; a dry
    run and continuous play never). Not reached on a self-restart (RestartRunner leaves run() by exception)."""
    if r is None or not opt.until or opt.keep_game or opt.dry_run:
        return False
    r.end_run_game(when, "--keep-game leaves it up")
    return True


def run_series(opt: rstate.Options, n: int, current: dict, allow_drift: bool = False, scenario: dict | None = None,
               save_sha1: str | None = None) -> int:
    """run --until M --runs N: runs 1..N of the series opt.series, each a new playthrough (bseries.run_name) and a new
    game, one runs.jsonl record per run in runtime/sessions/<base>/. Resumes at the first run without a record (a
    restart, by hand or on changed code, continues the series). Between runs the game is stopped (end_run_game).
    Stops early on an operator stop or on something a next run can't fix (stale bg3le, a runner error)."""
    from new_playthrough import SetupError
    base = opt.series
    path = renv.RUNTIME / "sessions" / base / "runs.jsonl"
    rc, prev, first = 0, None, True
    while not current["stop"]:
        k = bseries.next_run_index(bseries.series_records(path, base))
        if k > n:
            break
        if prev is not None:
            prev.end_run_game()
        pt = bseries.run_name(base, k)
        restarted = restart_half_run(opt, base, pt, k, forced=opt.restart_run and first)
        first = False
        try:
            prepare_series_run(base, pt, k, scenario)
        except (SetupError, OSError) as e:
            renv.log(f"series {base}: can't set up run {k} ({pt}): {e}")
            renv.log(f"runner stopped (series setup failed: run {k})")   # RUN-10: every exit path logs it
            return 1
        r = current["runner"] = lpt.Runner(replace(opt, playthrough=pt, run_index=k))
        if restarted:
            r.event("run_restarted", started=restarted.get("started"), finished_sessions=restarted["finished_sessions"],
                    streams=restarted["streams"], moved=restarted["moved"])
        if current["stop"]:
            r.stop_requested = True
        pins = current_pins()
        freeze_series(base, n, opt.until, r.profile, pins, {k_: getattr(opt, k_) for k_ in bseries.CAP_KEYS},
                      bscenario.scenario_freeze(scenario, save_sha1) if scenario else None,
                      opt.judge_model if opt.judge else None, opt.client,
                      bseries.sandbox_mode(opt.sandbox, bool(opt.box and opt.box.unfirewalled)))
        refusal = gate_series_run(base, r, pins, allow_drift)
        if refusal:
            renv.log(refusal)
            print(refusal, file=sys.stderr)
            renv.log("runner stopped (version drift: run refused)")   # operator scripts wait for it
            return 2
        opt = replace(opt, series_end=r.opt.series_end)   # a self-restart's handover keeps the series budget
        renv.log(f"series {base}: run {k}/{n} ({pt}, --until {opt.until})")
        rc = r.run()
        if r.outcome not in runs.OUTCOMES or r.fatal or r.stop_requested:
            renv.log(f"series {base}: stopping after run {k} ({r.outcome or 'no outcome'}"
                + ("; needs a human" if r.fatal else "") + ")")
            break
        prev = r
    stop_game_at_end(current["runner"], opt, "series end")
    recs = bseries.series_records(path, base)
    if recs:
        renv.log(f"series {base}: {len(recs)}/{n} run(s) recorded, {sum(1 for x in recs if x.get('clean'))} clean, "
            f"{brecords.clean_streak(recs)} clean in a row (python3 runner/play.py runs --playthrough {base})")
    return rc


def exec_new_runner(lock, skip_stop, execv=os.execv) -> None:
    """Replace this process with the new runner code: same pid (the game's pgid bookkeeping and the status file
    stay valid), same argv. The lock and skipper are released first; the successor takes them again at once."""
    if skip_stop is not None:
        skip_stop.set()
    try:
        lock.close()
    except OSError:
        pass
    sys.stdout.flush()
    sys.stderr.flush()
    execv(sys.executable, rcode.restart_argv())


class SeriesSide:
    def runs_path(self) -> Path:
        """runs.jsonl of this run: the series' (runtime/sessions/<base>/) for --runs, else the playthrough's."""
        return renv.RUNTIME / "sessions" / self.opt.series / "runs.jsonl" if self.opt.series else self.state.dir / "runs.jsonl"

    def write_run_record(self, run_started: float, outcome: str = "reached", why: str = "") -> dict:
        """runs.jsonl: one line per --until run (the benchmark's raw score), whatever the outcome, with the version
        pins the run started with and its caps. A --runs run also gets its knowledge/ edits put back (FAIR-2) and
        noted in the record."""
        cap = (self.cap_hit or {}).get("cap") if outcome == "over_budget" else None
        rec = run_record(self.state.dir, run_started, time.time(), self.until_hit or {}, self.opt.playthrough,
                         self.state["history"] or [], outcome, why, self.opt.take_over, cap)
        play = self.model_for()[0]
        rec.update(model=play, effort=self.model_for()[1],
                   keeper_model=rpolicy.keeper_model(self.opt.keeper_model, play) if self.opt.keeper else None)
        cur = self.state["run"] or {}
        rec.update(caps=dict(self.caps(), max_cost=self.opt.max_cost), pins=cur.get("pins") or self.opt.pins,
                   version_drift=cur.get("version_drift", self.opt.version_drift) or None,
                   cap_changes=self.opt.cap_changes or None, spend_cap=self.opt.spend_cap)
        if self.opt.scenario:
            rec["scenario"] = self.opt.scenario
        if self.opt.scenario_settings:
            rec["scenario_settings"] = self.opt.scenario_settings
            rec["timing"] = self.opt.scenario_settings.get("timing")
        rec["model_time"] = runs.model_time_field(model_time.model_time(self.state.dir, since=run_started,
                                                                         until=rec["t"]))
        if self.opt.judge_change:
            rec["judge_change"] = self.opt.judge_change
        rec["client"] = self.opt.client
        if self.opt.profile is not None:   # the condition: its profile file, and the system text pi got (D2)
            rec["profile"] = self.opt.profile.source
        if self.opt.manifest:
            rec["manifest"] = self.opt.manifest
        if self.opt.system_file and Path(self.opt.system_file).exists():
            rec["system_sha256"] = client_setup.sha256(Path(self.opt.system_file))
        if getattr(self, "box", None) is not None:
            rec.update(self.box.record())   # sandbox=container (+ sandbox_unfirewalled)
        if self.opt.client_change:
            rec["client_change"] = self.opt.client_change
        if self.opt.sandbox_change:
            rec["sandbox_change"] = self.opt.sandbox_change
        if self.opt.series:
            rec = {"series": self.opt.series, "run": self.opt.run_index, **rec}
            run = self.state["run"] or {}
            if run.get("reset"):
                rec["reset"] = run["reset"]
            k = self.restore_knowledge(run_started)
            if k is not None:
                rec["knowledge"] = k
                # FAIR-2: knowledge/ is shared by every run; one that changed during the run (put back or not)
                # isn't comparable with the others in its series
                if k.get("agent_edits") or k.get("other_changes"):
                    rec["clean"] = False
                    rec["knowledge_changed"] = True
        if getattr(self, "game_settings", None) is not None:
            rec["game_settings"] = self.game_settings
        if self.opt.seed is not None:
            rec["seed"] = self.opt.seed
        self.scenario_undo()
        self.end_facts(outcome)
        path = self.runs_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a") as f:
                f.write(json.dumps(rec) + "\n")
        except OSError as e:
            renv.log(f"run record write failed: {e}")
        self.state["run"] = None   # this run is over; a later --until run of the playthrough starts its own
        renv.log(f"run record: {outcome} ({rec['milestone'] or why or '-'}) in {rec['wall_min']} min, {rec['sessions']} "
            f"session(s), ${rec['cost_usd']:.2f}, {rec['crashes']} game crash(es)/hang(s), {rec['recoveries']} "
            f"recoveries, {rec['reloads']} agent reload(s); failures harness {rec['harness_failures']} / agent "
            f"{rec['agent_failures']} / unknown {rec['unknown_failures']}; clean={rec['clean']} → {path}")
        if self.opt.judge:   # last: it costs time and money, and the record above is already safe on disk
            self.judge_run_record(path, rec)
        return rec

    def judge_run_record(self, path: Path, rec: dict) -> None:
        """--judge: the persona judge on the run just recorded, merged into its runs.jsonl line. Never raises."""
        model = self.opt.judge_model or judging.DEFAULT_MODEL
        renv.log(f"persona judge ({model}) on run {rec.get('run') or 1} ({rec['playthrough']}) ...")
        rec["persona"] = judge_record(self.state.dir, rec, model, stop=lambda: self.abort)
        if not bseries.update_record(path, rec, {"persona": rec["persona"]}):
            renv.log(f"persona: could not update the record in {path} (play.py judge --playthrough "
                f"{self.opt.series or self.opt.playthrough} redoes it)")
        renv.log("persona: " + lcli.persona_line(rec["persona"]))

    def begin_run(self, now: float) -> float:
        """--until: the run's start, kept in runner.json "run" across runner restarts until its record is written.
        A --runs run on a fresh playthrough first gets reset_for_run()."""
        cur = self.state["run"]
        if cur and cur.get("started"):
            return cur["started"]
        rec = {"started": now, "series": self.opt.series, "index": self.opt.run_index, "pins": self.opt.pins,
               "version_drift": self.opt.version_drift}
        if self.opt.series and not self.state["history"] and not self.opt.dry_run:
            rec["reset"] = self.reset_for_run(now)
        if not self.opt.dry_run:
            self.state["run"] = rec
        return now

    def reset_for_run(self, now: float) -> dict:
        """Before a --runs run's first session: nothing a new playthrough would inherit from an earlier one.
        - saves: only saves written from now on are this playthrough's (saves_after; runner and MCP server)
        - the mod's navigation kill list and breadcrumb mirrors (Script Extender/BG3AI/nav_*.json) are moved
          into runtime/sessions/<pt>/reset/ (NAV-3)
        - play/knowledge is snapshotted; the agent's edits are put back after the run (FAIR-2)
        The game process itself is restarted between runs (end_run_game)."""
        self.state["saves_after"] = now
        moved = bresets.archive_nav_state(renv.SE_BG3AI_DIR, self.state.dir / "reset")
        snap = bresets.snapshot_tree(renv.KNOWLEDGE_DIR)
        try:
            (self.state.dir / "knowledge_snapshot.json").write_text(json.dumps(snap))
        except OSError as e:
            renv.log(f"knowledge snapshot failed: {e}")
        renv.log(f"run {self.opt.run_index} of {self.opt.series}: fresh start for {self.opt.playthrough} (saves from "
            f"{datetime.fromtimestamp(now):%H:%M:%S} on; nav state moved aside: {', '.join(moved) or 'none'}; "
            f"knowledge/ snapshot of {len(snap)} files)")
        self.event("run_reset", nav_files=moved, knowledge_files=len(snap))
        return {"nav_files": moved, "knowledge_files": len(snap)}

    def restore_knowledge(self, run_started: float) -> dict | None:
        """FAIR-2: knowledge/ files the agent edited during the run go back to the snapshot (its versions are kept
        in runtime/sessions/<pt>/knowledge_edits/); other changes are only reported. None: no snapshot."""
        try:
            snap = json.loads((self.state.dir / "knowledge_snapshot.json").read_text())
        except (OSError, ValueError):
            return None
        streams = []
        for f in sorted(self.state.dir.glob("S*.jsonl")):
            try:
                if runs.CLAUDE_STREAM_RE.match(f.name) and f.stat().st_mtime >= run_started:
                    streams.append(f)
            except OSError:
                continue
        res = bresets.restore_agent_edits(renv.KNOWLEDGE_DIR, snap, bresets.agent_written(streams, renv.KNOWLEDGE_DIR, renv.PLAY_DIR),
                                       self.state.dir / "knowledge_edits")
        if res["agent_edits"] or res["other_changes"]:
            renv.log(f"knowledge/ after the run: agent edits put back: {', '.join(res['agent_edits']) or 'none'}; "
                f"other changes (not reverted): {', '.join(res['other_changes']) or 'none'}")
        return res

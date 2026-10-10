"""All read-only live checks in one command (≤ 2 min), for the orchestrator between sessions.

    uv run --project bridge python tools/dev/check_all.py [--skip a,b] [--only a,b] [--budget 120] [--calls 50] [-v]

Refuses to run while a session is playing: runtime/runner.lock held by a runner, or a `claude` process working
in play/ (exit 3). The runner itself runs it between sessions with BG3AI_CHECK_ALL_RUNNER_PID=<its pid>: a lock
held by that pid (the pid the lock file names) is then not a blocker; a claude in play/ still is. --json PATH
also writes the rows and counts (the runner appends the table to the session's triage report). Nothing here moves a character, saves, loads or hot-reloads. The only UI it touches: the
`waypoints` check opens the map (M) and closes it, and only on the plain game view out of combat.

Checks, in order (latency first, so nothing else is in flight while it measures):
  latency     look / get_status / get_nearby through the MCP tool functions, --calls each: p50/p90 (goal: bridge
              read round trip < 100 ms). PASS p50 < 100 and p90 < 250; FAIL p50 > 300 or any FAILED reply.
  waypoints   fast_travel's list: state.map's unlocked waypoints vs the map UI's list (ui.waypoints), and each
              one (by name and by WAYP id) resolves to itself through the mod's SV.pickWaypoint. Empty UI list
              = the S007 bug (FAIL).
  keys        press_key dry run: UIActions.keySpec maps DELETE/1/ENTER/ctrl+s/... to bg3se SDLScanCode labels,
              and each label exists in Ext.Enums.SDLScanCode / SDLKeyModifier. Nothing is pressed.
  confirm     confirm-dialog detection: UIActions.confirmBox() runs, and when a box is open, which button
              confirm("yes"/"no") would press (ui.confirm dry=true; nothing is pressed).
  smoke       tools/dev/smoke.py (every read tool)
  act         tools/dev/check_act.py latency approach forms (its read-only default)
  vis         tools/dev/check_vis.py (no --move/--reload; it toggles the ray rule and restores it)
  se          tools/dev/check_se.py --only hook,children,autoskip
  route       tools/dev/check_route.py
  mod         the hot-reloaded mod Lua is the new one: Navigation.lua's connector budget/memo, the nav.waypoints
              and act.watch ops answer, nav.activity carries party_turn in combat (research/live-todo.md)
  reads       tools/dev/check_reads.py --rounds 3 --queue: its connections line (bridge pools within cap, no bg3le
              debug slot without a client socket = the leak) decides; its strict read target (<30 ms) only WARNs
  session     tools/dev/check_session.py: the newest finished session's own files (latency.json, transcript,
              metrics, saves, runner.log, bg3le log) against the live-todo items they answer. No game access.

Prints one PASS / WARN / FAIL table (one row per check, the worst line of a sub-script decides its row) and
exits 1 on any FAIL, 2 when the game isn't reachable, 3 when a session is playing. -v prints the sub-scripts'
own output too.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import statistics
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

REPO = Path(__file__).resolve().parents[2]
DEV = REPO / "tools" / "dev"
RUNTIME = Path(os.environ.get("BG3AI_RUNTIME", REPO / "runtime"))
PLAY_DIR = Path(os.environ.get("BG3AI_PLAY_DIR", REPO / "play"))
sys.path.insert(0, str(REPO / "bridge" / "src"))
ENV0 = dict(os.environ)  # before main() points BG3AI_RUNTIME at a scratch dir for its own bridge import

RANK = {"PASS": 0, "INFO": 0, "SKIP": 0, "WARN": 1, "FAIL": 2}

# Read tools' latency verdicts (ms).
P50_PASS, P90_PASS, P50_FAIL = 100.0, 250.0, 300.0

# press_key dry run: what the agent types -> (scancode label, modifiers) the mod must produce.
KEY_CASES = [
    ("DELETE", "DEL", None), ("1", "NUM_1", None), ("0", "NUM_0", None), ("ENTER", "RETURN", None),
    ("ESCAPE", "ESCAPE", None), ("SPACE", "SPACE", None), ("TAB", "TAB", None), ("F5", "F5", None),
    ("PGDN", "PAGEDOWN", None), ("BACKSPACE", "BACKSPACE", None), ("LEFT", "LEFT", None),
    ("NUMPAD_1", "KP_1", None), ("ctrl+s", "S", ["LCtrl"]), ("Shift-Tab", "TAB", ["LShift"]), ("I", "I", None),
]

# Sub-scripts: name -> (argv after the script, timeout s, env overrides). "TMP" = a scratch runtime dir, so the
# bridge's actions.jsonl / metrics.jsonl stay clean (check_se reads the real runtime/logs; check_vis sets its own).
SCRIPTS = {
    "smoke": (["smoke.py"], 40, {"BG3AI_RUNTIME": "TMP"}),
    "act": (["check_act.py", "latency", "approach", "forms"], 30, {"BG3AI_RUNTIME": "TMP"}),
    "vis": (["check_vis.py"], 40, {}),
    "se": (["check_se.py", "--only", "hook,children,autoskip"], 25, {}),
    "route": (["check_route.py"], 25, {}),
    "reads": (["check_reads.py", "--rounds", "3", "--queue"], 30, {}),
    "session": (["check_session.py"], 20, {}),
}
ORDER = ["latency", "waypoints", "keys", "confirm", "smoke", "act", "vis", "se", "route", "mod", "reads", "session"]


@dataclass
class Row:
    name: str
    verdict: str
    detail: str
    secs: float = 0.0
    lines: list[str] = field(default_factory=list)  # the WARN/FAIL lines behind the verdict


def worst(verdicts) -> str:
    return max(verdicts, key=lambda v: RANK.get(v, 2), default="PASS")


# ---------------------------------------------------------------------------------------- session guard

def runner_lock_held(lock: Path) -> bool:
    """True when a live runner holds runtime/runner.lock (flock; released when the runner dies)."""
    if not lock.exists():
        return False
    try:
        with open(lock, "a") as f:  # "a": never truncate the runner's pid
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(f, fcntl.LOCK_UN)
    except OSError:
        return False
    return False


def proc_table(proc: Path = Path("/proc")):
    """(pid, cmdline argv, cwd) of every process we can see."""
    for d in proc.iterdir():
        if not d.name.isdigit():
            continue
        try:
            argv = [a for a in (d / "cmdline").read_bytes().decode(errors="replace").split("\0") if a]
            cwd = Path(os.readlink(d / "cwd"))
        except OSError:
            continue
        yield int(d.name), argv, cwd


def is_claude(argv: list[str]) -> bool:
    if not argv:
        return False
    if Path(argv[0]).name == "claude":
        return True
    # node .../@anthropic-ai/claude-code/cli.js, or a wrapper script named claude
    return any(Path(a).name == "claude" or "claude-code" in a for a in argv[:3])


def playing_sessions(play_dir: Path = PLAY_DIR, procs=None) -> list[str]:
    """claude processes working in play/ (the runner's sessions, or a manual one)."""
    play = play_dir.resolve()
    me = os.getpid()
    out = []
    for pid, argv, cwd in procs if procs is not None else proc_table():
        if pid == me or not is_claude(argv):
            continue
        if cwd == play or play in cwd.parents:
            out.append(f"pid {pid}: {' '.join(argv)[:80]}")
    return out


def lock_pid(lock: Path) -> int | None:
    try:
        return int(lock.read_text().strip() or "x")
    except (OSError, ValueError):
        return None


def session_blockers(runtime: Path = RUNTIME, play_dir: Path = PLAY_DIR, procs=None,
                     runner_pid: str | None = None) -> list[str]:
    """Why a check can't run now. runner_pid (BG3AI_CHECK_ALL_RUNNER_PID): the runner calling us between
    sessions; its own lock doesn't count, any other holder does."""
    if runner_pid is None:
        runner_pid = os.environ.get("BG3AI_CHECK_ALL_RUNNER_PID")
    why = []
    lock = runtime / "runner.lock"
    if runner_lock_held(lock) and not (runner_pid and str(lock_pid(lock)) == runner_pid.strip()):
        why.append(f"a runner holds {runtime / 'runner.lock'} (stop it: kill -TERM <runner pid>)")
    why += [f"a claude session is playing in {play_dir.name}/ ({p})" for p in playing_sessions(play_dir, procs)]
    return why


# ---------------------------------------------------------------------------------------- sub-scripts

LINE_RE = re.compile(r"^\s*(?:\[\s*(PASS|WARN|FAIL|INFO)\s*\]|(PASS|WARN|FAIL|INFO)\b)\s*:?\s*(.*)$")


def parse_output(text: str) -> tuple[list[str], list[str]]:
    """Verdict lines of a check script: smoke's ✓/✗ rows, check_act's `FAIL  msg`, check_vis/check_route's
    `[FAIL] check: msg`, check_se's `FAIL name: detail`. -> (verdict per line, the WARN/FAIL lines)."""
    verdicts, bad = [], []
    for raw in text.splitlines():
        line = raw.rstrip()
        m = LINE_RE.match(line)
        if m:
            v = m.group(1) or m.group(2)
        elif line.startswith("✓ "):
            v = "PASS"
        elif line.startswith("✗ "):
            v = "FAIL"
        else:
            continue
        verdicts.append(v)
        if v in ("WARN", "FAIL"):
            bad.append(line.strip())
    return verdicts, bad


def summarize(name: str, rc: int | None, out: str, secs: float) -> Row:
    """Row for one sub-script run (rc None = timed out)."""
    verdicts, bad = parse_output(out)
    v, n = worst(verdicts), len(verdicts)
    if rc is None:
        return Row(name, "FAIL", f"timed out after {secs:.0f} s ({n} lines before)", secs, bad)
    if rc != 0 and v != "FAIL":
        tail = (out.strip().splitlines() or ["no output"])[-1][:120]
        return Row(name, "FAIL", f"exit {rc}: {tail}", secs, bad)
    if n == 0:
        return Row(name, "WARN" if rc == 0 else "FAIL", "no PASS/WARN/FAIL lines in its output", secs, bad)
    detail = f"{n} lines, {verdicts.count('FAIL')} FAIL, {verdicts.count('WARN')} WARN"
    if bad:
        detail += ": " + bad[0][:110]
    return Row(name, v, detail, secs, bad)


def run_script(name: str, timeout: float, tmp: Path, runner=None) -> tuple[int | None, str, float]:
    argv, _, env_over = SCRIPTS[name]
    env = dict(ENV0)
    for k, val in env_over.items():
        env[k] = str(tmp) if val == "TMP" else val
    env.setdefault("BG3AI_METRICS_PATH", str(tmp / "metrics.jsonl"))
    t0 = time.monotonic()
    try:
        p = (runner or subprocess.run)([sys.executable, str(DEV / argv[0]), *argv[1:]], cwd=str(REPO), env=env, capture_output=True,
                   text=True, timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or ""), time.monotonic() - t0
    except subprocess.TimeoutExpired as e:
        out = e.stdout.decode(errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
        return None, out, time.monotonic() - t0


# ---------------------------------------------------------------------------------------- in-process checks

def pct(xs: list[float], q: float) -> float:
    s = sorted(xs)
    return s[min(len(s) - 1, max(0, round(q * (len(s) - 1))))]


def check_latency(tools: dict[str, Callable[[], str]], calls: int) -> Row:
    parts, verdicts, bad = [], [], []
    for name, fn in tools.items():
        ms, fails = [], 0
        first_fail = ""
        for _ in range(calls):
            t = time.perf_counter()
            try:
                out = fn()
            except Exception as e:  # tools never raise; flag it
                out = f"EXCEPTION {type(e).__name__}: {e}"
            ms.append((time.perf_counter() - t) * 1000)
            if not isinstance(out, str) or out.startswith(("FAILED", "NOT READY", "EXCEPTION")):
                fails += 1
                first_fail = first_fail or str(out)[:100]
        p50, p90 = statistics.median(ms), pct(ms, 0.9)
        v = ("FAIL" if fails or p50 > P50_FAIL else
             "PASS" if p50 < P50_PASS and p90 < P90_PASS else "WARN")
        verdicts.append(v)
        parts.append(f"{name} {p50:.0f}/{p90:.0f}" + (f" ({fails} failed)" if fails else ""))
        if v != "PASS":
            bad.append(f"{v} {name}: p50 {p50:.0f} ms, p90 {p90:.0f} ms, max {max(ms):.0f} ms"
                       + (f"; {fails}/{calls} failed: {first_fail}" if fails else ""))
    return Row("latency", worst(verdicts), f"p50/p90 ms ×{calls}: " + ", ".join(parts), lines=bad)


def lua_str(s: str) -> str:
    """A Lua long-bracket literal for s."""
    n = 0
    while f"]{'=' * n}]" in s:
        n += 1
    return f"[{'=' * n}[{s}]{'=' * n}]"


def client_json(g, body: str, timeout: float = 8.0):
    """Run a client chunk (sets `out`) with A = UIActions, SV = Services; return decoded JSON."""
    code = ("local B = Mods.BG3AI.BG3AI\nlocal A, SV = B.UIActions, B.Services\nlocal out\n" + body +
            "\nreturn Ext.Json.Stringify(out, { Beautify = false })")
    raw = g.eval(code, context="client", timeout=timeout)
    return json.loads(raw) if raw not in ("", "nil") else None


KEYS_LUA = r"""
local cases = Ext.Json.Parse(%s)
local E = Ext.Enums or {}
local SC, KM = E.SDLScanCode, E.SDLKeyModifier
local function has(enum, label)
  if not enum then return nil end
  local ok, v = pcall(function() return enum[label] end)
  return ok and v ~= nil
end
out = { enums = SC ~= nil, keyspec = A ~= nil and A.keySpec ~= nil, cases = {} }
if out.keyspec then
  for _, c in ipairs(cases) do
    local k, mods = A.keySpec(c[1])
    local mok = true
    for _, m in ipairs(mods or {}) do if has(KM, m) == false then mok = false end end
    local ex = has(SC, k)
    out.cases[#out.cases + 1] = { key = c[1], label = k, mods = mods or false, exists = ex == nil and "?" or ex, mods_ok = mok }
  end
end
"""


def check_keys(g) -> Row:
    res = client_json(g, KEYS_LUA % lua_str(json.dumps([[k] for k, _, _ in KEY_CASES])))
    if not res or not res.get("keyspec"):
        return Row("keys", "FAIL", "UIActions.keySpec missing (old UIActions.lua loaded? hot reload the mod)")
    want = {k: (label, mods) for k, label, mods in KEY_CASES}
    bad, verdicts = [], []
    for c in res["cases"]:
        key, label, mods, exists, mods_ok = c["key"], c["label"], c.get("mods") or None, c.get("exists"), c.get("mods_ok")
        exp = want.get(key)
        if exp and (label, mods) != exp:
            verdicts.append("FAIL")
            bad.append(f"FAIL {key!r} -> {label} {mods} (want {exp[0]} {exp[1]})")
        elif exists is False or mods_ok is False:
            verdicts.append("FAIL")
            bad.append(f"FAIL {key!r} -> {label} {mods}: not in Ext.Enums.SDLScanCode/SDLKeyModifier")
        else:
            verdicts.append("PASS")
    v = worst(verdicts)
    if v == "PASS" and not res.get("enums"):
        v = "WARN"
        bad.append("WARN Ext.Enums.SDLScanCode not readable: labels checked against the mapping only")
    detail = f"{len(res['cases']) - len([b for b in bad if b.startswith('FAIL')])}/{len(res['cases'])} keys map"
    return Row("keys", v, detail + (": " + bad[0] if bad else " to real scancode labels (nothing pressed)"), lines=bad)


CONFIRM_LUA = r"""
local box = A and A.confirmBox and A.confirmBox()
out = { api = A ~= nil and A.confirmBox ~= nil and A.pickConfirm ~= nil }
if box then
  local labels = {}
  for _, b in ipairs(box.buttons) do labels[#labels + 1] = b.label end
  local y, n = A.pickConfirm(box.buttons, "yes"), A.pickConfirm(box.buttons, "no")
  out.box = { name = box.name, title = box.title, buttons = labels,
              yes = y and y.label or false, no = n and n.label or false }
end
"""


def check_confirm(g) -> Row:
    res = client_json(g, CONFIRM_LUA)
    if not res or not res.get("api"):
        return Row("confirm", "FAIL", "UIActions.confirmBox/pickConfirm missing (old UIActions.lua loaded?)")
    box = res.get("box")
    if not box:
        return Row("confirm", "PASS", "detection runs; no confirmation box open now")
    btns = ", ".join(map(str, box.get("buttons") or []))
    if not box.get("yes"):
        return Row("confirm", "FAIL", f"{box.get('name')} [{btns}]: no button answers 'yes'")
    try:
        dry = g.rpc("ui.confirm", {"answer": "yes", "dry": True}, context="client")
        via = dry.get("via") if isinstance(dry, dict) else dry
    except Exception as e:  # noqa: BLE001 - reported, not raised
        return Row("confirm", "FAIL", f"{box.get('name')} [{btns}]: ui.confirm dry failed: {e}")
    return Row("confirm", "PASS", f"{box.get('name')} [{btns}]: yes -> {box['yes']} via {via}, no -> {box.get('no')}")


PICK_LUA = r"""
local list, wants = Ext.Json.Parse(%s), Ext.Json.Parse(%s)
out = { api = SV ~= nil and SV.pickWaypoint ~= nil, picks = {} }
if out.api then
  for _, w in ipairs(wants) do
    local wp, n = SV.pickWaypoint(list, w)
    out.picks[#out.picks + 1] = { want = w, got = wp and wp.name or false, n = n or 0 }
  end
end
"""

QUIET_SCREENS = ("game",)


def check_waypoints(g) -> Row:
    st = g.rpc("state.status") or {}
    unlocked = (g.rpc("state.map") or {}).get("waypoints") or []
    if not unlocked:
        return Row("waypoints", "PASS", "no waypoint unlocked yet (nothing to list)")
    ui = g.try_rpc("ui.state", {}, context="client", default={}) or {}
    screen = ui.get("screen", "?") if isinstance(ui, dict) else "?"
    combat = any((p or {}).get("in_combat") for p in st.get("party") or []) or st.get("in_combat")
    if screen not in QUIET_SCREENS or combat:
        return Row("waypoints", "WARN", f"skipped the map UI ({'in combat' if combat else 'screen ' + screen}); "
                   f"{len(unlocked)} unlocked in state.map")
    r = g.run_job("ui.waypoints", {}, context="client", wait_s=8.0)
    if r.status != "done":
        return Row("waypoints", "FAIL", f"ui.waypoints {r.status}: {r.error or r.result}")
    listed = (r.result or {}).get("waypoints") or []
    if not listed:
        return Row("waypoints", "FAIL", f"the map UI listed no waypoints ({len(unlocked)} unlocked in state.map): "
                   "fast_travel would say 'no waypoint list'")
    wants = [w.get("name") for w in unlocked if w.get("name")] + [w.get("id") for w in unlocked if w.get("id")]
    res = client_json(g, PICK_LUA % (lua_str(json.dumps(listed)), lua_str(json.dumps(wants))))
    if not res or not res.get("api"):
        return Row("waypoints", "FAIL", "Services.pickWaypoint missing (old Services.lua loaded?)")
    misses = [f"{p['want']!r} -> none ({p.get('n', 0)} matches)" for p in res["picks"] if not p.get("got")]
    blocked = (r.result or {}).get("travel_blocked")
    detail = f"map UI lists {len(listed)}, state.map {len(unlocked)}; {len(wants) - len(misses)}/{len(wants)} names+ids resolve"
    if blocked:
        detail += f"; travel blocked now ({blocked})"
    if misses:
        return Row("waypoints", "FAIL" if len(misses) * 2 > len(wants) else "WARN", detail + ": " + "; ".join(misses[:3]),
                   lines=[f"WARN {m}" for m in misses])
    return Row("waypoints", "PASS", detail)


MOD_LUA = r"""
local B = Mods.BG3AI.BG3AI
local N, R = B.Nav, B.RPC
out = { nav = N ~= nil, budget = N and N.CONNECT_BUDGET_MS or false,
        memo = N ~= nil and type(N.connectFail) == "table",
        ops = { ["nav.waypoints"] = R ~= nil and R.ops["nav.waypoints"] ~= nil,
                ["act.watch"] = R ~= nil and R.ops["act.watch"] ~= nil,
                ["nav.activity"] = R ~= nil and R.ops["nav.activity"] ~= nil } }
"""


def check_mod(g) -> Row:
    """Markers of the mod Lua the live-todo items need (they only exist after the runner's hot reload)."""
    raw = g.eval("local out\n" + MOD_LUA + "\nreturn Ext.Json.Stringify(out, { Beautify = false })", context="server")
    res = json.loads(raw) if raw not in ("", "nil") else {}
    bad, parts = [], []
    if not res.get("nav"):
        return Row("mod", "FAIL", "BG3AI.Nav missing on the server (mod not loaded?)")
    if res.get("budget") != 40 or not res.get("memo"):
        bad.append(f"FAIL Navigation.lua is old: CONNECT_BUDGET_MS={res.get('budget')}, connectFail={res.get('memo')}")
    for op, ok in sorted((res.get("ops") or {}).items()):
        if not ok:
            bad.append(f"FAIL op {op} not registered (old mod Lua: hot reload)")
    if not any(b.startswith("FAIL op nav.waypoints") for b in bad):
        wp = g.try_rpc("nav.waypoints", {}, default=None)
        if isinstance(wp, dict) and isinstance(wp.get("waypoints"), (list, dict)):
            parts.append(f"nav.waypoints {len(wp['waypoints'])} unlocked here (level {wp.get('level')})")
        else:
            bad.append(f"WARN nav.waypoints answered {str(wp)[:80]}")
    if not any(b.startswith("FAIL op act.watch") for b in bad):
        w = g.try_rpc("act.watch", {}, default=None)
        if isinstance(w, dict) and isinstance(w.get("party"), list):
            seen = w.get("seen") or []
            parts.append(f"act.watch party {len(w['party'])}, seen {len(seen)} "
                         f"({sum(1 for x in seen if isinstance(x, dict) and x.get('hostile'))} hostile)")
        else:
            bad.append(f"WARN act.watch answered {str(w)[:80]}")
    act = g.try_rpc("nav.activity", {}, default=None)
    if isinstance(act, dict) and act.get("in_combat"):
        if "party_turn" not in act:
            bad.append("FAIL nav.activity has no party_turn in combat (old Navigation.lua)")
        else:
            parts.append(f"party_turn={act['party_turn']}")
    elif isinstance(act, dict):
        parts.append("out of combat (party_turn not exercised)")
    v = worst([b.split()[0] for b in bad]) if bad else "PASS"
    return Row("mod", v, (bad[0] + "; " if bad else "") + "; ".join(parts), lines=bad)


READS_RE = re.compile(r"^(connections|ALL)\b.*->\s*(PASS|FAIL)")


def summarize_reads(rc: int | None, out: str, secs: float) -> Row:
    """check_reads prints `... -> PASS|FAIL` lines: the connections line decides (FAIL = a debug slot leak);
    the ALL line's strict read target (< 30 ms) is a WARN here (check_all's latency row has the goal's FAIL)."""
    lines = {m.group(1): (m.group(2), raw.strip()) for raw in out.splitlines() if (m := READS_RE.match(raw.strip()))}
    if rc is None:
        return Row("reads", "FAIL", f"timed out after {secs:.0f} s", secs)
    if "connections" not in lines:
        tail = (out.strip().splitlines() or ["no output"])[-1][:120]
        return Row("reads", "FAIL" if rc not in (0, 1) else "WARN", f"exit {rc}: {tail}", secs)
    conn_v, conn = lines["connections"]
    all_v, allr = lines.get("ALL", ("PASS", "ALL: no records"))
    v = worst([conn_v, "WARN" if all_v == "FAIL" else "PASS"])
    bad = [f"{conn_v} {conn}"] if conn_v != "PASS" else []
    if all_v == "FAIL":
        bad.append(f"WARN {allr}")
    return Row("reads", v, f"{conn[:90]}; {allr[:70]}", secs, bad)


# ---------------------------------------------------------------------------------------- main

def print_table(rows: list[Row], verbose: bool = False, out=None) -> None:
    out = out or sys.stdout
    w = max([len(r.name) for r in rows] + [5])
    print(f"\n{'check':{w}}  verdict   secs  detail", file=out)
    print(f"{'-' * w}  -------  -----  {'-' * 60}", file=out)
    for r in rows:
        print(f"{r.name:{w}}  {r.verdict:7}  {r.secs:5.1f}  {r.detail}", file=out)
        for line in r.lines[1:6] if not verbose else r.lines[1:]:
            print(f"{'':{w}}  {'':7}  {'':5}    {line[:150]}", file=out)
    n = {v: sum(1 for r in rows if r.verdict == v) for v in ("PASS", "WARN", "FAIL", "SKIP")}
    print(f"\n{n['PASS']} PASS, {n['WARN']} WARN, {n['FAIL']} FAIL" + (f", {n['SKIP']} SKIP" if n["SKIP"] else ""),
          file=out)


def run_all(names: list[str], g, tools_fn, calls: int, budget: float, verbose: bool, tmp: Path,
            runner=None) -> list[Row]:
    deadline = time.monotonic() + budget
    rows = []
    for name in names:
        left = deadline - time.monotonic()
        if left < 5:
            rows.append(Row(name, "SKIP", f"over the {budget:.0f} s budget"))
            continue
        t0 = time.monotonic()
        print(f"… {name}", flush=True)
        try:
            if name == "latency":
                row = check_latency(tools_fn(), calls)
            elif name == "waypoints":
                row = check_waypoints(g)
            elif name == "keys":
                row = check_keys(g)
            elif name == "confirm":
                row = check_confirm(g)
            elif name == "mod":
                row = check_mod(g)
            else:
                rc, out, secs = run_script(name, min(SCRIPTS[name][1], left), tmp, runner)
                if verbose:
                    print(out.rstrip())
                row = summarize_reads(rc, out, secs) if name == "reads" else summarize(name, rc, out, secs)
        except Exception as e:  # noqa: BLE001 - one broken check must not hide the others
            row = Row(name, "FAIL", f"{type(e).__name__}: {str(e)[:140]}")
        row.secs = time.monotonic() - t0
        rows.append(row)
    return rows


def server_tools() -> dict[str, Callable[[], str]]:
    from bg3ai import server as S
    return {"look": S.look, "get_status": S.get_status, "get_nearby": S.get_nearby}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", default="", help="comma list: " + ",".join(ORDER))
    ap.add_argument("--skip", default="", help="comma list of checks to skip")
    ap.add_argument("--budget", type=float, default=120.0, help="wall-clock budget in seconds")
    ap.add_argument("--calls", type=int, default=50, help="calls per read tool in the latency check")
    ap.add_argument("-v", "--verbose", action="store_true", help="print the sub-scripts' output")
    ap.add_argument("--json", type=Path, help="also write {rows, counts, exit} here")
    a = ap.parse_args(argv)
    only = [x for x in a.only.split(",") if x]
    skip = set(x for x in a.skip.split(",") if x)
    unknown = (set(only) | skip) - set(ORDER)
    if unknown:
        ap.error(f"unknown check(s): {', '.join(sorted(unknown))}")
    names = [n for n in ORDER if (not only or n in only) and n not in skip]

    blockers = session_blockers()
    if blockers:
        print("Refusing to run: a session is playing.\n  " + "\n  ".join(blockers))
        return 3

    tmp = Path(tempfile.mkdtemp(prefix="bg3ai-checkall-"))
    # Our own bridge import (latency) must not write the real runtime's actions/metrics logs.
    os.environ["BG3AI_RUNTIME"] = str(tmp)
    os.environ.setdefault("BG3AI_METRICS_PATH", str(tmp / "metrics.jsonl"))
    from bg3ai.game import Game
    g = Game(port=int(os.environ.get("BG3LE_DEBUG_PORT", "9998")))
    if not g.server_ready():
        print("Game/mod not reachable on the LuaDebug port (launch the game and load a save first).")
        return 2
    rows = run_all(names, g, server_tools, a.calls, a.budget, a.verbose, tmp)
    print_table(rows, a.verbose)
    rc = 1 if any(r.verdict == "FAIL" for r in rows) else 0
    if a.json:
        write_json(a.json, rows, rc)
    return rc


def write_json(path: Path, rows: list[Row], rc: int) -> None:
    counts = {v: sum(1 for r in rows if r.verdict == v) for v in ("PASS", "WARN", "FAIL", "SKIP")}
    path.write_text(json.dumps({"t": time.time(), "exit": rc, "counts": counts,
                                "rows": [{"name": r.name, "verdict": r.verdict, "secs": round(r.secs, 1),
                                          "detail": r.detail, "lines": r.lines[:6]} for r in rows]}, indent=1))


if __name__ == "__main__":
    sys.exit(main())

"""Benchmark runs (`play.py run --until M --runs N`): run naming, per-run resets, failure tagging, the runs table.

Stdlib only (plus runner/triage.py and runner/fairness.py, which are stdlib + bg3ai.metrics). play.py owns the
run loop; everything here is a pure function over files the runner and the bridge already write, so it is unit
tested without a game.

Failure classes (every failure in a run record is one of them):
    harness   ours: game crash/hang/OOM, launch/load/setup failures, a stale bg3le, the MCP server not connected,
              claude API/usage errors and claude exiting at once, tool exceptions and timeouts, a tool result that
              leaked something a player can't see (fairness LEAK), runner exceptions and runner give-ups
    agent     the model's: stalls with no story progress, party wipes (Game Over), reloads it chose, early exits,
              running out of the run's time, sandbox/fairness-guard blocks of its own file calls
    unknown   can't tell from the logs: claude silent for minutes (a slow model turn or a stalled API), claude
              killed by a signal from outside the runner
Operator actions (a manual restart, a stop, a runner self-restart on changed code, --take-over) are listed apart:
a run with any of them is not clean.

Benchmark comparability: every run record carries its version pins (game build, bg3le, mod, repo) and caps (time,
cost, tokens); a series freezes both in series.json from its first run (play.py refuses a drifted later run unless
--allow-version-drift / --allow-cap-change).
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import statistics
import subprocess
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fairness  # noqa: E402
import triage  # noqa: E402

HARNESS, AGENT, UNKNOWN = "harness", "agent", "unknown"
OUTCOMES = ("reached", "timeout", "over_budget", "gave_up", "runner_error")   # timeout: the time cap; over_budget:
CAPS = ("time", "cost", "tokens")                                                # the cost or token cap (record "cap")

# runner_events.jsonl kinds that are harness failures (the fault kinds are mapped in event_failures)
HARNESS_EVENTS = {
    "game_oom": "game_oom",
    "setup_failed": "setup_failed",
    "recovery_failed": "recovery_failed",
    "launch_failed": "launch_failed",
    "launch_gave_up": "launch_gave_up",
    "load_refused": "load_refused",
    "bg3le_stale": "bg3le_stale",
    "steam_refused": "steam_refused",
    "oom_save_refused": "oom_save_refused",
}
FAULT_KINDS = {"crash": ("game_crash", HARNESS), "hang": ("game_hang", HARNESS), "agent_hang": ("agent_hang", UNKNOWN)}

PLAY_STREAM_RE = re.compile(r"^S\d{3,}(?:-[rc]\d+)?\.jsonl$")   # play sessions and their resumes (crash -rN, stop
                                                                # check -cN; not -fix/-keeper)
CLAUDE_STREAM_RE = re.compile(r"^S\d{3,}(?:-[\w-]+)?\.jsonl$")   # every claude stream, incl. -fix and -keeper
API_ERROR_RE = re.compile(r"API Error|usage limit|rate.?limit|overloaded|credit balance|invalid api key|"
                          r"authentication|\b(?:429|500|502|503|529)\b", re.I)
TOOL_EXCEPTION_RE = re.compile(r"Error executing tool|Traceback \(most recent call last\)|Internal error|MCP error",
                               re.I)
TIMEOUT_RE = re.compile(r"\bTIMEOUT\b|timed out", re.I)
GUARD_BLOCK = "Blocked by the sandbox/fairness rule"
GAME_OVER_RE = re.compile(r"\bgame[_ ]over\b", re.I)
WRITE_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")


def failure(t, kind: str, cls: str, detail: str, n: int = 1) -> dict:
    f = {"t": round(t, 1) if isinstance(t, (int, float)) else None, "kind": kind, "class": cls,
         "detail": str(detail)[:300]}
    if n > 1:
        f["n"] = n
    return f


# ======================================================================== naming

def run_name(base: str, k: int) -> str:
    """Run k of the series `base`: run 1 is the playthrough itself, run k > 1 is `<base>-r<k>`."""
    return base if k <= 1 else f"{base}-r{k}"


def series_records(path: Path, base: str) -> list[dict]:
    """The series' runs.jsonl records (runtime/sessions/<base>/runs.jsonl), in file order."""
    out = []
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return out
    for ln in lines:
        try:
            r = json.loads(ln)
        except ValueError:
            continue
        if isinstance(r, dict) and r.get("series", r.get("playthrough")) == base:
            out.append(r)
    return out


def next_run_index(records: list[dict]) -> int:
    """The run to play next: one after the highest recorded run (a record means that run is over, any outcome)."""
    return max([int(r.get("run") or 1) for r in records], default=0) + 1


# ======================================================================== per-run reset

def archive_nav_state(se_bg3ai: Path, dest: Path) -> list[str]:
    """NAV-3: move the mod's machine-global navigation files (the per-level kill list nav_guard.json and the
    breadcrumb mirrors nav_<host uuid>.json, Script Extender/BG3AI/) into dest, so a new playthrough starts with no
    level killed and no breadcrumbs from an earlier game. Moved, not deleted. Returns the moved names."""
    moved = []
    try:
        files = sorted(p for p in se_bg3ai.glob("nav_*.json") if p.is_file())
    except OSError:
        return moved
    for p in files:
        try:
            dest.mkdir(parents=True, exist_ok=True)
            shutil.move(str(p), str(dest / p.name))
            moved.append(p.name)
        except OSError:
            continue
    return moved


def snapshot_tree(root: Path) -> dict[str, str]:
    """{relative path: text} of every file under root (play/knowledge: ~200 KB of markdown)."""
    out = {}
    if not root.is_dir():
        return out
    for p in sorted(root.rglob("*")):
        if p.is_file():
            try:
                out[str(p.relative_to(root))] = p.read_text(errors="replace")
            except OSError:
                continue
    return out


def tree_changes(root: Path, snap: dict[str, str]) -> dict[str, str]:
    """{relative path: changed|added|removed} between the snapshot and the tree now."""
    now = snapshot_tree(root)
    out = {}
    for rel in sorted(set(snap) | set(now)):
        if rel not in now:
            out[rel] = "removed"
        elif rel not in snap:
            out[rel] = "added"
        elif snap[rel] != now[rel]:
            out[rel] = "changed"
    return out


def agent_written(streams: list[Path], root: Path, cwd: Path) -> set[str]:
    """Paths under root (relative to it) that the playing agent's Edit/Write calls targeted (relative paths are
    resolved from cwd, play/)."""
    out = set()
    root = root.resolve()
    for s in streams:
        for ev in _read_jsonl(s):
            content = (ev.get("message") or {}).get("content") if ev.get("type") == "assistant" else None
            for b in content if isinstance(content, list) else []:
                if not (isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name") in WRITE_TOOLS):
                    continue
                fp = (b.get("input") or {}).get("file_path") or (b.get("input") or {}).get("notebook_path")
                if not isinstance(fp, str) or not fp:
                    continue
                p = Path(fp)
                p = (p if p.is_absolute() else cwd / p).resolve()
                try:
                    out.add(str(p.relative_to(root)))
                except ValueError:
                    continue
    return out


def restore_agent_edits(root: Path, snap: dict[str, str], agent_files: set[str], keep: Path) -> dict:
    """FAIR-2: after a run, put back the knowledge files the agent changed (its version is kept under `keep`).
    Changes nobody can pin on the agent (an operator's edit meanwhile) are reported, never reverted."""
    changes = tree_changes(root, snap)
    restored, other = [], []
    for rel, how in changes.items():
        if rel not in agent_files:
            other.append(f"{rel} ({how})")
            continue
        p = root / rel
        try:
            if p.exists():
                (keep / rel).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(p, keep / rel)
            if how == "added":
                p.unlink(missing_ok=True)
            else:
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(snap[rel])
            restored.append(f"{rel} ({how})")
        except OSError as e:
            other.append(f"{rel} ({how}; restore failed: {e})")
    return {"agent_edits": restored, "other_changes": other}


# ======================================================================== failure tagging

def _read_jsonl(path: Path) -> list[dict]:
    out = []
    try:
        lines = path.read_text(errors="replace").splitlines()
    except OSError:
        return out
    for ln in lines:
        try:
            v = json.loads(ln)
        except ValueError:
            continue
        if isinstance(v, dict):
            out.append(v)
    return out


def event_failures(events: list[dict]) -> tuple[list[dict], list[dict]]:
    """(failures, operator actions) from the run's runner_events.jsonl records."""
    fails, ops = [], []
    starts = 0
    for e in events:
        k, t = e.get("kind"), e.get("t")
        if k == "fault":
            kind, cls = FAULT_KINDS.get(e.get("fault"), (f"fault_{e.get('fault')}", UNKNOWN))
            fails.append(failure(t, kind, cls, f"{e.get('session') or ''}: {e.get('detail') or e.get('fault')}"))
        elif k in HARNESS_EVENTS:
            detail = e.get("error") or e.get("why") or e.get("problems") or e.get("save") or ""
            fails.append(failure(t, HARNESS_EVENTS[k], HARNESS, detail if isinstance(detail, str) else
                                 json.dumps(detail)))
        elif k == "runner_start":
            starts += 1
            if starts > 1:
                ops.append({"t": t, "what": "runner started again by hand mid-run"})
        elif k == "runner_restart":
            ops.append({"t": t, "what": "runner restarted itself on changed code: " + ", ".join(e.get("files") or [])})
        elif k == "runner_stop" and e.get("why") == "stopped":
            ops.append({"t": t, "what": "runner stopped by the operator (play.py stop / signal)"})
    return fails, ops


def history_failures(sessions: list[dict]) -> list[dict]:
    """From runner.json history entries of the run's play sessions."""
    out = []
    for h in sessions:
        sid, t = h.get("session"), h.get("ended") or h.get("started")
        dur = (h.get("ended") or 0) - (h.get("started") or 0)
        if h.get("reason") == "killed":
            out.append(failure(t, "claude_killed", UNKNOWN, f"{sid}: claude ended by a signal from outside the runner"))
        elif h.get("reason") == "exited" and 0 <= dur < 60:
            out.append(failure(t, "claude_quick_exit", HARNESS, f"{sid}: claude exited after {dur:.0f} s "
                                                               f"(exit {h.get('exit')}: auth, config or usage)"))
        if h.get("early_exit_min") is not None:
            out.append(failure(t, "early_exit", AGENT, f"{sid}: ended the session with {h['early_exit_min']} min left"))
        if h.get("started_at_game_over"):
            out.append(failure(h.get("started"), "party_wipe", AGENT, f"{sid} started at the Game Over screen"))
    return out


def progress_failures(paths: list[Path], since: float) -> list[dict]:
    """Stalls: the progress poller's `stuck` records (no story progress for a while although the agent acted)."""
    out = []
    for p in paths:
        for r in _read_jsonl(p):
            if r.get("type") == "stuck" and (r.get("t") or 0) >= since:
                out.append(failure(r.get("t"), "stall", AGENT, f"{p.name.split('.')[0]}: no story progress for "
                                   f"{r.get('idle_min')} min ({r.get('actions')} tool calls meanwhile)"))
    return out


HAND_INPUT = ("click", "press_key", "ui_action")   # raw UI input tools
MOVE_TOOLS = ("move", "move_to", "walk_toward", "travel_to")   # "FAILED: beyond remaining movement" is routine


def stream_failures(stream: Path) -> list[dict]:
    """One play stream (Sxxx.jsonl / Sxxx-rN.jsonl): MCP server not connected, claude API/usage errors, tool
    exceptions and timeouts, fairness-guard blocks, fairness leaks, Game Over. Same-kind failures of one tool and
    error are grouped (n)."""
    sid = stream.name[:-len(".jsonl")]
    try:
        t_end = stream.stat().st_mtime
    except OSError:
        return []
    out = []
    for ev in _read_jsonl(stream):
        typ = ev.get("type")
        if typ == "system" and ev.get("subtype") == "init":
            for s in ev.get("mcp_servers") or []:
                if isinstance(s, dict) and s.get("name") == "bg3" and s.get("status") != "connected":
                    out.append(failure(None, "mcp_failed", HARNESS, f"{sid}: the bg3 MCP server is {s.get('status')}"))
        elif typ == "result" and ev.get("is_error") and API_ERROR_RE.search(str(ev.get("result") or "")):
            out.append(failure(t_end, "claude_api", HARNESS, f"{sid}: {ev.get('result')}"))
        elif typ == "assistant":
            content = (ev.get("message") or {}).get("content")
            for b in content if isinstance(content, list) else []:
                txt = b.get("text", "") if isinstance(b, dict) and b.get("type") == "text" else ""
                if txt.lstrip().startswith(("API Error", "Claude AI usage limit")):
                    out.append(failure(t_end, "claude_api", HARNESS, f"{sid}: {txt.strip()}"))
    calls, _ = triage.load_calls(stream, Path(str(stream)[:-len(".jsonl")] + ".turns.jsonl"))
    groups: dict[tuple[str, str, str], list[dict]] = {}
    wipe = None
    for c in calls:
        name, text = c["name"], c.get("text") or ""
        if name in fairness.NOT_GAME:
            if GUARD_BLOCK in text:
                groups.setdefault(("fairness_block", name, triage.error_key(text)), []).append(c)
            continue
        if wipe is None and GAME_OVER_RE.search(text):
            wipe = c
        for step, msg in c["fails"]:
            if TOOL_EXCEPTION_RE.search(msg):
                kind = "tool_timeout" if TIMEOUT_RE.search(msg) else "tool_exception"
            elif TIMEOUT_RE.search(msg.split(":", 1)[0]) or msg.startswith("TIMEOUT"):
                kind = "tool_timeout"
            elif step == name and msg.startswith("FAILED") and name not in MOVE_TOOLS:
                # the tool's own result failed (not a step inside a macro, not a walk cut short): usually the
                # agent's call, sometimes our gap (naut S001: create_character FAILED twice on the name box);
                # listed as unknown, never breaks "clean"
                kind = "tool_failed"
            else:
                continue   # a macro step, a partial walk, NOT READY / REFUSED / a rejected call: the game said no
            groups.setdefault((kind, step, triage.error_key(msg)), []).append(c)
        if name in HAND_INPUT:
            # raw clicks and keys: the agent working around a tool (the definition of done wants 0 hand clicks)
            groups.setdefault(("hand_input", name, ""), []).append(c)
    for (kind, tool, key), cs in groups.items():
        cls = AGENT if kind == "fairness_block" else UNKNOWN if kind in ("tool_failed", "hand_input") else HARNESS
        out.append(failure(cs[0].get("t") or t_end, kind, cls, f"{sid}: {tool}: {key}", len(cs)))
    if wipe is not None:
        out.append(failure(wipe.get("t") or t_end, "party_wipe", AGENT, f"{sid}: Game Over ({wipe['name']})"))
    for rule, hits in fairness.audit_calls(calls).items():
        if fairness.RULES[rule][0] == fairness.LEAK:
            out.append(failure(hits[0][0].get("t") or t_end, "fairness_leak", HARNESS,
                               f"{sid}: {rule}: {hits[0][1]}", len(hits)))
    return out


def reload_failures(actions: list[dict]) -> list[dict]:
    """Every load_game the agent called (runtime/actions.jsonl): a reload it chose."""
    out = []
    for a in actions:
        if a.get("tool") == "load_game":
            args = a.get("args") or {}
            out.append(failure(a.get("t"), "reload", AGENT, f"load_game {args.get('save') or args.get('label') or ''}"
                               .strip()))
    return out


def outcome_failure(outcome: str, why: str, t: float, others: list[dict], cap: str | None = None) -> list[dict]:
    """The run's own ending as a failure: a timeout or a cost/token cap is the agent's (unknown when harness failures
    hit the run), a give-up and a runner error are ours."""
    if outcome in ("timeout", "over_budget"):
        cls = UNKNOWN if any(f["class"] == HARNESS for f in others) else AGENT
        kind = "timeout" if outcome == "timeout" else f"{cap or 'cost'}_cap"
        return [failure(t, kind, cls, why or ("the run's time ran out before the milestone" if outcome == "timeout"
                                              else f"the run's {cap or 'cost'} cap was used up before the milestone"))]
    if outcome == "gave_up":
        return [failure(t, "gave_up", HARNESS, why)]
    if outcome == "runner_error":
        return [failure(t, "runner_error", HARNESS, why)]
    return []


def classify_run(state_dir: Path, run_started: float, now: float, sessions: list[dict], events: list[dict],
                 actions: list[dict], outcome: str, why: str, cap: str | None = None) -> list[dict]:
    """Every failure of one run, in time order (failures without a time first)."""
    fails, _ = event_failures(events)
    fails += history_failures(sessions)
    files = []
    try:
        files = sorted(state_dir.iterdir()) if state_dir.is_dir() else []
    except OSError:
        pass
    recent = []
    for f in files:
        try:
            if f.stat().st_mtime >= run_started:
                recent.append(f)
        except OSError:
            continue
    fails += progress_failures([f for f in recent if f.name.endswith(".progress.jsonl")], run_started)
    for f in recent:
        if PLAY_STREAM_RE.match(f.name):
            fails += stream_failures(f)
    fails += reload_failures(actions)
    fails += outcome_failure(outcome, why, now, fails, cap)
    return sorted(fails, key=lambda f: (f["t"] is not None, f["t"] or 0))


def is_clean(outcome: str, failures: list[dict], operator: list[dict]) -> bool:
    """The milestone reached with no harness failure and no operator action: a run that scores the model alone."""
    return outcome == "reached" and not operator and not any(f.get("class") == HARNESS for f in failures)


# ======================================================================== version pins

# Repo paths whose working-tree state a run plays with (runtime/, playthroughs/ and docs are not code).
CODE_PATHS = ("runner", "bridge", "mod", "play", "tools", "patches", "memory-template")
# The pins compared between the runs of a series (dotted paths into version_pins()).
DRIFT_KEYS = ("game_build", "bg3le.sha256", "bg3le.source", "mod.sha1", "repo.commit", "repo.dirty", "repo.diff")


def _git(cwd: Path, *args: str) -> str | None:
    try:
        p = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    return p.stdout if p.returncode == 0 else None


def buildinfo(text: str) -> dict:
    """build/bg3le/dist/BUILDINFO (tools/build_bg3le.sh): the upstream commit, the binary's sha256, the patches."""
    out: dict = {"patches": []}
    for ln in text.splitlines():
        k, _, v = ln.partition(":")
        k, v = k.strip(), v.strip()
        if k == "bg3le commit":
            out["commit"] = v
        elif k == "sha256":
            out["sha256"] = v
        elif k == "patch":
            out["patches"].append(v)
    return out


def mod_version(meta_lsx: str) -> str | None:
    """The mod's meta.lsx Version64 as major.minor.revision.build (Larian's bit layout), or None."""
    m = re.search(r'id="Version64"[^>]*value="(\d+)"', meta_lsx)
    if not m:
        return None
    v = int(m.group(1))
    return f"{v >> 55}.{(v >> 47) & 0xFF}.{(v >> 31) & 0xFFFF}.{v & 0x7FFFFFFF}"


def tree_digest(hashes: dict[str, str]) -> str:
    """One sha1 over a {relative path: sha1} tree (play.py mod_tree_hashes)."""
    return hashlib.sha1("".join(f"{k}\0{v}\n" for k, v in sorted(hashes.items())).encode()).hexdigest()


def repo_pin(repo: Path, git=_git) -> dict:
    """The repo commit and whether the code paths differ from it (tracked edits or untracked files); diff: a sha1 of
    `git diff HEAD` plus the untracked names over CODE_PATHS, so two dirty trees with different edits differ."""
    head = (git(repo, "rev-parse", "HEAD") or "").strip() or None
    status = git(repo, "status", "--porcelain", "--", *CODE_PATHS)
    diff = git(repo, "diff", "HEAD", "--", *CODE_PATHS)
    dirty = bool(status and status.strip()) if status is not None else None
    untracked = "".join(ln for ln in (status or "").splitlines(True) if ln.startswith("??"))
    return {"commit": head, "dirty": dirty,
            "diff": hashlib.sha1(((diff or "") + untracked).encode()).hexdigest()[:16] if dirty else None}


def bg3le_pin(dist: Path, source: Path, git=_git) -> dict:
    """bg3le as the game loads it: the built binary (BUILDINFO's upstream commit and sha256, the patches built in)
    and the source tree's `git describe --dirty` (third_party/bg3le carries local changes past its pinned tag:
    local_changes)."""
    try:
        info = buildinfo((dist / "BUILDINFO").read_text(errors="replace"))
    except OSError:
        info = {"patches": []}
    sha = info.get("sha256")
    if not sha:
        try:
            sha = hashlib.sha256((dist / "libbg3le.so").read_bytes()).hexdigest()
        except OSError:
            sha = None
    desc = (git(source, "describe", "--tags", "--dirty", "--always") or "").strip() or None
    return {"source": desc, "local_changes": desc.endswith("-dirty") if desc else None, "commit": info.get("commit"),
            "sha256": sha, "patches": len(info["patches"])}


def version_pins(repo: Path, game_build: str, dist: Path, mod_meta: Path, mod_hashes: dict[str, str],
                 git=_git) -> dict:
    """What one run plays with: the BG3 Steam buildid, bg3le, the mod (version + tree sha1) and the repo."""
    try:
        ver = mod_version(mod_meta.read_text(errors="replace"))
    except OSError:
        ver = None
    return {"game_build": game_build or None, "bg3le": bg3le_pin(dist, repo / "third_party" / "bg3le", git),
            "mod": {"version": ver, "sha1": tree_digest(mod_hashes) if mod_hashes else None},
            "repo": repo_pin(repo, git)}


def _dig(d: dict | None, dotted: str):
    for k in dotted.split("."):
        d = d.get(k) if isinstance(d, dict) else None
    return d


def pin_drift(first: dict | None, now: dict) -> list[str]:
    """`key: first → now` for every DRIFT_KEYS pin that differs from the series' first run (none: no first pins)."""
    if not first:
        return []
    return [f"{k}: {_dig(first, k)} → {_dig(now, k)}" for k in DRIFT_KEYS if _dig(first, k) != _dig(now, k)]


# ======================================================================== caps (time, cost, tokens)

CAP_KEYS = ("run_hours", "max_cost", "max_tokens")   # --run-hours, --max-cost USD, --max-tokens N


def series_caps(frozen: dict | None, cli: dict, allow_change: bool) -> tuple[dict, str | None, list[str]]:
    """(caps for this run, error, changes): series.json's caps rule a series; a CLI cap left out takes the frozen
    one, a different CLI value is an error unless allow_change (then it is used and listed). No frozen caps (the
    first run, or a series from before caps): the CLI's."""
    if frozen is None:
        return {k: cli.get(k) for k in CAP_KEYS}, None, []
    caps, changes = {}, []
    for k in CAP_KEYS:
        want, have = cli.get(k), frozen.get(k)
        if want is None or want == have:
            caps[k] = have
        else:
            changes.append(f"{k}: {have} → {want}")
            caps[k] = want
    if changes and not allow_change:
        return caps, ("the series' caps are fixed in series.json (" + "; ".join(changes) + "): drop the flag, or "
                      "pass --allow-cap-change to record the change"), changes
    return caps, None, changes


TOKEN_FIELDS = (("input", "input_tokens"), ("output", "output_tokens"), ("cache_read", "cache_read_input_tokens"),
                ("cache_write", "cache_creation_input_tokens"))


def usage_tokens(usage: dict | None) -> dict:
    u = usage if isinstance(usage, dict) else {}
    return {k: int(u.get(src) or 0) for k, src in TOKEN_FIELDS}


def token_total(tokens: dict | None) -> int:
    """What --max-tokens counts: every token claude processed (input, output, cache reads and writes)."""
    return sum(int(v or 0) for v in (tokens or {}).values())


def claude_totals(state_dir: Path, since: float) -> dict:
    """Cost, turns and tokens of every claude stream written since `since` (play, resumes, -fix, -keeper): their
    result events, i.e. the finished claude processes."""
    cost, turns, tokens = 0.0, 0, {k: 0 for k, _ in TOKEN_FIELDS}
    try:
        files = sorted(state_dir.iterdir()) if state_dir.is_dir() else []
    except OSError:
        files = []
    for f in files:
        try:
            if not CLAUDE_STREAM_RE.match(f.name) or f.stat().st_mtime < since:
                continue
            lines = f.read_text(errors="replace").splitlines()
        except OSError:
            continue
        for ln in lines:
            if '"type": "result"' not in ln and '"type":"result"' not in ln:
                continue
            try:
                ev = json.loads(ln)
            except ValueError:
                continue
            cost += float(ev.get("total_cost_usd") or 0)
            turns += int(ev.get("num_turns") or 0)
            for k, v in usage_tokens(ev.get("usage")).items():
                tokens[k] += v
    return {"cost_usd": cost, "turns": turns, "tokens": tokens}


class UsageMeter:
    """A run's cost and tokens while a claude process streams: `base` (the run's finished processes, claude_totals)
    plus this stream's assistant messages (their usage, per message id) until its result event gives the exact
    totals. Claude reports cost only in result events, so the live cost is the live tokens at the run's cost per
    token so far (none before the run's first result: a cost cap then trips at the end of the first process)."""

    def __init__(self, base_cost: float = 0.0, base_tokens: int = 0):
        self.done_cost, self.done_tokens = float(base_cost), int(base_tokens)
        self.msgs: dict = {}
        self.live = 0

    def on_event(self, ev: dict) -> None:
        typ = ev.get("type")
        if typ == "assistant":
            msg = ev.get("message") or {}
            if not isinstance(msg.get("usage"), dict):
                return
            mid = msg.get("id") or f"#{len(self.msgs)}"
            n = token_total(usage_tokens(msg["usage"]))
            self.live += n - self.msgs.get(mid, 0)
            self.msgs[mid] = n
        elif typ == "result":
            self.done_cost += float(ev.get("total_cost_usd") or 0)
            self.done_tokens += token_total(usage_tokens(ev.get("usage")))
            self.msgs, self.live = {}, 0

    @property
    def tokens(self) -> int:
        return self.done_tokens + self.live

    @property
    def cost(self) -> float:
        rate = self.done_cost / self.done_tokens if self.done_tokens else 0.0
        return self.done_cost + self.live * rate


def cap_exceeded(caps: dict | None, cost: float, tokens: int) -> str | None:
    """'cost' or 'tokens' when the run reached --max-cost / --max-tokens, else None (the time cap is play.py's)."""
    caps = caps or {}
    if caps.get("max_cost") is not None and cost >= caps["max_cost"]:
        return "cost"
    if caps.get("max_tokens") is not None and tokens >= caps["max_tokens"]:
        return "tokens"
    return None


# ======================================================================== run metrics (pure functions over files)

def _recent(state_dir: Path, since: float, pattern: re.Pattern) -> list[Path]:
    out = []
    try:
        files = sorted(state_dir.iterdir()) if state_dir.is_dir() else []
    except OSError:
        return out
    for f in files:
        try:
            if pattern.match(f.name) and f.stat().st_mtime >= since:
                out.append(f)
        except OSError:
            continue
    return out


def time_to_milestone(started: float | None, reached_at: float | None) -> float | None:
    """Wall seconds from the run's start to the milestone (None: not reached)."""
    if started is None or reached_at is None:
        return None
    return round(reached_at - started, 1)


FIGHTS_RE = re.compile(r"^S\d{3,}\.fights\.jsonl$")


def death_counts(state_dir: Path, since: float, failures: list[dict]) -> dict:
    """Party downs and deaths in the run's fights (Sxxx.fights.jsonl, the MCP server's fight debriefs: party[].downed
    and .died per fight) and Game Overs (the run's party_wipe failures). Downs or deaths outside a fight the
    debrief saw (a fall, a trap out of combat) are not observable in the logs and not counted."""
    fights = downs = deaths = 0
    for f in _recent(state_dir, since, FIGHTS_RE):
        for r in _read_jsonl(f):
            fights += 1
            for m in r.get("party") or []:
                if isinstance(m, dict):
                    downs += int(m.get("downed") or 0)
                    deaths += 1 if m.get("died") else 0
    wipes = sum(int(x.get("n") or 1) for x in failures or [] if x.get("kind") == "party_wipe")
    return {"fights": fights, "downs": downs, "deaths": deaths, "wipes": wipes}


def fairness_flags(streams: list[Path]) -> dict:
    """runner/fairness.py over the run's play streams: hits per rule (LEAK and CHECK), plus the sandbox/fairness
    guard's blocks of the agent's own file calls."""
    kinds: Counter = Counter()
    leak = check = blocks = 0
    for s in streams:
        calls, _ = triage.load_calls(s, Path(str(s)[:-len(".jsonl")] + ".turns.jsonl"))
        blocks += sum(1 for c in calls if c["name"] in fairness.NOT_GAME and GUARD_BLOCK in (c.get("text") or ""))
        for rule, hits in fairness.audit_calls(calls).items():
            kinds[rule] += len(hits)
            if fairness.RULES[rule][0] == fairness.LEAK:
                leak += len(hits)
            else:
                check += len(hits)
    return {"count": leak + check, "leak": leak, "check": check, "blocks": blocks, "kinds": dict(kinds)}


LATENCY_RE = re.compile(r"^S\d{3,}(?:-[rc]\d+)?\.latency\.json$")   # play sessions only (not -fix/-keeper)


def _weighted_median(pairs: list[tuple[float, int]]) -> float | None:
    pairs = sorted((v, n) for v, n in pairs if v is not None and n)
    total = sum(n for _, n in pairs)
    acc = 0
    for v, n in pairs:
        acc += n
        if acc * 2 >= total:
            return v
    return None


def latency_summary(state_dir: Path, since: float) -> dict | None:
    """The run's live feel from its play sessions' Sxxx.latency.json (bg3ai.metrics): idle gap median and p90 (s),
    gaps over 10 s, the read tools' round-trip median (ms). Over several sessions each value is the median of the
    per-session values weighted by their sample counts (exact for a one-session run); None: no latency files."""
    idle_med, idle_p90, rt, gaps, n = [], [], [], 0, 0
    for f in _recent(state_dir, since, LATENCY_RE):
        try:
            d = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        n += 1
        a = ((d.get("idle_s") or {}).get("all")) or {}
        idle_med.append((a.get("median"), a.get("n") or 0))
        idle_p90.append((a.get("p90"), a.get("n") or 0))
        r = ((d.get("tool_rt_ms") or {}).get("read")) or {}
        rt.append((r.get("median"), r.get("n") or 0))
        gaps += int((d.get("long_gaps") or {}).get("total") or 0)
    if not n:
        return None
    return {"sessions": n, "idle_median_s": _weighted_median(idle_med), "idle_p90_s": _weighted_median(idle_p90),
            "gaps_over_10s": gaps, "read_rt_median_ms": _weighted_median(rt)}


def story_gained(state_dir: Path, since: float) -> dict:
    """Story progress points the run's sessions gained (progress_history.jsonl, bg3ai.progress: one line per
    session) and their events by kind."""
    points = events = sessions = 0
    by_kind: Counter = Counter()
    for r in _read_jsonl(state_dir / "progress_history.jsonl"):
        if (r.get("t") or 0) < since:
            continue
        sessions += 1
        points += int(r.get("points") or 0)
        events += int(r.get("events") or 0)
        by_kind.update({k: int(v or 0) for k, v in (r.get("by_kind") or {}).items()})
    return {"points": points, "events": events, "sessions": sessions, "by_kind": dict(by_kind)}


def run_metrics(state_dir: Path, run_started: float, reached_at: float | None, failures: list[dict]) -> dict:
    """The benchmark fields of a run record beyond the failures (each a pure function above)."""
    streams = _recent(state_dir, run_started, PLAY_STREAM_RE)
    return {"time_to_milestone_s": time_to_milestone(run_started, reached_at),
            "deaths": death_counts(state_dir, run_started, failures),
            "fairness_flags": fairness_flags(streams),
            "latency": latency_summary(state_dir, run_started),
            "story": story_gained(state_dir, run_started)}


# ======================================================================== the table

def class_counts(failures: list[dict]) -> Counter:
    c = Counter()
    for f in failures or []:
        c[f.get("class", UNKNOWN)] += int(f.get("n") or 1)
    return c


def clean_streak(records: list[dict]) -> int:
    """Clean runs in a row at the end of the series (the launch bar: 3)."""
    n = 0
    for r in reversed(records):
        if not r.get("clean"):
            break
        n += 1
    return n


def best_streak(records: list[dict]) -> int:
    best = cur = 0
    for r in records:
        cur = cur + 1 if r.get("clean") else 0
        best = max(best, cur)
    return best


def runs_table(records: list[dict]) -> str:
    """Plain-text table of a series' runs (play.py runs --playthrough <base>)."""
    head = ("run", "playthrough", "outcome", "clean", "wall min", "to M min", "sessions", "harness", "agent",
            "unknown", "cost $", "tokens M", "idle med s", "top failures")
    rows = []
    for r in records:
        c = class_counts(r.get("failures"))
        kinds = Counter()
        for f in r.get("failures") or []:
            kinds[f"{f.get('kind')}({f.get('class', '?')[0]})"] += int(f.get("n") or 1)
        top = ", ".join(f"{k}×{n}" if n > 1 else k for k, n in kinds.most_common(4))
        outcome = r.get("outcome") or ("reached" if r.get("milestone") else "?")
        ttm, idle = record_ttm(r), record_idle(r)
        outcome += f" ({r['cap']})" if r.get("cap") and outcome != "timeout" else ""
        rows.append((str(r.get("run") or 1), str(r.get("playthrough") or ""), outcome,
                     "yes" if r.get("clean") else "no", f"{r.get('wall_min', 0):g}",
                     "-" if ttm is None else f"{ttm / 60:.1f}", str(r.get("sessions", "")),
                     str(c[HARNESS]), str(c[AGENT]), str(c[UNKNOWN]), f"{r.get('cost_usd', 0):.2f}",
                     f"{token_total(r.get('tokens')) / 1e6:.2f}", "-" if idle is None else f"{idle:g}", top))
    widths = [max(len(x) for x in col) for col in zip(head, *rows)] if rows else [len(h) for h in head]
    lines = ["  ".join(v.ljust(w) for v, w in zip(row, widths)).rstrip() for row in [head, *rows]]
    lines.insert(1, "  ".join("-" * w for w in widths))
    n = clean_streak(records)
    clean = sum(1 for r in records if r.get("clean"))
    lines += ["", f"{len(records)} run(s), {clean} clean; {n} clean in a row (latest), best streak "
                  f"{best_streak(records)}"]
    return "\n".join(lines)


# ======================================================================== the series summary

def record_ttm(r: dict) -> float | None:
    """time_to_milestone_s of a record (derived from reached_at - started for records written before the field)."""
    if "time_to_milestone_s" in r:
        return r["time_to_milestone_s"]
    return time_to_milestone(r.get("started"), r.get("reached_at"))


def record_idle(r: dict) -> float | None:
    return ((r.get("latency") or {}).get("idle_median_s"))


def stats(values: list) -> dict | None:
    """{n, min, max, mean, stddev (sample; 0 for one value)} of the non-None values, or None."""
    v = [float(x) for x in values if x is not None]
    if not v:
        return None
    return {"n": len(v), "min": min(v), "max": max(v), "mean": statistics.fmean(v),
            "stddev": statistics.stdev(v) if len(v) > 1 else 0.0}


SUMMARY_METRICS = (   # (label, value of a record, format)
    ("wall min", lambda r: r.get("wall_min"), "{:.1f}"),
    ("to milestone min", lambda r: None if record_ttm(r) is None else record_ttm(r) / 60, "{:.1f}"),
    ("cost $", lambda r: r.get("cost_usd"), "{:.2f}"),
    ("tokens M", lambda r: token_total(r.get("tokens")) / 1e6 if r.get("tokens") else None, "{:.2f}"),
    ("idle median s", record_idle, "{:.2f}"),
)


def series_summary(records: list[dict]) -> str:
    """The series in numbers (play.py runs): n, reached, and min/max/mean/stddev of wall time, time to the
    milestone (reached runs only), cost, tokens and the idle-gap median; the pins and caps when they vary."""
    reached = sum(1 for r in records if (r.get("outcome") or ("reached" if r.get("milestone") else "")) == "reached")
    lines = [f"series: n={len(records)}, reached {reached}/{len(records)}"]
    head = ("metric", "n", "min", "max", "mean", "stddev")
    rows = []
    for label, get, fmt in SUMMARY_METRICS:
        st = stats([get(r) for r in records])
        rows.append((label, "0", "-", "-", "-", "-") if st is None else
                    (label, str(st["n"]), *(fmt.format(st[k]) for k in ("min", "max", "mean", "stddev"))))
    widths = [max(len(x) for x in col) for col in zip(head, *rows)]
    lines += ["  ".join(v.ljust(w) for v, w in zip(row, widths)).rstrip() for row in [head, *rows]]
    lines.insert(2, "  ".join("-" * w for w in widths))
    drift = [f"run {r.get('run') or 1}: " + "; ".join(r["version_drift"]) for r in records if r.get("version_drift")]
    if drift:
        lines += ["version drift (--allow-version-drift): " + " | ".join(drift)]
    caps = {json.dumps(r.get("caps"), sort_keys=True) for r in records if r.get("caps")}
    if len(caps) > 1:
        lines += ["caps differ between runs: " + " | ".join(sorted(caps))]
    return "\n".join(lines)


# ======================================================================== scenarios (scenarios/<name>.json)
# A scenario is a fixed start point for a series: a new game or a start save, the milestone it ends at, the caps,
# the character profile and the per-run resets. `play.py run --scenario NAME` freezes it into series.json.

SCENARIO_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,47}$")
SAVE_FOLDER_RE = re.compile(r"^(?P<owner>.+)-(?P<num>\d+)__(?P<label>.+)$")   # play.py's: <owner>-<id>__<label>
START_KINDS = ("new_game", "save")
SCENARIO_KEYS = {"name", "title", "about", "start", "until", "runs", "caps", "profile", "reset"}
NOTE_FIELDS = ("where", "objective", "party", "journal")
AGENT_LABEL_RE = re.compile(r"^S\d{3}-")   # the agent's save labels (play.py is_ai_label)
NOTE_MAX_CHARS = 1500
# The start note is the harness's, neutral and the same for every model: never a model's own play history.
NOTE_HISTORY_RE = re.compile(r"\bS\d{3}\b|claude-run|playthroughs/|runtime/|sessions/", re.I)
READY, NEEDS_SAVE, INVALID = "ready", "needs a start save", "invalid"
RESETS = {   # what every run of a series starts without (play.py reset_for_run / prepare_series_run); not tunable
    "new_game": {"saves": "after_run_start", "nav_state": "archive", "knowledge": "restore_agent_edits",
                 "memory": "template"},
    "save": {"saves": "after_run_start+start_save", "nav_state": "archive", "knowledge": "restore_agent_edits",
             "memory": "template+start_note"},
}


class ScenarioError(ValueError):
    pass


def save_owner_of(folder: str | None) -> str | None:
    m = SAVE_FOLDER_RE.match(folder or "")
    return m.group("owner") if m else None


def save_label_of(folder: str | None) -> str | None:
    m = SAVE_FOLDER_RE.match(folder or "")
    return m.group("label") if m else None


def _num_ok(v) -> bool:
    return v is None or (isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0)


def note_text(note: dict | None) -> str:
    """Every string of a start note, for the neutrality and length checks."""
    parts = []
    for v in (note or {}).values():
        parts += [str(x) for x in v] if isinstance(v, list) else [str(v)]
    return "\n".join(parts)


def validate_scenario(sc, name: str, milestone_ids, roleplays, difficulties) -> list[str]:
    """The problems of a scenario file's content (empty: valid). name is the file's stem; milestone_ids the ones
    run --until knows (bg3ai.milestones); roleplays/difficulties play.py's profile values."""
    if not isinstance(sc, dict):
        return ["not a JSON object"]
    errs = []
    if not SCENARIO_NAME_RE.match(name):
        errs.append(f"file name {name!r}: use a-z, 0-9, '_' or '-'")
    if sc.get("name") != name:
        errs.append(f"name {sc.get('name')!r} must be the file name {name!r}")
    if unknown := sorted(set(sc) - SCENARIO_KEYS):
        errs.append(f"unknown keys: {', '.join(unknown)}")
    if sc.get("until") not in milestone_ids:
        errs.append(f"until {sc.get('until')!r} is not a milestone (bridge/src/bg3ai/milestones.py)")
    runs_n = sc.get("runs", 3)
    if not isinstance(runs_n, int) or isinstance(runs_n, bool) or runs_n < 1:
        errs.append("runs must be an integer >= 1")
    caps = sc.get("caps")
    if not isinstance(caps, dict) or set(caps) - set(CAP_KEYS) or not all(_num_ok(v) for v in caps.values()):
        errs.append(f"caps must be an object with {', '.join(CAP_KEYS)} (positive numbers or null)")
    elif not caps.get("run_hours"):
        errs.append("caps.run_hours is required: every run must end")
    prof = sc.get("profile")
    c = prof.get("character") if isinstance(prof, dict) else None
    if not isinstance(c, dict) or not isinstance(c.get("brief"), str) or not c["brief"].strip():
        errs.append("profile.character.brief is required (phase 0's brief, or the persona of the save's character)")
        c = {}
    if c.get("roleplay", "strict") not in roleplays:
        errs.append(f"profile.character.roleplay must be one of {', '.join(roleplays)}")
    if isinstance(prof, dict) and prof.get("difficulty") is not None and prof["difficulty"] not in difficulties:
        errs.append(f"profile.difficulty must be one of {', '.join(difficulties)}")
    st = sc.get("start")
    kind = st.get("kind") if isinstance(st, dict) else None
    if kind not in START_KINDS:
        errs.append(f"start.kind must be one of {', '.join(START_KINDS)}")
    elif kind == "new_game":
        if set(st) - {"kind"}:
            errs.append("a new_game start takes no save or note")
    else:
        save, note = st.get("save"), st.get("note")
        if save is not None and save_owner_of(save) is None:
            errs.append(f"start.save {save!r} is not a save folder name (<owner>-<id>__<label>)")
        elif save is not None and AGENT_LABEL_RE.match(save_label_of(save)):
            # An Sxxx- label is a playthrough's own save: it names that playthrough's session in the start note, and
            # its owner's other playthrough (pick_save takes any Sxxx- save of the owner) could load it.
            errs.append(f"start.save {save!r} has an Sxxx- label: re-save it as SCN-{name} (scenarios/README.md)")
        if st.get("save_sha1") is not None and not isinstance(st["save_sha1"], str):
            errs.append("start.save_sha1 must be a string or null")
        if not c.get("name"):
            errs.append("a save start needs profile.character.name: the save fixes the character")
        elif save_owner_of(save) and save_owner_of(save) != c["name"]:
            errs.append(f"profile.character.name {c['name']!r} must match the start save's character "
                        f"{save_owner_of(save)!r}")
        if set(st) - {"kind", "save", "save_sha1", "note"}:
            errs.append(f"unknown start keys: {', '.join(sorted(set(st) - {'kind', 'save', 'save_sha1', 'note'}))}")
        if not isinstance(note, dict) or not all(isinstance(note.get(k), str) and note[k].strip()
                                                 for k in ("where", "objective")):
            errs.append("a save start needs start.note with where and objective (the harness's start note)")
        else:
            if set(note) - set(NOTE_FIELDS):
                errs.append(f"unknown start.note keys: {', '.join(sorted(set(note) - set(NOTE_FIELDS)))}")
            for k in ("party", "journal"):
                if note.get(k) is not None and not (isinstance(note[k], list) and
                                                    all(isinstance(x, str) for x in note[k])):
                    errs.append(f"start.note.{k} must be a list of strings")
            text = note_text(note)
            if len(text) > NOTE_MAX_CHARS:
                errs.append(f"start.note is {len(text)} chars > {NOTE_MAX_CHARS}: keep it short")
            if m := NOTE_HISTORY_RE.search(text):
                errs.append(f"start.note mentions {m.group(0)!r}: it must be neutral game facts, never a "
                            "playthrough's own history (sessions, memory)")
    if kind in RESETS and sc.get("reset") is not None and sc["reset"] != RESETS[kind]:
        errs.append(f"reset must be the standard one for a {kind} start (or left out): {json.dumps(RESETS[kind])}")
    return errs


def load_scenario(path: Path, milestone_ids, roleplays, difficulties) -> dict:
    """scenarios/<name>.json, validated and with its defaults filled (runs 3, every cap key, the reset);
    ScenarioError when it can't be read or is invalid."""
    try:
        sc = json.loads(path.read_text())
    except (OSError, ValueError) as e:
        raise ScenarioError(f"{path}: {e}") from None
    errs = validate_scenario(sc, path.stem, milestone_ids, roleplays, difficulties)
    if errs:
        raise ScenarioError(f"{path}: " + "; ".join(errs))
    out = dict(sc, runs=sc.get("runs", 3), caps={k: sc["caps"].get(k) for k in CAP_KEYS},
               reset=RESETS[sc["start"]["kind"]])
    out["profile"] = dict(sc["profile"], character=dict(sc["profile"]["character"],
                                                        roleplay=sc["profile"]["character"].get("roleplay", "strict")))
    return out


def scenario_digest(sc: dict) -> str:
    """A short hash of a scenario's content: a series refuses a scenario file edited after it started."""
    return hashlib.sha1(json.dumps(sc, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]


def save_digest(save_dir: Path, folder: str) -> str | None:
    """sha1 of a save's <folder>/<folder>.lsv (read only); None when it isn't there."""
    h = hashlib.sha1()
    try:
        with open(save_dir / folder / f"{folder}.lsv", "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
    except OSError:
        return None
    return h.hexdigest()


def scenario_status(sc: dict, folders: set[str], sha1_of=None) -> tuple[str, str]:
    """(READY | NEEDS_SAVE, detail) of a loaded scenario: a save start is ready when its save is set and present
    (and, with start.save_sha1, unchanged: sha1_of(folder) is save_digest)."""
    st = sc["start"]
    if st["kind"] == "new_game":
        return READY, "new game"
    save = st.get("save")
    if not save:
        return NEEDS_SAVE, "start.save is a placeholder (scenarios/README.md: how to make it)"
    if save not in folders:
        return NEEDS_SAVE, f"start save {save} is not in the save folder"
    if st.get("save_sha1") and sha1_of is not None:
        got = sha1_of(save)
        if got != st["save_sha1"]:
            return NEEDS_SAVE, f"start save {save} changed (sha1 {got} != the scenario's {st['save_sha1']})"
    return READY, f"save {save}"


def start_note(sc: dict) -> str:
    """The memory's HANDOFF.md for a run that starts from a save: the scenario's fixed start note (written by the
    operator from the game's screens when making the save) rendered the same way for every run and every model.
    Pure: nothing from the game or another playthrough goes in."""
    note = sc["start"]["note"]
    label = save_label_of(sc["start"].get("save")) or "(the scenario's start save)"
    lines = ["# Handoff S000 → S001", "",
             f"Save to load: \"{label}\" — the scenario's start save (the runner loads it)", "",
             f"<!-- Written by the runner from scenarios/{sc['name']}.json: a neutral start note, the same for every "
             "run. This run starts mid-game from a fixed save; nothing else from before it is in your memory. -->",
             "", "## Doing right now", f"Scenario start: {sc.get('title') or sc['name']}.",
             f"Where: {note['where'].strip()}"]
    if note.get("party"):
        lines.append("Party: " + ", ".join(note["party"]))
    lines += ["", "## Next steps", f"1. {note['objective'].strip()}",
              "2. Read the character and party back (get_status, get_party) and write CHARACTER.md and party-build.md "
              "from what the game shows.", "", "## Journal at the start save"]
    lines += [f"- {q}" for q in note.get("journal") or []] or ["- (empty)"]
    lines += ["", "## Watch out", "", "## Unresolved", "", "## Discrepancies found this session", ""]
    return "\n".join(lines)


def scenario_cli_conflicts(sc: dict, cli: dict) -> list[str]:
    """CLI flags that contradict the scenario (run --scenario refuses them; caps go through series_caps): a flag
    left out takes the scenario's value, the same value is fine. cli: until, character_brief, roleplay, difficulty,
    save_owner (None = not given)."""
    c = sc["profile"]["character"]
    want = {"until": ("--until", sc["until"]), "character_brief": ("--character-brief", c["brief"]),
            "roleplay": ("--roleplay", c.get("roleplay", "strict")),
            "difficulty": ("--difficulty", sc["profile"].get("difficulty")),
            "save_owner": ("--save-owner", c.get("name"))}
    out = []
    for k, (flag, have) in want.items():
        v = cli.get(k)
        if v is not None and (v.strip() if isinstance(v, str) else v) != have:
            out.append(f"{flag} {v!r} contradicts scenario {sc['name']} ({have!r})")
    return out


def scenario_series_check(meta: dict, sc: dict, base: str, save_sha1: str | None) -> str | None:
    """Before a scenario series run: a refusal (str) when runtime/sessions/<base>/series.json belongs to another
    scenario or none, the scenario file changed since the series froze it, or its start save changed; else None."""
    if not meta:
        return None
    frozen = meta.get("scenario")
    if not frozen:
        return f"series {base} was started without a scenario: pick another --playthrough for scenario {sc['name']}"
    if frozen.get("name") != sc["name"]:
        return f"series {base} plays scenario {frozen.get('name')}, not {sc['name']}"
    if frozen.get("digest") != scenario_digest(sc):
        return (f"scenarios/{sc['name']}.json changed since series {base} started (digest {frozen.get('digest')} → "
                f"{scenario_digest(sc)}): restore it, or start a new series with another --playthrough")
    if frozen.get("start_save_sha1") and frozen["start_save_sha1"] != save_sha1:
        return (f"the start save {sc['start'].get('save')} changed since series {base} started (sha1 "
                f"{frozen['start_save_sha1']} → {save_sha1})")
    return None


def scenario_freeze(sc: dict, save_sha1: str | None) -> dict:
    """series.json's "scenario": what every run of the series is held to."""
    return {"name": sc["name"], "digest": scenario_digest(sc), "start_save_sha1": save_sha1, "spec": sc}

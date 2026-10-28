"""Run records' core (moved from the runner, Phase D): failure tagging, the usage
meter and caps, and the run metrics over a run's files. The benchmark runner (play.py) and playthroughs both write
their run records with it; the series, pins, resets, runs table and scenario files are bg3bench's.

Every function is pure over files the runner and the bridge already write (the state dir and the run's window are
passed in), so it is unit tested without a game.

Failure classes (every failure in a run record is one of them):
    harness   ours: game crash/hang/OOM, launch/load/setup failures, a stale bg3le, the MCP server not connected,
              claude API/usage errors and claude exiting at once, tool exceptions and timeouts, a tool result that
              leaked something a player can't see (fairness LEAK), runner exceptions and runner give-ups
    agent     the model's: stalls with no story progress, party wipes (Game Over), reloads it chose, early exits,
              running out of the run's time, sandbox/fairness-guard blocks of its own file calls
    unknown   can't tell from the logs: claude silent for minutes (a slow model turn or a stalled API), claude
              killed by a signal from outside the runner; also listed here, never voiding a run: a tool's own
              FAILED, raw clicks/keys, and a background action's TIMEOUT echoed in another call's EVENTS
              (bg_timeout: the call itself returned, so the agent's play wasn't touched)
Not failures: the game refusing a call (FAILED inside a macro, NOT READY, REFUSED) and an argument-validation
rejection of the agent's own call (ARG_VALIDATION_RE).
Operator actions (a manual restart, a stop, a runner self-restart on changed code, --take-over) are listed apart:
a run with any of them is not clean.
"""

from __future__ import annotations

import contextlib
import itertools
import json
import re
from collections import Counter
from pathlib import Path

from . import clients, events, fairness, model_time, triage

HARNESS, AGENT, UNKNOWN = "harness", "agent", "unknown"
CLASSES = (HARNESS, AGENT, UNKNOWN)
OUTCOMES = ("reached", "timeout", "over_budget", "gave_up", "runner_error")  # timeout: the time cap; over_budget:
CAPS = ("time", "cost", "tokens")  # the cost or token cap (record "cap")

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
    # the game's settings contradict the scenario, or the readout can't confirm one it fixes: not the run it claims
    "settings_mismatch": "settings_mismatch",
    "settings_unverified": "settings_unverified",
}
FAULT_KINDS = {"crash": ("game_crash", HARNESS), "hang": ("game_hang", HARNESS), "agent_hang": ("agent_hang", UNKNOWN)}

PLAY_STREAM_RE = re.compile(r"^S\d{3,}(?:-[rc]\d+)?\.jsonl$")  # play sessions and their resumes (crash -rN, stop
# check -cN; not -fix/-keeper)
CLAUDE_STREAM_RE = re.compile(r"^S\d{3,}(?:-[\w-]+)?\.jsonl$")  # every claude stream, incl. -fix and -keeper
API_ERROR_RE = re.compile(
    r"API Error|usage limit|rate.?limit|overloaded|credit balance|invalid api key|"
    r"authentication|\b(?:429|500|502|503|529)\b",
    re.I,
)
TOOL_EXCEPTION_RE = re.compile(
    r"Error executing tool|Traceback \(most recent call last\)|Internal error|MCP error", re.I
)
# the MCP server (FastMCP) refusing the agent's arguments before the tool runs: the agent's malformed call, like
# claude's own <tool_use_error>InputValidationError and pi's 'Validation failed for tool' (bench B4 Haiku x
# fight_act1_windmill: end_combat_report called with {})
ARG_VALIDATION_RE = re.compile(r"Error executing tool (\w+): \d+ validation errors? for \1Arguments\b")
TIMEOUT_RE = re.compile(r"\bTIMEOUT\b|timed out", re.I)
GUARD_BLOCK = "Blocked by the sandbox/fairness rule"
GAME_OVER_RE = re.compile(r"\bgame[_ ]over\b", re.I)


def failure(t, kind: str, cls: str, detail: str, n: int = 1) -> dict:
    f = {
        "t": round(t, 1) if isinstance(t, (int, float)) else None,
        "kind": kind,
        "class": cls,
        "detail": str(detail)[:300],
    }
    if n > 1:
        f["n"] = n
    return f


# ======================================================================== failure tagging


def read_jsonl(path: Path) -> list[dict]:
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
            fails.append(
                failure(t, HARNESS_EVENTS[k], HARNESS, detail if isinstance(detail, str) else json.dumps(detail))
            )
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
    """From runner.json history entries of the run's play sessions. A quick exit (claude_quick_exit: auth, config or
    usage) is a process that exited within a minute without a tool call: one that played (`tool_calls`) just finished
    fast (N15: the scripted reload agent's 31 s save + load voided its run 3 times). An entry without the count is
    taken by its time alone."""
    out = []
    for h in sessions:
        sid, t = h.get("session"), h.get("ended") or h.get("started")
        dur = (h.get("ended") or 0) - (h.get("started") or 0)
        if h.get("reason") == "killed":
            out.append(failure(t, "claude_killed", UNKNOWN, f"{sid}: claude ended by a signal from outside the runner"))
        elif h.get("reason") == "exited" and 0 <= dur < 60 and not h.get("tool_calls"):
            out.append(
                failure(
                    t,
                    "claude_quick_exit",
                    HARNESS,
                    f"{sid}: claude exited after {dur:.0f} s (exit {h.get('exit')}: auth, config or usage)",
                )
            )
        if h.get("early_exit_min") is not None:
            out.append(failure(t, "early_exit", AGENT, f"{sid}: ended the session with {h['early_exit_min']} min left"))
        if h.get("started_at_game_over"):
            out.append(failure(h.get("started"), "party_wipe", AGENT, f"{sid} started at the Game Over screen"))
    return out


def progress_failures(paths: list[Path], since: float) -> list[dict]:
    """Stalls: the progress poller's `stuck` records (no story progress for a while although the agent acted)."""
    out = []
    for p in paths:
        for r in read_jsonl(p):
            if r.get("type") == "stuck" and (r.get("t") or 0) >= since:
                out.append(
                    failure(
                        r.get("t"),
                        "stall",
                        AGENT,
                        f"{p.name.split('.')[0]}: no story progress for "
                        f"{r.get('idle_min')} min ({r.get('actions')} tool calls meanwhile)",
                    )
                )
    return out


HAND_INPUT = ("click", "press_key", "ui_action")  # raw UI input tools
UNKNOWN_KINDS = ("tool_failed", "hand_input", "bg_timeout")  # stream failures listed, never voiding a run
MOVE_TOOLS = ("move", "move_to", "walk_toward", "travel_to")  # "FAILED: beyond remaining movement" is routine


def stream_failures(stream: Path) -> list[dict]:
    """One play stream (Sxxx.jsonl / Sxxx-rN.jsonl, any client): MCP server not connected, model API/usage errors
    (kind claude_api, whichever client), tool exceptions and timeouts, fairness-guard blocks, fairness leaks, Game
    Over. Same-kind failures of one tool and error are grouped (n)."""
    sid = stream.name[: -len(".jsonl")]
    try:
        t_end = stream.stat().st_mtime
        lines = stream.read_text(errors="replace").splitlines()
    except OSError:
        return []
    out = []
    for ev in stream_events(lines):
        if ev.kind == "init":
            for m in ev.mcp or []:
                name, _, status = m.partition(":")
                if name == "bg3" and status != "connected":
                    out.append(failure(None, "mcp_failed", HARNESS, f"{sid}: the bg3 MCP server is {status}"))
        elif ev.api_error and ((ev.kind == "result" and ev.is_error) or ev.kind in ("text", "error")):
            out.append(failure(t_end, "claude_api", HARNESS, f"{sid}: {ev.text}"))
    calls, _ = triage.load_calls(stream, Path(str(stream)[: -len(".jsonl")] + ".turns.jsonl"))
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
            if ARG_VALIDATION_RE.search(msg):
                continue  # a rejected call: the server said no to the agent's arguments
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
                continue  # a macro step, a partial walk, NOT READY / REFUSED / a rejected call: the game said no
            groups.setdefault((kind, step, triage.error_key(msg)), []).append(c)
        for step, msg in c["bg_fails"]:
            # a background action's TIMEOUT echoed in this call's EVENTS (a companion's walk-back queue): a harness
            # defect the call didn't depend on (it returned), so listed as unknown, never a harness failure
            if msg.startswith("TIMEOUT"):
                groups.setdefault(("bg_timeout", step, triage.error_key(msg)), []).append(c)
        if name in HAND_INPUT:
            # raw clicks and keys: the agent working around a tool (the definition of done wants 0 hand clicks)
            groups.setdefault(("hand_input", name, ""), []).append(c)
    for (kind, tool, key), cs in groups.items():
        cls = AGENT if kind == "fairness_block" else UNKNOWN if kind in UNKNOWN_KINDS else HARNESS
        out.append(failure(cs[0].get("t") or t_end, kind, cls, f"{sid}: {tool}: {key}", len(cs)))
    if wipe is not None:
        out.append(failure(wipe.get("t") or t_end, "party_wipe", AGENT, f"{sid}: Game Over ({wipe['name']})"))
    for rule, hits in fairness.audit_calls(calls).items():
        if fairness.RULES[rule][0] == fairness.LEAK:
            out.append(
                failure(
                    hits[0][0].get("t") or t_end, "fairness_leak", HARNESS, f"{sid}: {rule}: {hits[0][1]}", len(hits)
                )
            )
    return out


# the kinds stream_failures writes, each detail starting "<session>: " (a history party_wipe says "S001 started at")
STREAM_KINDS = (
    "mcp_failed",
    "claude_api",
    "fairness_block",
    "party_wipe",
    "fairness_leak",
    "tool_timeout",
    "tool_exception",
    "tool_failed",
    "hand_input",
    "bg_timeout",
)
OUTCOME_KINDS = ("timeout", "cost_cap", "tokens_cap", "time_cap")


def restream_failures(record: dict, state_dir: Path) -> list[dict]:
    """A run record's failures with its play streams' classified again by today's stream_failures, the runner-event,
    history, progress and reload ones kept and a timeout's or cap's class recomputed: what the record would say now
    (bg3bench.regrade). Reads the run's files only."""
    since = record.get("started") or 0
    streams = [f for f in sorted(state_dir.iterdir()) if PLAY_STREAM_RE.match(f.name) and f.stat().st_mtime >= since]
    sids = tuple(f"{f.name[: -len('.jsonl')]}: " for f in streams)
    old = record.get("failures") or []
    kept = [
        f
        for f in old
        if f["kind"] not in OUTCOME_KINDS
        and not (f["kind"] in STREAM_KINDS and str(f.get("detail") or "").startswith(sids))
    ]
    fails = kept + [f for s in streams for f in stream_failures(s)]
    for f in old:
        if f["kind"] in OUTCOME_KINDS:
            cls = UNKNOWN if any(g["class"] == HARNESS for g in fails) else AGENT
            fails.append({**f, "class": cls})
    return sorted(fails, key=lambda f: (f["t"] is not None, f["t"] or 0))


def reload_failures(actions: list[dict]) -> list[dict]:
    """Every load_game the agent called (runtime/actions.jsonl): a reload it chose."""
    out = []
    for a in actions:
        if a.get("tool") == "load_game":
            args = a.get("args") or {}
            out.append(
                failure(a.get("t"), "reload", AGENT, f"load_game {args.get('save') or args.get('label') or ''}".strip())
            )
    return out


def outcome_failure(outcome: str, why: str, t: float, others: list[dict], cap: str | None = None) -> list[dict]:
    """The run's own ending as a failure: a timeout or a cost/token cap is the agent's (unknown when harness failures
    hit the run), a give-up and a runner error are ours."""
    if outcome in ("timeout", "over_budget"):
        cls = UNKNOWN if any(f["class"] == HARNESS for f in others) else AGENT
        kind = "timeout" if outcome == "timeout" else f"{cap or 'cost'}_cap"
        return [
            failure(
                t,
                kind,
                cls,
                why
                or (
                    "the run's time ran out before the milestone"
                    if outcome == "timeout"
                    else f"the run's {cap or 'cost'} cap was used up before the milestone"
                ),
            )
        ]
    if outcome == "gave_up":
        return [failure(t, "gave_up", HARNESS, why)]
    if outcome == "runner_error":
        return [failure(t, "runner_error", HARNESS, why)]
    return []


# A play process's ending (SessionResult.reason) that is ours: the session path (LiveRunHost.record) has no run loop
# whose events or history would show it (a toolless agent ended at once, a play command that didn't start, a
# settings mismatch that aborted the run, a usage/rate limit).
HARNESS_REASONS = {
    "mcp_failed": "the agent's bg3 tools were missing",
    "setup_failed": "the play process didn't start",
    "aborted": "the runner aborted the run",
    "api_limit": "the model API refused (usage or rate limit)",
}


def reason_failures(reason: str, detail: str, t: float, others: list[dict]) -> list[dict]:
    """The session's ending reason as a harness failure (HARNESS_REASONS), unless one of that kind is listed."""
    if reason not in HARNESS_REASONS or any(f["kind"] == reason for f in others):
        return []
    return [failure(t, reason, HARNESS, detail or HARNESS_REASONS[reason])]


def classify_run(
    state_dir: Path,
    run_started: float,
    now: float,
    sessions: list[dict],
    events: list[dict],
    actions: list[dict],
    outcome: str,
    why: str,
    cap: str | None = None,
    ending: tuple[str, str] | None = None,
) -> list[dict]:
    """Every failure of one run, in time order (failures without a time first). ending: the session path's last
    process (reason, detail) when there is no run loop (reason_failures)."""
    fails, _ = event_failures(events)
    fails += history_failures(sessions)
    files = []
    with contextlib.suppress(OSError):
        files = sorted(state_dir.iterdir()) if state_dir.is_dir() else []
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
    if ending is not None:
        fails += reason_failures(*ending, now, fails)
    fails += outcome_failure(outcome, why, now, fails, cap)
    return sorted(fails, key=lambda f: (f["t"] is not None, f["t"] or 0))


def is_clean(outcome: str, failures: list[dict], operator: list[dict]) -> bool:
    """The milestone reached with no harness failure and no operator action: a run that scores the model alone."""
    return outcome == "reached" and not operator and not any(f.get("class") == HARNESS for f in failures)


MODEL_TIME_DOCS = ("measured", "estimated", "not_separable")  # model_time's constant field lists: not per record


def model_time_field(mt: dict) -> dict:
    """The `model_time` of a run record: model_time.model_time() without its constant documentation lists."""
    return {k: v for k, v in (mt or {}).items() if k not in MODEL_TIME_DOCS}


TOKEN_FIELDS = (
    ("input", "input_tokens"),
    ("output", "output_tokens"),
    ("cache_read", "cache_read_input_tokens"),
    ("cache_write", "cache_creation_input_tokens"),
)


def usage_tokens(usage: dict | None) -> dict:
    u = usage if isinstance(usage, dict) else {}
    return {k: int(u.get(src) or 0) for k, src in TOKEN_FIELDS}


def token_total(tokens: dict | None) -> int:
    """What --max-tokens counts: every token claude processed (input, output, cache reads and writes)."""
    return sum(int(v or 0) for v in (tokens or {}).values())


# The first event type of a stream → the client that wrote it (claude: system init; codex: thread.started; gemini:
# init). A stream from before the client interface is claude's.
STREAM_CLIENTS = {"system": "claude", "thread.started": "codex", "init": "gemini", "session": "pi"}
# The lines claude_totals needs, per client (a substring test before json: streams run to tens of MB).
RESULT_MARKS = {
    "claude": ('"type": "result"', '"type":"result"'),
    "codex": ("thread.started", "turn.completed", "turn.failed"),
    "gemini": ('"type": "result"', '"type":"result"', '"type":"init"', '"type": "init"'),
}


def stream_client(lines: list[str]) -> str:
    """The client whose stream these lines are (STREAM_CLIENTS by the first JSON object's type; default claude)."""
    for ln in lines:
        try:
            ev = json.loads(ln)
        except ValueError:
            continue
        if isinstance(ev, dict):
            return STREAM_CLIENTS.get(ev.get("type"), "claude")
    return "claude"


def stream_events(lines: list[str], parsers: dict | None = None, name: str | None = None) -> list:
    """The lines of one stream as clients.Events, parsed by the client that wrote it (name, default
    stream_client(lines)). parsers: client name → instance, shared across the streams of a run (codex's resumed
    threads report running totals: its parser keeps the last total per thread, so feed streams in write order)."""
    name = name or stream_client(lines)
    parsers = {} if parsers is None else parsers
    if name == "pi" and name not in parsers:  # pi's parser needs no profile
        from . import pi_client

        parsers[name] = pi_client.PiParser()
    elif name not in parsers:  # parsing only: no MCP server config needed (codex/gemini would read play/.mcp.json)
        parsers[name] = clients.make(name, **({} if name == "claude" else {"server": {}}))
    parser = parsers[name]
    out = []
    for ln in lines:
        out.extend(parser.parse(ln))
    return out


def process_client(base: Path) -> str:
    """The client one process ran (base: its path without the extension): its events log's session_start, else the
    raw stream's first event (stream_client)."""
    try:
        with open(base.with_name(base.name + ".events.jsonl"), encoding="utf-8") as f:
            first = json.loads(f.readline() or "{}")
        if first.get("kind") == "session_start" and (first.get("detail") or {}).get("client"):
            return str(first["detail"]["client"])
    except (OSError, ValueError, AttributeError):
        pass
    try:
        with open(base.with_name(base.name + ".jsonl"), encoding="utf-8", errors="replace") as f:
            return stream_client(list(itertools.islice(f, 20)))
    except OSError:
        return "claude"


def own_cost(ev, running: dict) -> float | None:
    """The cost a RUNNING_COST client's result event adds (clients.RUNNING_COST: its cost_usd is the conversation's
    running total over --resume, so a conversation costs the highest total it reported): what the figure adds to
    the conversation's highest so far. running: conversation id -> that highest figure, updated here. A result
    without a conversation id is its process's own. Assumed, not yet seen live (research/fix/harness.md L5): a claude
    process killed before its result (claude_totals adds it at its token estimate, unfinished_result) is NOT in the
    next resume's total_cost_usd; if claude carries it over, that process is counted twice."""
    if ev.cost_usd is None or not ev.session_id:
        return ev.cost_usd
    prev = running.get(ev.session_id, 0.0)
    running[ev.session_id] = max(prev, float(ev.cost_usd))
    return running[ev.session_id] - prev


def claude_totals(state_dir: Path, since: float) -> dict:
    """Cost, turns and tokens of every play-agent stream written since `since` (play, resumes, -fix, -keeper; any
    client): their result events (events.load_events); a claude conversation costs the highest running total it
    reported (own_cost; running: conversation id -> that total, UsageMeter's seed), not the sum over its resumes
    (Opus act1_gold_max: $42.09 for 9.70). A process stopped before its result (a cap, a hang, a crash: no
    result event, its events log's session_end; stopped_usage) counts its usage events, UsageMeter's way: the tokens per
    message id, one turn per message, the cost its client reported as running (pi), else its tokens at the run's cost
    per token so far (claude reports cost only in its result). A process whose cost can't be known (a codex/gemini
    model without a price, a first process stopped before any result priced a token) adds no cost and sets
    cost_unknown: the cost_usd is then a lower bound, never a run's true cost (H4: it read $0 with 208k tokens).
    served: what the endpoints served (pi's thinking levels and providers), a stopped process's from its last usage."""
    cost, turns, tokens = 0.0, 0, {k: 0 for k, _ in TOKEN_FIELDS}
    unknown = False
    try:
        files = list(state_dir.iterdir()) if state_dir.is_dir() else []
    except OSError:
        files = []
    found = []
    for f in files:
        try:
            if CLAUDE_STREAM_RE.match(f.name) and (mtime := f.stat().st_mtime) >= since:
                found.append((mtime, f.name, f))
        except OSError:
            continue
    parsers: dict = {}
    served: dict = {}  # what the endpoints served (clients.Event.served), merged in first-seen order
    running: dict = {}
    for _, _, f in sorted(found):  # in the order written: a resume after the process it resumes
        base = f.with_name(f.name[: -len(".jsonl")])
        evs = events.load_events(base, kinds={"result"}, parsers=parsers)
        reported = bool(evs) and process_client(base) in clients.RUNNING_COST
        if not evs and (usage := stopped_usage(base)) is not None:
            evs = [unfinished_result(usage, cost, token_total(tokens))]
        for ev in evs:
            unknown = unknown or (ev.cost_usd is None and token_total(ev.tokens) > 0)
            cost += float((own_cost(ev, running) if reported else ev.cost_usd) or 0)
            turns += int(ev.turns or 0)
            for k in clients.TOKEN_KEYS:
                tokens[k] += int((ev.tokens or {}).get(k) or 0)
            for k, values in (ev.served or {}).items():
                served.setdefault(k, []).extend(v for v in values if v not in served[k])
    return {
        "cost_usd": cost,
        "turns": turns,
        "tokens": tokens,
        "cost_unknown": unknown,
        "served": served or None,
        "running": running,
    }


def stopped_usage(base: Path) -> list | None:
    """The usage events of a process that ended without its result (its events log has the runner's session_end),
    else None: a stream still running (or one from before the events log) is no finished process."""
    if not base.with_name(base.name + ".events.jsonl").exists():
        return None
    evs = events.load_events(base, kinds={"usage", "session_end"})
    return [e for e in evs if e.kind == "usage"] if any(e.kind == "session_end" for e in evs) else None


def unfinished_result(usage: list, done_cost: float, done_tokens: int):
    """The result a process stopped before its own would have reported, from its usage events (claude_totals): the
    last tokens per message id, one turn per message, the last running cost (pi), else the tokens at done_cost per
    done_token (UsageMeter's live rate); no cost (None) when nothing priced a token yet."""
    msgs: dict = {}
    running = served = None
    for ev in usage:
        msgs[ev.message_id or f"#{len(msgs)}"] = ev.tokens or {}
        if ev.cost_usd is not None:
            running = float(ev.cost_usd)
        served = ev.served or served
    tokens = {k: sum(int(t.get(k) or 0) for t in msgs.values()) for k in clients.TOKEN_KEYS}
    if running is None and done_tokens:
        running = token_total(tokens) * done_cost / done_tokens
    return clients.Event("result", turns=len(msgs), cost_usd=running, tokens=tokens, served=served)


class UsageMeter:
    """A run's cost and tokens while a play process streams: `base` (the run's finished processes, claude_totals)
    plus this stream's running usage (clients.Event "usage", per message id) until its result event gives the exact
    totals. Claude reports cost only in result events, so the live cost is the live tokens at the run's cost per
    token so far (none before the run's first result: a cost cap then trips at the end of the first process). A
    client whose usage events carry the process's running cost (pi: billed, or its estimate) is metered by that.
    running: for a clients.RUNNING_COST client, claude_totals' running (its results' cost is the conversation's so
    far: own_cost); None: a result's cost is its process's."""

    def __init__(self, base_cost: float = 0.0, base_tokens: int = 0, running: dict | None = None):
        self.done_cost, self.done_tokens = float(base_cost), int(base_tokens)
        self.running = running
        self.msgs: dict = {}
        self.live = 0
        self.live_cost: float | None = None

    def on_event(self, ev) -> None:
        """One clients.Event."""
        if ev.kind == "usage":
            mid = ev.message_id or f"#{len(self.msgs)}"
            n = token_total(ev.tokens)
            self.live += n - self.msgs.get(mid, 0)
            self.msgs[mid] = n
            if ev.cost_usd is not None:
                self.live_cost = float(ev.cost_usd)
        elif ev.kind == "result":
            self.done_cost += float((ev.cost_usd if self.running is None else own_cost(ev, self.running)) or 0)
            self.done_tokens += token_total(ev.tokens)
            self.msgs, self.live, self.live_cost = {}, 0, None

    @property
    def tokens(self) -> int:
        return self.done_tokens + self.live

    @property
    def cost(self) -> float:
        if self.live_cost is not None:
            return self.done_cost + self.live_cost
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


def _recent(state_dir: Path, since: float, pattern: re.Pattern, until: float | None = None) -> list[Path]:
    """The files matching `pattern` written since `since` (and not after `until`: a backfill of an old record)."""
    out = []
    try:
        files = sorted(state_dir.iterdir()) if state_dir.is_dir() else []
    except OSError:
        return out
    for f in files:
        try:
            mtime = f.stat().st_mtime if pattern.match(f.name) else None
            if mtime is not None and mtime >= since and (until is None or mtime <= until):
                out.append(f)
        except OSError:
            continue
    return out


FIGHTS_RE = re.compile(r"^S\d{3,}\.fights\.jsonl$")


def death_counts(state_dir: Path, since: float, failures: list[dict], until: float | None = None) -> dict:
    """Party downs and deaths in the run's fights (Sxxx.fights.jsonl, the MCP server's fight debriefs: party[].downed
    and .died per fight) and Game Overs (the run's party_wipe failures). Downs or deaths outside a fight the
    debrief saw (a fall, a trap out of combat) are not observable in the logs and not counted."""
    fights = downs = deaths = 0
    for f in _recent(state_dir, since, FIGHTS_RE, until):
        for r in read_jsonl(f):
            fights += 1
            for m in r.get("party") or []:
                if isinstance(m, dict):
                    downs += int(m.get("downed") or 0)
                    deaths += 1 if m.get("died") else 0
    wipes = sum(int(x.get("n") or 1) for x in failures or [] if x.get("kind") == "party_wipe")
    return {"fights": fights, "downs": downs, "deaths": deaths, "wipes": wipes}


def fairness_flags(streams: list[Path]) -> dict:
    """fairness.py over the run's play streams (their events: triage.load_calls): hits per rule (LEAK and
    CHECK), plus the sandbox/fairness guard's blocks of the agent's own file calls."""
    kinds: Counter = Counter()
    leak = check = blocks = 0
    for s in streams:
        calls, _ = triage.load_calls(s, Path(str(s)[: -len(".jsonl")] + ".turns.jsonl"))
        blocks += sum(1 for c in calls if c["name"] in fairness.NOT_GAME and GUARD_BLOCK in (c.get("text") or ""))
        for rule, hits in fairness.audit_calls(calls).items():
            kinds[rule] += len(hits)
            if fairness.RULES[rule][0] == fairness.LEAK:
                leak += len(hits)
            else:
                check += len(hits)
    return {"count": leak + check, "leak": leak, "check": check, "blocks": blocks, "kinds": dict(kinds)}


LATENCY_RE = re.compile(r"^S\d{3,}(?:-[rc]\d+)?\.latency\.json$")  # play sessions only (not -fix/-keeper)


def _weighted_median(pairs: list[tuple[float, int]]) -> float | None:
    pairs = sorted((v, n) for v, n in pairs if v is not None and n)
    total = sum(n for _, n in pairs)
    acc = 0
    for v, n in pairs:
        acc += n
        if acc * 2 >= total:
            return v
    return None


def latency_summary(state_dir: Path, since: float, until: float | None = None) -> dict | None:
    """The run's live feel from its play sessions' Sxxx.latency.json (bg3ai.metrics): idle gap median and p90 (s),
    gaps over 10 s, the read tools' round-trip median (ms). Over several sessions each value is the median of the
    per-session values weighted by their sample counts (exact for a one-session run); None: no latency files."""
    idle_med, idle_p90, rt, gaps, n = [], [], [], 0, 0
    for f in _recent(state_dir, since, LATENCY_RE, until):
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
    return {
        "sessions": n,
        "idle_median_s": _weighted_median(idle_med),
        "idle_p90_s": _weighted_median(idle_p90),
        "gaps_over_10s": gaps,
        "read_rt_median_ms": _weighted_median(rt),
    }


def result_tokens_summary(state_dir: Path, since: float, until: float | None = None) -> dict | None:
    """The tool results' size in estimated model tokens over the run's play sessions (Sxxx.latency.json
    "result_tokens", bg3ai.metrics; research/impl-vis.md): total, results counted, mean per result, results with an
    image. Sessions written before the field are backfilled from their raw stream (Sxxx.jsonl next to it,
    source "stream"). None: no latency files."""
    total = n = images = sessions = 0
    sources = set()
    for f in _recent(state_dir, since, LATENCY_RE, until):
        try:
            d = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        rt = d.get("result_tokens") if isinstance(d.get("result_tokens"), dict) else {}
        if rt.get("total") is None:
            stream = f.with_name(f.name.removesuffix(".latency.json") + ".jsonl")
            rt = (
                dict(
                    triage.latency.result_token_stats(triage.latency.result_tokens_from_stream(stream)), source="stream"
                )
                if stream.is_file()
                else {}
            )
            if not rt.get("n"):
                continue
        sessions += 1
        total += int(rt.get("total") or 0)
        n += int(rt.get("n") or 0)
        images += int(rt.get("images") or 0)
        sources.add(rt.get("source") or "turns")
    if not sessions:
        return None
    return {
        "sessions": sessions,
        "total": total,
        "n": n,
        "per_result": round(total / n, 1) if n else None,
        "images": images,
        "source": "+".join(sorted(sources)),
    }


def story_gained(state_dir: Path, since: float, until: float | None = None) -> dict:
    """Story progress points the run's sessions gained (progress_history.jsonl, bg3ai.progress: one line per
    session) and their events by kind."""
    points = events = sessions = 0
    by_kind: Counter = Counter()
    for r in read_jsonl(state_dir / "progress_history.jsonl"):
        if (r.get("t") or 0) < since or (until is not None and (r.get("t") or 0) > until):
            continue
        sessions += 1
        points += int(r.get("points") or 0)
        events += int(r.get("events") or 0)
        by_kind.update({k: int(v or 0) for k, v in (r.get("by_kind") or {}).items()})
    return {"points": points, "events": events, "sessions": sessions, "by_kind": dict(by_kind)}


def run_metrics(
    state_dir: Path,
    run_started: float,
    reached_at: float | None,
    failures: list[dict],
    until: float | None = None,
) -> dict:
    """The benchmark fields of a run record beyond the failures (each a pure function above). until: the run's end,
    for a record computed after the fact (backfill_metrics); None = now (the record being written)."""
    streams = _recent(state_dir, run_started, PLAY_STREAM_RE, until)
    return {
        "time_to_milestone_s": time_to_milestone(run_started, reached_at),
        "deaths": death_counts(state_dir, run_started, failures, until),
        "fairness_flags": fairness_flags(streams),
        "latency": latency_summary(state_dir, run_started, until),
        "story": story_gained(state_dir, run_started, until),
        "result_tokens": result_tokens_summary(state_dir, run_started, until),
    }


# run_metrics' keys
METRIC_FIELDS = ("time_to_milestone_s", "deaths", "fairness_flags", "latency", "story", "result_tokens")
BACKFILL_SLACK_S = 5.0  # as model_time.WINDOW_SLACK_S: a run's last files are written a little before its record


def backfill_metrics(state_dir: Path, rec: dict) -> dict:
    """The run_metrics fields a record written before them lacks, computed from its files now (the run's window:
    started .. t + slack); {} when it has them all or has no start. A present field is never in the result, so
    merging it (update_record) never overwrites a value."""
    missing = [k for k in METRIC_FIELDS if k not in rec]
    started, end = rec.get("started"), rec.get("t")
    if not missing or not isinstance(started, (int, float)):
        return {}
    until = end + BACKFILL_SLACK_S if isinstance(end, (int, float)) else None
    m = run_metrics(state_dir, started, rec.get("reached_at"), rec.get("failures") or [], until)
    return {k: m[k] for k in missing}


def backfill_model_time(state_dir: Path, rec: dict) -> dict:
    """{"model_time": …} for a record without one, or whose model_time lacks keys a newer model_time.py reports
    (api_floor, the headline): computed over the run's window and merged under the present keys, so a value already
    in the record is never changed. {} when nothing is missing, no start, or no timing in the window."""
    started, end = rec.get("started"), rec.get("t")
    old = rec.get("model_time")
    if not isinstance(started, (int, float)) or (
        isinstance(old, dict) and "api_floor" in old and model_time.HEADLINE in old
    ):
        return {}
    until = end + BACKFILL_SLACK_S if isinstance(end, (int, float)) else None
    fresh = model_time_field(model_time.model_time(state_dir, since=started, until=until))
    if not fresh.get("sessions"):
        return {}
    return {"model_time": {**fresh, **(old if isinstance(old, dict) else {})}}


def time_to_milestone(started: float | None, reached_at: float | None) -> float | None:
    """Wall seconds from the run's start to the milestone (None: not reached). bg3bench.records imports it."""
    if started is None or reached_at is None:
        return None
    return round(reached_at - started, 1)

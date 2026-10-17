#!/usr/bin/env python3
"""One-command review of a finished play session, for the logs worker (playthrough mode).

    python3 tools/logs/review_session.py claude-run-2 S065            # writes runtime/sessions/claude-run-2/S065.review.md
    python3 tools/logs/review_session.py naut2 S001 --stdout         # print instead
    python3 tools/logs/review_session.py claude-run-2 S065 --runtime DIR [--sessions DIR]  # other roots

Sections: headline (minutes, story points/h, progress, stuck, fights/deaths, cost, latency targets PASS/FAIL, APM,
pipelining, think overlap), long non-dialogue gaps with their cause, refusal episodes ranked by model seconds lost
(tools/vis/overlap.py), failed steps inside batches, hand clicks/screenshots, fights (fights.py: bad fights, retries
of earlier fights, same tactic?), SESSION-WATCH signatures (session_watch.py + series_report.py's naut3 rows),
memory/keeper health, runner faults/crashes/relaunches (runner_events.jsonl + runtime/run-*.log), and a TOP 10 of
harness issues with an owner guess and evidence pointers (session #call +s tool). Read-only: it writes only the
review file. Calls are numbered as overlap.load numbers them (timed calls of the session and its resumes).
"""
from __future__ import annotations

import argparse
import collections
import json
import re
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "tools" / "vis"))
sys.path.insert(0, str(REPO / "tools" / "logs"))
import series_report as SR  # noqa: E402  (puts runner/ and bridge/src on the path)
import overlap  # noqa: E402
import triage  # noqa: E402
from bg3ai import metrics as M  # noqa: E402
import fights as F  # noqa: E402
import hand_clicks as HC  # noqa: E402
import session_watch as SW  # noqa: E402

RUNTIME = overlap.repo_paths.runtime_dir()   # $BG3AI_RUNTIME, else the main tree's runtime/ (also from a worktree)
LONG_GAP_S = triage.LONG_GAP_S
TOP_N = 10
HAND = ("click", "press_key", "screenshot", "zoom", "camera")
MEMORY = ("note", "end_session", "session_clock", "Read", "Edit", "Write", "Glob", "Grep")
OWNERS = {   # docs/ORCHESTRATOR.md "Ownership": act = action tools/UI, vis = nav/perception/observations, setup = runner,
    # playbooks, progression, memory
    "vis": {"travel_to", "walk_toward", "move_to", "route", "look", "look_at", "get_nearby", "get_frontier", "get_map",
            "get_status", "get_layout", "path_preview", "jump_to", "wait_for", "get_party", "get_inventory",
            "get_journal", "get_dialogue", "get_combat_state", "get_actions", "regroup"},
    "setup": {"level_up", "create_character", "new_game", "character_creation_options", "set_party", "note",
              "end_session", "session_clock", "session_start", "Read", "Edit", "Write", "Glob", "Grep", "ToolSearch"},
}
RUNNER_KINDS = ("fault", "game_relaunch", "game_relaunch_skipped", "game_oom", "recovered", "recovery_failed",
                "setup_failed", "steam_refused", "runner_restart", "runner_restarted", "agent_hang")
RUNLOG_RX = re.compile(r"FAULT|crash|killing the game|relaunching|\bOOM\b|\boom\b|recover|early exit|resuming|"
                       r"\bhang\b|\bhung\b|REFUSING|\bkilled\b|memory left open|setup refused|stuck detector")


def owner(tool: str) -> str:
    if tool.startswith(("memory_", "wiki_")):
        return "setup"
    return next((o for o, names in OWNERS.items() if tool in names), "act")


def hm(t: float | None) -> str:
    return time.strftime("%H:%M:%S", time.localtime(t)) if t else "--:--:--"


def _json(path: Path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


# ======================================================================== load

def stems(sd: Path, sid: str) -> list[Path]:
    """The session and its resumes (S065, S065-r1, S065-c1 ...) that have a turns file."""
    out = [sd / sid] + sorted(sd / f.name[:-6] for f in sd.glob(f"{sid}-*.jsonl")
                              if re.match(rf"{sid}-[rc]\d+\.jsonl$", f.name))
    return [s for s in out if (s.parent / (s.name + ".turns.jsonl")).exists()]


def load(sd: Path, sid: str) -> list[dict]:
    """overlap.load's timed calls of the session (+ resumes), each with k (index), session, t0 (session start)."""
    calls = []
    for stem in stems(sd, sid):
        for c in overlap.load(stem):
            c["k"], c["session"] = len(calls), stem.name
            calls.append(c)
    for c in calls:
        c["t0"] = calls[0]["t"]
    return calls


def ev(c: dict, text: str = "") -> str:
    """Evidence pointer: S065 #77 +516s load_game: text."""
    s = f"{c['session']} #{c['k']} +{c['t'] - c['t0']:.0f}s {c['name']}"
    return s + (f": {text.strip()[:160]}" if text else "")


def next_think(calls: list[dict], k: int) -> float:
    """The model time the agent spent right after call k (its reaction to the result)."""
    return calls[k + 1]["think"] if k + 1 < len(calls) else 0.0


def stream_result(path: Path) -> dict | None:
    res = None
    for e in M.read_jsonl(path):
        if e.get("type") == "result":
            res = e
    return res


# ======================================================================== sections

def play_gaps(stem: Path, lat: dict) -> tuple[list[dict], list[float]]:
    """A stream's idle gaps outside wrap-up and its read round trips (ms), as metrics.summarize finds them for
    Sxxx.latency.json: several streams' targets come from these pooled (a median of medians is none)."""
    recs = M.read_jsonl(Path(f"{stem}.metrics.jsonl"))
    tools = [r for r in recs if r.get("type") == "tool" and "t0" in r]
    samples = [r for r in recs if r.get("type") == "sample"]
    turns = M.read_jsonl(Path(f"{stem}.turns.jsonl"))
    turns += M.wrapup_from_stream(Path(f"{stem}.jsonl"), turns)
    wrap_t = M.wrapup_start(turns)
    if wrap_t is not None and lat.get("start") is not None and not lat["start"] <= wrap_t < lat.get("end", wrap_t + 1):
        wrap_t = None
    gl = [g for g in M.gaps(M.action_intervals(tools) or M.stream_actions(turns), samples, tools, wrap_t)
          if g["kind"] != "wrapup"]
    skip = M.ACTION_TOOLS | M.BATCH_TOOLS | M.NOT_READ | M.NOT_GAME_READ | M.NOT_OBS
    return gl, [r["ms"] for r in tools if not r.get("nested") and r["tool"] not in skip]


def pooled_targets(legs: list[Path], lats: list[dict]) -> dict:
    """latency.json's three targets over all of a session's streams."""
    gl, reads = [], []
    for stem, lat in zip(legs, lats):
        g, r = play_gaps(stem, lat)
        gl += g
        reads += r
    ex = M.percentile([g["s"] for g in gl if g["kind"] == "explore"], 50)
    rt = M.percentile(reads, 50)
    long_n = sum(1 for g in gl if g["kind"] in ("explore", "combat") and g["s"] > LONG_GAP_S)
    tg = M.TARGETS
    return {"median_idle_explore_s": {"value": None if ex is None else round(ex, 2),
                                      "target": tg["median_idle_explore_s"],
                                      "pass": None if ex is None else ex < tg["median_idle_explore_s"]},
            "long_gaps_non_dialogue": {"value": long_n, "target": tg["long_gaps_non_dialogue"],
                                       "pass": long_n == 0 if gl else None},
            "bridge_rt_median_ms": {"value": None if rt is None else round(rt, 1), "target": tg["bridge_rt_median_ms"],
                                    "pass": None if rt is None else rt < tg["bridge_rt_median_ms"]}}


def progress(progs: list[dict]) -> dict:
    """Sxxx.progress.json, summed over the session's streams (S078: 0 pts in 1.2 min before the crash, 88 in r1)."""
    ps = [p for p in progs if p]
    if len(ps) < 2:
        return ps[0] if ps else {}
    mins = sum(p.get("minutes") or 0 for p in ps)
    pts = sum(p.get("points") or 0 for p in ps)
    kinds: collections.Counter = collections.Counter()
    for p in ps:
        kinds.update(p.get("by_kind") or {})
    longest = [p["longest_no_progress_min"] for p in ps if isinstance(p.get("longest_no_progress_min"), (int, float))]
    return {"minutes": round(mins, 1), "points": pts, "per_hour": pts / mins * 60 if mins else 0.0,
            "events": sum(p.get("events") or 0 for p in ps), "by_kind": dict(kinds),
            "longest_no_progress_min": max(longest) if longest else "?",
            "stuck_episodes": sum(p.get("stuck_episodes") or 0 for p in ps),
            "stuck_min": round(sum(p.get("stuck_min") or 0 for p in ps), 1)}


def headline(sd: Path, sid: str, calls: list[dict], fights: list[dict], cost: dict) -> list[str]:
    legs = stems(sd, sid) or [sd / sid]   # the session and its resumes: all of them count
    lats = [_json(Path(f"{s}.latency.json")) or {} for s in legs]
    progs = [_json(Path(f"{s}.progress.json")) or {} for s in legs]
    prog = progress(progs)
    per = [lat.get("minutes") or p.get("minutes") or 0 for lat, p in zip(lats, progs)]
    mins = sum(per) or (
        (calls[-1]["t_res"] - calls[0]["t"]) / 60 if calls else 0)
    out = ["## Headline", ""]
    t0 = lats[0].get("start") or (calls[0]["t"] if calls else None)
    out.append(f"- **{sid}** {time.strftime('%Y-%m-%d %H:%M', time.localtime(t0)) if t0 else '?'}, "
               f"{mins:.1f} min" + (f" ({' + '.join(f'{m:.1f}' for m in per)}: {', '.join(s.name for s in legs)})"
                                    if len(legs) > 1 else "")
               + f", {len(calls)} calls ({len(legs)} stream(s))")
    if prog:
        out.append(f"- story: **{prog.get('points', 0)} pts ({prog.get('per_hour', 0):.0f}/h)**, "
                   f"{prog.get('events', 0)} progress events (pts: "
                   + ", ".join(f"{k} {v}" for k, v in sorted((prog.get('by_kind') or {}).items(),
                                                             key=lambda kv: -kv[1]))
                   + f"); longest without progress {prog.get('longest_no_progress_min', '?')} min; "
                   f"stuck episodes {prog.get('stuck_episodes', 0)} ({prog.get('stuck_min', 0)} min)")
    real = [f for f in fights if not f["key"].startswith("INCIDENT")]
    out.append(f"- fights: {len(real)} ({sum(1 for f in real if f['outcome'] == 'reload')} reloaded, "
               f"{sum(1 for f in real if f['outcome'] == 'wipe')} wiped, "
               f"{sum(1 for f in real if F.bad(f))} bad); downs {sum(f['downs'] for f in fights)}; party deaths "
               f"{sorted({n for f in fights for n in f['party_died']}) or 0}")
    out.append(f"- cost: play ${cost['play']:.2f}" + (f" + keeper ${cost['keeper']:.2f}" if cost.get("keeper") else "")
               + (f"; ${cost['play'] / mins * 60:.2f}/h of play" if mins else ""))
    tg = (lats[0].get("targets") or {}) if len(legs) == 1 else pooled_targets(legs, lats) if any(lats) else {}
    if tg:
        cells = []
        for k, label, unit in (("median_idle_explore_s", "idle median (explore)", "s"),
                               ("long_gaps_non_dialogue", "gaps >10 s (non-dialogue)", ""),
                               ("bridge_rt_median_ms", "bridge RT median", "ms")):
            if k in tg:
                v = tg[k]
                cells.append(f"{label} {v['value']}{unit} {'PASS' if v['pass'] else '**FAIL**'} (target "
                             f"{'<' if k != 'long_gaps_non_dialogue' else ''}{v['target']}{unit})")
        out.append("- latency: " + "; ".join(cells) + (" (all streams)" if len(legs) > 1 else ""))
    for stem, lat in zip(legs, lats):
        if not lat:
            continue
        out.append(f"- live feel{' ' + stem.name if len(legs) > 1 else ''}: APM {lat.get('apm', '?')}, calls/min {(lat.get('rates') or {}).get('calls_pm', '?')}, "
                   f"think median {(lat.get('think_s') or {}).get('median', '?')} s (p90 "
                   f"{(lat.get('think_s') or {}).get('p90', '?')}), pipelined {lat.get('pipelining_pct', '?')} %, "
                   f"think overlap {lat.get('think_overlap_pct', '?')} %, busy {lat.get('busy_pct', '?')} %, "
                   f"enemy turns {lat.get('enemy_turn_s', '?')} s, loading {lat.get('loading_s', '?')} s")
    by, _ = overlap.overlap(calls)
    tot = [sum(b[i] for b in by.values()) for i in range(6)]
    model = sum(c["think"] for c in calls)
    out.append(f"- model time {model:.0f} s; blocking calls {tot[2]} = {tot[3]:.0f} s; could overlap: upper "
               f"{tot[4]:.0f} s, realistic {tot[5]:.0f} s (overlap.py)")
    return out + [""]


def gap_cause(calls: list[dict], a: float, b: float) -> tuple[str, str, dict | None]:
    """(cause, owner, the call to point at) of an idle gap [a, b]."""
    before = [c for c in calls if c["t"] <= a][-1:]
    inside = [c for c in calls if a < c["t"] < b]
    names = [c["name"] for c in inside]
    if before and before[0]["refusal"]:
        p = before[0]
        return f"recovering from a refusal: {overlap.refusal_key(p['name'], p['refusal'])[:90]}", owner(p["name"]), p
    if "load_game" in names:
        c = next(c for c in inside if c["name"] == "load_game")
        return "a reload (load_game) and re-orientation", "act", c
    mem = [c for c in inside if c["name"] in MEMORY or c["name"].startswith("memory_")]
    if mem:
        return f"notes/memory during play ({', '.join(sorted({c['name'] for c in mem}))})", "setup", mem[0]
    wiki = [c for c in inside if c["name"].startswith("wiki_")]
    if wiki:
        return "wiki lookups", "setup", wiki[0]
    reads = [c for c in inside if overlap.is_read(c)]
    if reads and len(reads) == len(inside):
        return f"reads only ({', '.join(sorted({c['name'] for c in reads}))})", "vis", reads[0]
    nxt = next((c for c in calls if c["t"] >= b - 0.5), inside[-1] if inside else None)
    return "model think before the next action", "setup", nxt


def gaps(sd: Path, sid: str, calls: list[dict]) -> tuple[list[str], list[dict]]:
    """Long (> 10 s) gaps outside dialogue and wrap-up in all the session's streams, with the cause of each; [issue]
    for the top list."""
    gl = [g for stem in stems(sd, sid) for g in play_gaps(stem, _json(Path(f"{stem}.latency.json")) or {})[0]
          if g["kind"] != "dialogue" and g["s"] > LONG_GAP_S]
    out = [f"## Gaps > {LONG_GAP_S:.0f} s outside dialogue ({len(gl)}; target 0)", ""]
    issues = []
    for g in sorted(gl, key=lambda g: g["t"]):
        a, b = g["t"], g["t"] + g.get("gap_s", g["s"])
        cause, own, c = gap_cause(calls, a, b)
        own_s = f", own turn {g['own_s']:.1f} s" if g.get("own_s") is not None and g["kind"] == "combat" else ""
        out.append(f"- {hm(a)} **{g['s']:.1f} s** {g['kind']}{own_s} → next `{g['next']}`: {cause} [{own}]"
                   + (f" — {ev(c)}" if c else ""))
        issues.append({"cause": cause.split(":")[0] if cause.startswith("recovering") else cause, "own": own,
                       "s": g["s"], "c": c, "detail": cause})
    if not gl:
        out.append("none")
    return out + [""], issues


def refusals(calls: list[dict]) -> tuple[list[str], list[dict]]:
    """overlap.unneeded's refusal episodes grouped by the opening refusal, ranked by model seconds."""
    un = overlap.unneeded(calls)
    groups = collections.defaultdict(list)
    for u in un:
        if u["cat"] == "refusal":
            groups[u["key"]].append(u)
    out = ["## Refusal episodes (model seconds lost, overlap.py)", "", "- " + overlap.unneeded_summary(un), ""]
    issues = []
    for key, us in sorted(groups.items(), key=lambda kv: -sum(u["think"] for u in kv[1])):
        s = sum(u["think"] for u in us)
        c0 = us[0]["calls"][0]
        turns = sum(len(u["calls"]) for u in us)
        out.append(f"- **{s:.1f} s**, {len(us)} episode(s), {turns} turns: `{key[:110]}` [{owner(c0['name'])}] — "
                   f"{ev(c0, c0['refusal'])}")
        issues.append({"title": f"refused: {key[:100]}", "own": owner(c0["name"]), "s": s, "n": len(us),
                       "ev": ev(c0)})
    other = [u for u in un if u["cat"] != "refusal"]
    if other:
        cats = collections.Counter(u["key"] for u in other)
        out.append("- other unneeded turns: " + ", ".join(f"{k} ×{n}" for k, n in cats.most_common(6)))
    if not groups:
        out.append("- no refusals")
    return out + [""], issues


def step_failures(calls: list[dict]) -> tuple[list[str], list[dict]]:
    """Failed steps inside batches/turns/queues (the call itself was not refused): triage's failure groups."""
    groups = collections.defaultdict(list)
    for c in calls:
        if c["refusal"]:
            continue
        for step, msg in c.get("fails") or []:
            groups[(step, triage.error_key(msg))].append((c, msg))
    out = ["## Failed steps inside batches / turns (not refusals)", ""]
    issues = []
    for (step, key), hits in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        s = sum(next_think(calls, c["k"]) for c, _ in {c["k"]: (c, m) for c, m in hits}.values())
        c, msg = hits[0]
        own = owner(step) if step.islower() else owner(c["name"])  # an ability id (Target_X): the turn tool's owner
        out.append(f"- ×{len(hits)} **{step}** `{key[:100]}` (next think {s:.1f} s) [{own}] — {ev(c, msg)}")
        issues.append({"title": f"{step} step: {key[:90]}", "own": own, "s": s, "n": len(hits), "ev": ev(c)})
    if not groups:
        out.append("none")
    return out + [""], issues


def hand(calls: list[dict]) -> tuple[list[str], list[dict]]:
    hs = [c for c in calls if c["name"] in HAND]
    out = [f"## Hand clicks / raw keys / screenshots ({len(hs)}; target 0)", ""]
    if not hs:
        return out + ["none", ""], []
    by = collections.defaultdict(list)
    for c in hs:
        by[HC.bucket({"tool": c["name"], "args": json.dumps(c["input"] or {})})].append(c)
    issues = []
    for b, cs in sorted(by.items(), key=lambda kv: -len(kv[1])):
        s = sum(c["dur"] + c["think"] for c in cs)
        out.append(f"- {b} ×{len(cs)} ({s:.0f} s) — " + "; ".join(ev(c, triage.args_str(c["input"], 60)) for c in cs[:3]))
        issues.append({"title": f"hand {b} ×{len(cs)}", "own": "act", "s": s, "n": len(cs), "ev": ev(cs[0])})
    return out + [""], issues


def fights_section(sessions: Path, run: str, sid: str, store: Path) -> tuple[list[str], list[dict]]:
    """fights.py's fights of the session (all its streams); each compared with the last earlier attempt at the same group (this
    session's or runtime/mined/fights.jsonl's). Reloads are competence, not harness: not in the top list."""
    F.SESSIONS = sessions
    legs = [s.name for s in stems(sessions / run, sid)] or [sid]
    fs = [f for leg in legs for f in F.mine(run, leg)]
    out = [f"## Fights ({len(fs)}; fights.py)", ""]
    prior = [json.loads(l) for l in store.open()] if store.exists() else []
    prior = [f for f in prior if f.get("run", "claude-run-2") == run and f["session"] not in legs]
    for f in fs:
        out.append(f"- {'**BAD** ' if F.bad(f) else ''}{f['start']} {f['dur_s']} s r{f['rounds']} hp-{f['hp_lost']} "
                   f"downs {f['downs']} {f['outcome']}{' (' + f['debrief'] + ')' if f.get('debrief') else ''}"
                   f"{' → ' + f['reload_to'] if f.get('reload_to') else ''}: {f['key']}; died {f['party_died'] or '-'}; "
                   f"harness {[k for k, v in f['harness'].items() if v] or '-'}; top {list(f['abilities'])[:4]}")
        # an earlier attempt at the same group (>= 2 shared enemies): better? same tactic?
        prev = sorted((p for p in prior + fs if p["t"] < f["t"] and len(set(p["enemies"]) & set(f["enemies"])) >= 2),
                      key=lambda p: p["t"])   # earlier in time only: the store also holds later sessions
        if prev:
            p = prev[-1]
            verdict = F.retry_verdict(p, f)
            same = F.same_tactic(p, f)
            out.append(f"  - retry of {p['session']} {p['start']} ({p['outcome']}, hp-{p['hp_lost']}, downs "
                       f"{p['downs']}): {'**NOT better**' if verdict == 'NOT better' else verdict}"
                       + (f"; SAME TACTIC {same}" if same else "")
                       + ("" if f["harness"].get("lesson") else "; no lesson shown"))
    if not fs:
        out.append("none")
    return out + [""], fs


def watch(calls: list[dict], ctx: dict) -> tuple[list[str], list[dict]]:
    out = ["## SESSION-WATCH signatures (session_watch.py; series_report.py naut3 rows)", ""]
    issues = []
    rows = SW.check(calls)
    for name, nt, np_, nf, verdict, ex in rows:
        if verdict in ("FAIL?", "PASS?"):
            out.append(f"- {verdict} {name} (trigger {nt}, pass {np_}, fail {nf}): {ex[:200]}")
        if nf:
            m = re.match(r"#(\d+)", ex)
            k = int(m.group(1)) if m else 0
            issues.append({"title": f"SESSION-WATCH FAIL: {name} ×{nf}", "own": owner(calls[k]["name"]),
                           "s": next_think(calls, k) * nf, "n": nf, "ev": ev(calls[k])})
    seen = [f"{r[0].split(' (')[0]} ×{r[1]}" for r in rows if r[4] == "seen"]
    if seen:
        out.append(f"- seen (a tool the row names ran), no PASS/FAIL signature: {'; '.join(seen)}")
    hand = [r[0].split(" (")[0] for r in rows if r[4] == "by hand"]
    if hand:
        out.append(f"- no transcript signature, check from R/B/L/M by hand: {'; '.join(hand)}")
    for c in calls:
        c.setdefault("t0", calls[0]["t"])
    nautiloid = any("Nautiloid" in c["text"] for c in calls)
    for key, what, check in SR.SIGNATURES:
        if key.startswith("helm") and not nautiloid:
            continue   # the helm checks read any ladder pair (S059 Wyrm's Rock: "helm-once FAILED")
        v, e, npass, nfail = SR.verdict(calls, check, ctx)
        if v == "NOT-SEEN":
            continue
        k, line = e
        out.append(f"- {v} {key}: {what} — {ev(calls[k], line or '')} [pass {npass}, fail {nfail}]")
        if v == "FAILED":
            m = re.search(r"\b(act|vis|setup)\b", what)
            issues.append({"title": f"signature FAILED: {key} ×{nfail}", "own": m.group(1) if m else owner(calls[k]["name"]),
                           "s": next_think(calls, k) * nfail, "n": nfail, "ev": ev(calls[k])})
    return out + [""], issues


def memory(sd: Path, sid: str, calls: list[dict], events: list[dict], cost: dict) -> list[str]:
    out = ["## Memory / keeper", ""]
    mem = [c for c in calls if c["name"].startswith("memory_") or c["name"] in MEMORY]
    out.append(f"- in play: {len(mem)} memory/notes calls, {sum(c['think'] + c['dur'] for c in mem):.0f} s "
               f"(model + call); " + ", ".join(f"{k} ×{n}" for k, n in
                                             collections.Counter(c["name"] for c in mem).most_common(6)))
    kp = [e for e in events if e.get("kind") == "memory_keeper" and e.get("session") == sid]
    for e in kp:
        bad = e.get("timed_out") or e.get("reverted") or e.get("lint_errors") or e.get("status") != "ok"
        out.append(f"- keeper: {'**' + str(e.get('status')) + '**' if bad else e.get('status')} in "
                   f"{e.get('seconds')} s{' (' + e['model'] + ')' if e.get('model') else ''}; files {len(e.get('files') or [])}; commit "
                   f"{e.get('commit')}; timed_out {e.get('timed_out')}; reverted {e.get('reverted') or '-'}; lint "
                   f"{e.get('lint_errors') or '-'}" + (f"; ${cost['keeper']:.2f}" if cost.get("keeper") else ""))
    if not kp:
        out.append("- keeper: no memory_keeper event for this session")
    for e in events:
        if e.get("kind") == "plan_check" and sid in str(e.get("when", "")):
            out.append(f"- plan check ({e.get('when')}): {e.get('checked')} checked, invalid "
                       f"{e.get('invalid') or '-'}")
    return out + [""]


def runner(rt: Path, sid: str, events: list[dict], lo: float, hi: float) -> tuple[list[str], list[dict]]:
    """Runner events and runner-log lines from the session's start - 5 min to its end + 5 min (log lines that name
    another session are left out)."""
    a, b = lo - 300, hi + 300
    out = ["## Runner: faults, crashes, relaunches", ""]
    issues = []
    for e in events:
        if a <= e.get("t", 0) <= b and e.get("kind") in RUNNER_KINDS:
            detail = {k: v for k, v in e.items() if k not in ("t", "kind")}
            out.append(f"- {hm(e['t'])} **{e['kind']}** {json.dumps(detail, ensure_ascii=False)[:200]}")
            if e["kind"] in ("fault", "game_oom", "recovery_failed", "setup_failed", "agent_hang") and \
                    e.get("session") in (None, sid):
                issues.append({"title": f"runner {e['kind']}: {json.dumps(detail, ensure_ascii=False)[:80]}",
                               "own": "setup", "s": 300.0, "n": 1, "ev": f"runner_events {hm(e['t'])}"})
    seen = set()   # runner.log repeats run-cont.log's lines
    for log in sorted(rt.glob("run*.log")):
        if log.stat().st_mtime < a:
            continue
        with open(log, errors="replace") as f:
            for line in f:
                try:
                    t = time.mktime(time.strptime(line[:19], "%Y-%m-%d %H:%M:%S"))
                except ValueError:
                    continue
                other = {x for x in re.findall(r"\bS\d{3,}\b", line) if x != sid}
                if a <= t <= b and RUNLOG_RX.search(line[20:]) and not other and line.strip() not in seen:
                    seen.add(line.strip())
                    out.append(f"- {log.name}: {line.strip()[11:230]}")
    if len(out) == 2:
        out.append("none")
    return out + [""], issues


def top(issues: list[dict], gap_issues: list[dict]) -> list[str]:
    """TOP N by seconds: refusals, failed steps, hand calls, signature fails, reloaded fights, runner faults, and
    the long gaps grouped by cause."""
    by = collections.defaultdict(list)
    for g in gap_issues:
        by[(g["cause"], g["own"])].append(g)
    for (cause, own), gs in by.items():
        c = gs[0]["c"]
        issues.append({"title": f"gaps > 10 s: {cause} ×{len(gs)}", "own": own, "s": sum(g["s"] for g in gs),
                       "n": len(gs), "ev": ev(c) if c else ""})
    out = [f"## TOP {TOP_N} harness issues (by seconds; owner guess)", ""]
    for i, x in enumerate(sorted(issues, key=lambda x: -x["s"])[:TOP_N], 1):
        out.append(f"{i}. [{x['own']}] **{x['s']:.0f} s**, ×{x['n']}: {x['title']} — {x['ev']}")
    if not issues:
        out.append("none")
    return out + [""]


# ======================================================================== main

def review(run: str, sid: str, sessions: Path, rt: Path) -> str:
    sd = sessions / run
    calls = load(sd, sid)
    if not calls:
        raise SystemExit(f"{sd / sid}: no timed calls (missing .jsonl / .turns.jsonl?)")
    legs = stems(sd, sid)
    res = [stream_result(Path(f"{s}.jsonl")) or {} for s in legs]
    kres = [k for k in (stream_result(Path(f"{s}-keeper.jsonl")) for s in legs) if k]
    cost = {"play": sum(r.get("total_cost_usd") or 0.0 for r in res),
            "keeper": sum(k.get("total_cost_usd") or 0.0 for k in kres) if kres else None}
    events = M.read_jsonl(sd / "runner_events.jsonl")
    stage = rt / "stage_ms.jsonl"
    ctx = {"stage_ms": M.read_jsonl(stage) if stage.exists() else []}

    fsec, fs = fights_section(sessions, run, sid, rt / "mined" / "fights.jsonl")
    gsec, g_issues = gaps(sd, sid, calls)
    rsec, r_issues = refusals(calls)
    ssec, s_issues = step_failures(calls)
    hsec, h_issues = hand(calls)
    wsec, w_issues = watch(calls, ctx)
    usec, u_issues = runner(rt, sid, events, calls[0]["t"], calls[-1]["t_res"])
    out = [f"# Review {run}/{sid}", "",
           f"(tools/logs/review_session.py, {time.strftime('%Y-%m-%d %H:%M')}; call numbers = overlap.load order)", ""]
    out += headline(sd, sid, calls, fs, cost)
    out += top(r_issues + s_issues + h_issues + w_issues + u_issues, g_issues)
    out += gsec + rsec + ssec + hsec + fsec + wsec
    out += memory(sd, sid, calls, events, cost)
    out += usec
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("run", help="playthrough (runtime/sessions/<run>)")
    ap.add_argument("sid", help="session, e.g. S065")
    ap.add_argument("--runtime", type=Path, default=RUNTIME,
                    help="runtime dir: run*.log, stage_ms.jsonl, mined/fights.jsonl (default $BG3AI_RUNTIME, else the main tree's)")
    ap.add_argument("--sessions", type=Path, help="sessions root (default <runtime>/sessions)")
    ap.add_argument("--stdout", action="store_true", help="print, don't write Sxxx.review.md")
    a = ap.parse_args(argv)
    sessions = a.sessions or a.runtime / "sessions"
    text = review(a.run, a.sid, sessions, a.runtime)
    if a.stdout:
        print(text)
    else:
        p = sessions / a.run / f"{a.sid}.review.md"
        p.write_text(text + "\n")
        print(p)
    return 0


if __name__ == "__main__":
    sys.exit(main())

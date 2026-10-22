#!/usr/bin/env python3
"""One series against another (the Nautiloid regression): per-run and per-phase time, unneeded model turns and
refusals, and a PROVED / FAILED / NOT-SEEN verdict for each live signature of the fixes under test.

    tools/vis/series_report.py naut3 --vs naut2          # series names (runtime/sessions/<name>/runs.jsonl)
    tools/vis/series_report.py naut3 --vs naut2 -v       # + every signature hit per run

A series' runs come from its runs.jsonl records (runs.series_records); while a series is still running, the run
dirs that exist (<base>, <base>-r2, …). Calls: overlap.load (bg3harness/triage.py) over each run's play sessions
(model_time.session_files: Sxxx + resumes, not -fix / -keeper); model messages: model_time.model_turns.

(a) phases of a Nautiloid run (first match after the previous phase's end; "-" when not reached):
  CC                calls up to the last create_character, minus its world load (stage_ms world_ms: game time;
                    create_character waits it out since setup part 56)
  first fight       first COMBAT start after CC .. its combat_ended
  helm ladder       first "Nm above you" / climb / Helm refusal after the fight .. the 10 m climb up (dy >= 8)
  transponder dash  first Transponder / o3 mention after the ladder .. Wilderness
  rest              every call outside those (turns and model s only)
  turns = calls with >= TURN_S model time before them; first block = a model message's trigger -> first block.
(b) overlap.unneeded per run, overlap.top_refusals across the runs of both series.
(c) SIGNATURES: per run, any fail hit -> FAILED, else any pass hit -> PROVED, else NOT-SEEN; the series: FAILED if a
  run failed. Each verdict quotes its transcript line (run, session, call #, +s from the session start, tool).
"""
from __future__ import annotations

import argparse
import json
import re
import statistics as st
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "runner"))
sys.path.insert(0, str(REPO / "packages" / "harness" / "src"))
sys.path.insert(0, str(REPO / "bridge" / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import overlap  # noqa: E402
import runs  # noqa: E402
from bg3ai import metrics as M  # noqa: E402
from bg3harness import model_time as MT  # noqa: E402

TURN_S = 0.3          # a call with less model time before it was issued in the same message: not a turn
PHASES = ("CC", "first fight", "helm ladder", "transponder dash")
NAMING_MS = 2500      # setup part 52: naming target
CC_OWN_MS = 5000      # acceptance.CC_MAX_OWN_MS (setup part 57): cc_apply + naming + first look; the load is the game's
OBJECTIVE_MOVED_M = 5.0   # server.OBJECTIVE_MOVED_M: a re-trip farther than this after an arrival = the marker moved
ARRIVAL_READS = {"get_map", "get_status"}
FIRST_MOVES = {"travel_to", "walk_toward", "move_to", "queue_actions", "do_actions", "interact", "route"}


# ======================================================================== runs

def sessions_root() -> Path:
    return overlap.SESSIONS


def series_runs(base: str, root: Path) -> list[tuple[str, dict | None]]:
    """[(playthrough, record or None)] of a series: its records, else the run dirs that exist so far."""
    recs = runs.series_records(root / base / "runs.jsonl", base)
    out = [(r.get("playthrough") or runs.run_name(base, int(r.get("run") or 1)), r) for r in recs]
    seen = {p for p, _ in out}
    k = 1
    while (root / runs.run_name(base, k)).is_dir():
        if runs.run_name(base, k) not in seen:
            out.append((runs.run_name(base, k), None))
        k += 1
    return sorted(out, key=lambda pr: int((pr[1] or {}).get("run") or (pr[0].rsplit("-r", 1)[-1]
                                                                          if "-r" in pr[0] else 1)))


def load_run(d: Path) -> tuple[list[dict], list[dict]]:
    """(calls, model messages) of a run's play sessions in order; each call carries `session` and `k`."""
    calls, msgs = [], []
    for name, stream in MT.session_files(d):
        stem = d / name
        if not (d / f"{name}.turns.jsonl").exists():
            continue
        for c in overlap.load(stem):
            c["session"], c["k"] = name, len(calls)
            c["t0"] = calls[0]["t"] if calls else c["t"]
            calls.append(c)
        info = MT.stream_info(stream)
        msgs += [m for m in MT.model_turns(M.read_jsonl(d / f"{name}.turns.jsonl"), info["think_ms"])
                 if m["trigger"] is not None]
    return calls, msgs


# ======================================================================== (a) phases

def _first(cs: list[dict], pred, start: int = 0) -> int | None:
    return next((k for k in range(max(start, 0), len(cs)) if pred(cs[k])), None)


def _big_climb_up(c: dict) -> bool:
    return any(_climb(ln) == ("up", True) for ln in c["text"].splitlines())


def _climb(line: str) -> tuple[str, bool] | None:
    """('up'|'down', dy >= 8) of a climb result line, else None."""
    m = re.search(r"climbed=(up|down)", line)
    if not m:
        return None
    dy = re.search(r"dy=(-?[\d.]+)", line)
    return m.group(1), bool(dy and abs(float(dy.group(1))) >= 8)


def phases(cs: list[dict]) -> dict[str, tuple[int | None, int | None]]:
    ph = {}
    cc = max((k for k, c in enumerate(cs) if c["name"] == "create_character"), default=None)
    ph["CC"] = (0, cc) if cc is not None else (None, None)
    a = _first(cs, lambda c: "combat_started" in c["text"] or "COMBAT STARTED" in c["text"]
               or "-> COMBAT" in c["text"], (cc or 0) + 1)
    b = _first(cs, lambda c: "combat_ended" in c["text"], a) if a is not None else None
    ph["first fight"] = (a, b)
    h = _first(cs, lambda c: re.search(r"\d+(\.\d+)?m above you", c["text"]) or "climbed=" in c["text"]
               or (c["refusal"] and "Helm" in c["text"]), (b or 0) + 1)
    he = _first(cs, _big_climb_up, h) if h is not None else None
    ph["helm ladder"] = (h, he)
    t = _first(cs, lambda c: "Transponder" in c["text"] or "Transponder" in str(c["input"])
               or re.search(r"['\"]o3['\"]", str(c["input"])), (he or 0) + 1)
    te = _first(cs, lambda c: "Wilderness" in c["text"], t) if t is not None else None
    ph["transponder dash"] = (t, te)
    return ph


def cc_world_s(stage_ms: list[dict], lo: float, hi: float) -> float:
    """Seconds of world load inside create_character calls logged between lo and hi (stage_ms world_ms)."""
    return sum((r.get("world_ms") or 0) / 1000 for r in stage_ms
               if r.get("op") == "create_character" and lo <= (r.get("t") or 0) <= hi)


def phase_rows(cs: list[dict], msgs: list[dict], stage_ms: list[dict] = ()) -> dict[str, dict | None]:
    """{phase: {wall, turns, model, refused, first_block (median)} or None}, plus "rest" (no wall). The CC wall
    leaves out the world load create_character waited for (stage_ms)."""
    out, used = {}, set()
    for name, (a, b) in phases(cs).items():
        if a is None or b is None:
            out[name] = None
            continue
        seg = cs[a:b + 1]
        used.update(range(a, b + 1))
        lo, hi = seg[0]["t"] - seg[0]["think"] - 0.01, seg[-1]["t_res"]
        fb = [m["t_first"] - m["trigger"] for m in msgs if lo <= m["trigger"] <= hi]
        out[name] = dict(wall=hi - lo - (cc_world_s(stage_ms, lo, hi) if name == "CC" else 0), turns=sum(1 for c in seg if c["think"] >= TURN_S),
                         model=sum(c["think"] for c in seg), refused=sum(1 for c in seg if c["refusal"]),
                         first_block=st.median(fb) if fb else None)
    rest = [c for k, c in enumerate(cs) if k not in used]
    out["rest"] = dict(wall=None, turns=sum(1 for c in rest if c["think"] >= TURN_S),
                       model=sum(c["think"] for c in rest), refused=sum(1 for c in rest if c["refusal"]),
                       first_block=None)
    return out


def _f(v, fmt="{:.0f}") -> str:
    return "-" if v is None else fmt.format(v)


def phase_table(per_run: dict[str, dict]) -> list[str]:
    out = [f"  {'run':12} {'phase':17} {'wall s':>7} {'turns':>5} {'model s':>8} {'s/turn':>6} {'refused':>7} "
           f"{'1st blk med':>11}"]
    for run, rows in per_run.items():
        for name, r in rows.items():
            if r is None:
                out.append(f"  {run:12} {name:17} {'-':>7}")
                continue
            out.append(f"  {run:12} {name:17} {_f(r['wall']):>7} {r['turns']:5} {r['model']:8.1f} "
                       f"{r['model'] / max(r['turns'], 1):6.2f} {r['refused']:7} {_f(r['first_block'], '{:.2f}'):>11}")
    return out


def phase_means(series: dict[str, dict[str, dict]]) -> list[str]:
    """Per phase, the series' means side by side (a → b)."""
    names = list(series)
    out = [f"  {'phase':17} {'wall s':>13} {'turns':>13} {'model s':>13} {'s/turn':>13} {'refused':>11} "
           f"{'1st blk med':>13}   ({' → '.join(names)}, means over the runs that reached the phase)"]

    def mean(ser, ph, key):
        v = [rows[ph][key] for rows in series[ser].values() if rows.get(ph) and rows[ph][key] is not None]
        return st.mean(v) if v else None

    for ph in (*PHASES, "rest"):
        cells = []
        for key, fmt, w in (("wall", "{:.0f}", 13), ("turns", "{:.1f}", 13), ("model", "{:.1f}", 13),
                            (None, "{:.2f}", 13), ("refused", "{:.1f}", 11), ("first_block", "{:.2f}", 13)):
            if key is None:
                vals = []
                for s in names:
                    t, m = mean(s, ph, "turns"), mean(s, ph, "model")
                    vals.append(None if not t or m is None else m / t)
            else:
                vals = [mean(s, ph, key) for s in names]
            cells.append(f"{' → '.join(_f(v, fmt) for v in vals):>{w}}")
        out.append(f"  {ph:17} " + " ".join(cells))
    return out


# ======================================================================== (c) signatures

def _line(text: str, pat: str) -> str | None:
    rx = re.compile(pat)
    return next((ln.strip() for ln in text.splitlines() if rx.search(ln)), None)


def _inp(c: dict) -> dict:
    return c["input"] if isinstance(c["input"], dict) else {}


def _actions(c: dict) -> list[dict]:
    a = _inp(c).get("actions")
    if isinstance(a, str):
        try:
            a = json.loads(a)
        except ValueError:
            return []
    return [x for x in a if isinstance(x, dict)] if isinstance(a, list) else []


def climbs(cs: list[dict]) -> list[tuple[int, str | None, str, bool, str]]:
    """[(call k, ladder id, up|down, big, line)] of the agent's climbs: interact results and queue/do steps
    (a step's ladder = that step of the last queue_actions / the do_actions' own list)."""
    out, queued = [], []
    for k, c in enumerate(cs):
        if c["name"] in ("queue_actions", "do_actions"):
            queued = _actions(c)
        for ln in c["text"].splitlines():
            cl = _climb(ln)
            if not cl:
                continue
            m = re.match(r"\s*(\d+)\. interact\b", ln)
            if m:
                i = int(m.group(1)) - 1
                lid = (queued[i].get("target") if i < len(queued) else None)
            elif c["name"] == "interact" and ln.lstrip().startswith("interact"):
                lid = _inp(c).get("target")
            else:
                continue
            out.append((k, lid, cl[0], cl[1], ln.strip()))
    return out


def _arrival(c: dict) -> bool:
    t = c["text"]
    if any(s in t for s in ("STOPPED", "INTERRUPTED", "REFUSED", "early wake")) or c["refusal"]:
        return False
    return "arrived" in t or bool(re.search(r"(walk_toward|move_to): done", t)) or (
        "travel_to objective" in t and bool(re.search(r": done \(\d+ steps?\)", t)))


def _next_game(cs: list[dict], k: int) -> int | None:
    return _first(cs, lambda c: c["name"] not in overlap.INDEP, k + 1)


def sig_who(cs, ctx):
    for k, c in enumerate(cs):
        ln = _line(c["text"], r"\(who: .+, whose turn it is")
        if ln:
            yield "pass", k, ln
        if (c["refusal"] and "not this character's turn" in c["refusal"] and "who" not in _inp(c)
                and c["name"] not in ("combat_turn", "end_turn")):
            yield "fail", k, c["refusal"]


def sig_objective_move(cs, ctx):
    for k, c in enumerate(cs):
        if c["name"] != "combat_turn":
            continue
        if c["refusal"] and "objective marker" in c["refusal"]:
            yield "fail", k, c["refusal"]
        for ln in c["text"].splitlines():
            if re.match(r"\s*move o\d+ \(objective ", ln):
                last = ln.rsplit("→", 1)[-1]
                if "no movement left" in last or "INTERRUPTED" in last:
                    continue   # nothing to walk, or a game event (a conversation) cut a walk toward it: no verdict
                yield ("pass" if re.search(r":? done\b", last) and "FAILED" not in last else "fail"), k, ln.strip()


def sig_partial(cs, ctx):
    for k, c in enumerate(cs):
        if c["name"] != "combat_turn":
            continue
        for ln in c["text"].splitlines():
            m = re.search(r"→ partial [\d.]+m(?: toward it)?: (.*)", ln)
            if m:
                bad = m.group(1).startswith("FAILED") and "beyond movement" in m.group(1)
                yield ("fail" if bad else "pass"), k, ln.strip()


def sig_walk_clip(cs, ctx):
    for k, c in enumerate(cs):
        if c["name"] != "walk_toward":
            continue
        if c["refusal"] and "beyond movement" in c["refusal"]:
            yield "fail", k, c["refusal"]
        ln = _line(c["text"], r"cut to the movement left")
        if ln:
            yield "pass", k, ln


def sig_wait_arrival(cs, ctx):
    for k, c in enumerate(cs):
        if c["name"] != "wait_for":
            continue
        lead = _inp(c).get("lead_s")
        if "early wake" in c["text"] and not (isinstance(lead, (int, float)) and lead > 0):
            yield "fail", k, _line(c["text"], "early wake")
        elif _arrival(c):
            yield "pass", k, _line(c["text"], r"arrived|: done") or c["text"].splitlines()[0]


def _helm(cs):
    a, b = phases(cs)["helm ladder"]
    return (a, b if b is not None else len(cs) - 1) if a is not None else (None, None)


def sig_ladder_once(cs, ctx):
    a, b = _helm(cs)
    if a is None:
        return
    seen = {}
    for k, lid, d, big, ln in climbs(cs):
        if not a <= k <= b:
            continue
        if lid in seen:
            yield "fail", k, f"{lid} climbed again ({d}; first at #{seen[lid]}): {ln}"
        seen.setdefault(lid, k)
        if d == "up" and big:
            yield "pass", k, f"{lid} 10 m up, ladders climbed {list(seen)}: {ln}"


def hinted(cs: list[dict], k: int, lid: str | None) -> tuple[str, str] | None:
    """(up|down, line) the last result before call k said climbing ladder `lid` goes (route's NEXT "interact eN
    (ladder up)", its LADDERS list or a STOPPED hint "eN … (climbs 2.9m up"), else None."""
    if not lid:
        return None
    rx = re.compile(rf"\b{re.escape(lid)}\b[^\n]*?(?:\(ladder (up|down)\)|climbs [\d.]+m (up|down))")
    for c in reversed(cs[:k]):
        m = list(rx.finditer(c["text"]))
        if m:
            return m[-1].group(1) or m[-1].group(2), m[-1].group(0)[:90]
    return None


def sig_climb_chain(cs, ctx):
    """Judged: an interact/travel_to step (the bridge picks the way) right after a climb that went the way the hint
    said. Not judged: after a wrong-way climb the next step is on another floor (helm-hint's failure, naut3 #27), and
    a move_to to the agent's own coordinates may aim at the floor the climb left (naut3-r3 #45)."""
    wrong_way = {(k, ln) for k, lid, d, _, ln in climbs(cs) if (hinted(cs, k, lid) or (d,))[0] != d}
    for k, c in enumerate(cs):
        steps = [ln.strip() for ln in c["text"].splitlines() if re.match(r"\s*\d+\. \w+", ln)]
        for s0, s1 in zip(steps, steps[1:]):
            if "climbed=" not in s0 or (k, s0) in wrong_way or not re.match(r"\d+\. (interact|travel_to)\b", s1):
                continue
            if "no walkable path" in s1 or "FAILED" in s1:
                yield "fail", k, f"{s0} | {s1}"
            elif "STOPPED" not in c["text"]:
                yield "pass", k, f"{s0} | {s1}"
        if c["name"] == "do_actions" and c["refusal"] and "no walkable path" in c["refusal"] and len(steps) == 1 \
                and "climbed=" in steps[0] and (k, steps[0]) not in wrong_way:
            yield "fail", k, c["refusal"]


def sig_ladder_hint(cs, ctx):
    a, b = _helm(cs)
    if a is None:
        return
    first = next(((k, lid, d, ln) for k, lid, d, big, ln in climbs(cs) if a <= k <= b), None)
    if first:
        k, lid, d, ln = first
        h = hinted(cs, k, lid)
        yield ("pass" if d == "up" else "fail"), k, f"first helm climb {d} ({lid}): {ln}" + (
            f"  [hint said {h[0]}: {h[1]}]" if h else "")


def sig_wedged(cs, ctx):
    for k, c in enumerate(cs):
        ln = _line(c["text"], r"don't jump there")
        if ln:
            yield "pass", k, ln
        w = _line(c["text"], r"wedged.*jump_to")
        if w:
            j = _next_game(cs, k)
            if j is not None and cs[j]["name"] == "jump_to":
                again = _first(cs, lambda x: x["refusal"] and "no walkable path" in x["refusal"], j + 1)
                if again is not None and again - j <= 5:
                    yield "fail", k, f"{w} → jump_to → #{again} {cs[again]['name']}: {cs[again]['refusal'][:90]}"


def sig_moved_on(cs, ctx):
    for k, c in enumerate(cs):
        ln = _line(c["text"], r"moved on: now|is gone now")
        if ln:
            yield "pass", k, ln
        if not (_arrival(c) and "travel_to objective" in c["text"]):
            continue
        m = re.search(r"travel_to objective ([^:]+):", c["text"])
        j = _first(cs, lambda x: x["name"] == "travel_to", k + 1)
        if not m or j is None or ln:
            continue
        if any(x["name"] in ("walk_toward", "move_to", "interact", "combat_turn") for x in cs[k + 1:j]):
            continue
        d = re.search(rf"travel_to objective {re.escape(m.group(1))}: started \(([\d.]+)m\)", cs[j]["text"])
        if d and float(d.group(1)) > OBJECTIVE_MOVED_M:
            yield "fail", j, f"arrival #{k} said nothing, then {d.group(0)}"


def sig_arrival_reread(cs, ctx):
    for k, c in enumerate(cs):
        if not _arrival(c):
            continue
        j = _next_game(cs, k)
        if j is None:
            continue
        if cs[j]["name"] in ARRIVAL_READS:
            yield "fail", j, f"{cs[j]['name']} right after the arrival #{k}: {cs[j]['text'].splitlines()[0][:100]}"
        else:
            yield "pass", j, f"{cs[j]['name']} after the arrival #{k}"


def sig_map_objectives(cs, ctx):
    for k, c in enumerate(cs):
        ln = _line(c["text"], r"^No map change \(")
        if not ln:
            continue
        n = re.search(r"(\d+) objectives?", ln)
        if "Here:" in ln:
            yield "pass", k, ln
        elif n and int(n.group(1)) > 0:
            yield "fail", k, ln


def sig_first_look(cs, ctx):
    k = max((i for i, c in enumerate(cs) if c["name"] == "create_character" and not c["refusal"]), default=None)
    if k is None:
        return
    c = cs[k]
    j = _first(cs, lambda x: x["name"] in FIRST_MOVES and not (x["name"] == "route" and not _inp(x).get("go")), k + 1)
    gap = f"first move {cs[j]['name']} #{j} {cs[j]['t'] - c['t_res']:.1f}s after create_character" if j else \
        "no move after create_character"
    reads = [x["name"] for x in cs[k + 1:j] if x["name"] in ("get_status", "look", "save_game", "get_party")]
    where = _line(c["text"].split("FIRST LOOK", 1)[-1], r"^WHERE ")
    if "FIRST LOOK" not in c["text"]:
        yield "fail", k, f"no FIRST LOOK in create_character; {gap}; reads before it: {reads}"
    elif where and ("Character creation" in where or "screen=loading" in where):
        yield "fail", k, f"FIRST LOOK read before the world loaded ({where[:50]}): it shows the CC scene, so the " \
                         f"reads after it were needed; {gap}"
    elif reads:
        yield "fail", k, f"FIRST LOOK present, but {reads} before the first move; {gap}"
    else:
        yield "pass", k, f"{_line(c['text'], 'FIRST LOOK')[:60]}…; {gap}"


def sig_naming(cs, ctx):
    if not cs:
        return
    lo, hi = cs[0]["t"] - 120, cs[-1]["t_res"]
    k = next((i for i, c in enumerate(cs) if c["name"] == "create_character"), 0)
    for r in ctx.get("stage_ms") or []:
        if r.get("op") != "create_character" or not lo <= (r.get("t") or 0) <= hi or "naming_ms" not in r:
            continue
        ok = r["naming_ms"] <= NAMING_MS and r.get("typed", "over_selection") == "over_selection"
        typed = r.get("typed", "not logged")   # server._log_cc_timing drops naming's `typed` (setup): time only
        yield ("pass" if ok else "fail"), k, (f"stage_ms naming_ms={r['naming_ms']} typed={typed} "
                                              f"popup={(r.get('naming_stages') or {}).get('popup')} "
                                              f"type={(r.get('naming_stages') or {}).get('type')}")


def sig_cc_time(cs, ctx):
    """create_character's own phases (stage_ms: apply + naming + first look) within CC_OWN_MS; the world load is
    the game's, quoted, not judged."""
    if not cs:
        return
    lo, hi = cs[0]["t"] - 120, cs[-1]["t_res"]
    k = next((i for i, c in enumerate(cs) if c["name"] == "create_character"), 0)
    for r in ctx.get("stage_ms") or []:
        if (r.get("op") != "create_character" or not r.get("ok") or not lo <= (r.get("t") or 0) <= hi
                or r.get("apply_ms") is None or r.get("naming_ms") is None):
            continue
        own = r["apply_ms"] + r["naming_ms"] + (r.get("first_look_ms") or 0)
        yield ("pass" if own <= CC_OWN_MS else "fail"), k, (
            f"stage_ms ours {own} ms (apply {r['apply_ms']} + naming {r['naming_ms']} + first look "
            f"{r.get('first_look_ms', '-')}), world load {r.get('world_ms')} ms (game), total {r.get('total_ms')} ms")


SIGNATURES = (   # (key, what: the fix and its owner, check)
    ("who-acting", "ACT-P2 (act): a fight tool without who acts for the member whose turn it is", sig_who),
    ("move-oN", "ACT-P1 (act): combat_turn move \"oN\" walks toward the objective marker", sig_objective_move),
    ("partial", "act: combat_turn partial walk never ends FAILED beyond movement (shrink retry)", sig_partial),
    ("walk-clip", "ACT-P3 (act): walk_toward in a fight cut to the movement left", sig_walk_clip),
    ("wait-arrival", "ACT-P4 (act): wait_for on a walk returns at the arrival (no early wake unasked)",
     sig_wait_arrival),
    ("helm-once", "act/nav: helm, no ladder climbed twice; the 10 m ladder climbed up", sig_ladder_once),
    ("helm-chain", "act/nav: a step queued after a climb runs (climb settle + path-cache y key)", sig_climb_chain),
    ("helm-hint", "act: the helm's first climb goes up (refusal / route NEXT name the up ladder first)",
     sig_ladder_hint),
    ("wedged", "act/nav: a wedged hint never sends the actor to a lower floor", sig_wedged),
    ("moved-on", "vis: travel_to(objective) arrival says when the marker moved on", sig_moved_on),
    ("arrival-reread", "vis playbook: no get_map/get_status right after an arrival", sig_arrival_reread),
    ("map-objectives", "vis: get_map's no-change answer names the area's objectives", sig_map_objectives),
    ("cc-first-look", "setup: create_character carries the FIRST LOOK; no read before the first move",
     sig_first_look),
    ("cc-naming", "setup: naming <= 2.5 s, typed over the selection (runtime/stage_ms.jsonl)", sig_naming),
    ("cc-time", "setup: create_character's own phases (apply + naming + first look) <= 5 s; the world load is "
     "the game's (runtime/stage_ms.jsonl)", sig_cc_time),
)


def verdict(cs: list[dict], check, ctx: dict) -> tuple[str, tuple | None, int, int]:
    """(PROVED|FAILED|NOT-SEEN, evidence (k, line) of the first fail else the first pass, passes, fails)."""
    hits = list(check(cs, ctx))
    fails = [h for h in hits if h[0] == "fail"]
    passes = [h for h in hits if h[0] == "pass"]
    if fails:
        return "FAILED", fails[0][1:], len(passes), len(fails)
    if passes:
        return "PROVED", passes[0][1:], len(passes), 0
    return "NOT-SEEN", None, 0, 0


def evidence(run: str, cs: list[dict], ev: tuple) -> str:
    k, line = ev
    c = cs[k]
    return f"{run}/{c['session']} #{k} +{c['t'] - c['t0']:.0f}s {c['name']}: {(line or '')[:220]}"


def signature_lines(series: dict[str, dict[str, list]], ctx: dict, verbose: bool = False) -> list[str]:
    """One verdict per signature for the first series (the one under test), the others' verdicts beside it."""
    names = list(series)
    out = []
    for key, what, check in SIGNATURES:
        per = {s: {run: verdict(cs, check, ctx) for run, cs in series[s].items()} for s in names}
        agg = {}
        for s in names:
            vs = [v[0] for v in per[s].values()]
            agg[s] = "FAILED" if "FAILED" in vs else "PROVED" if "PROVED" in vs else "NOT-SEEN"
        head = names[0]
        runs_cell = ", ".join(f"{run}:{v[0]}" for run, v in per[head].items())
        others = "; ".join(f"{s}: {agg[s]}" for s in names[1:])
        out.append(f"{agg[head]:8} {key:15} {what}")
        out.append(f"         runs: {runs_cell}" + (f"   (was {others})" if others else ""))
        shown = [(run, v) for run, v in per[head].items() if v[1] and v[0] == agg[head]][:1]
        if verbose:
            shown = [(run, v) for run, v in per[head].items() if v[1]]
        for run, v in shown:
            out.append(f"         {v[0].lower()}: {evidence(run, series[head][run], v[1])}  "
                       f"[pass {v[2]}, fail {v[3]}]")
    return out


# ======================================================================== main

def report(base: str, vs: str | None, root: Path, verbose: bool = False) -> str:
    stage_path = root.parent / "stage_ms.jsonl"
    ctx = {"stage_ms": M.read_jsonl(stage_path) if stage_path.exists() else []}
    names = [base] + ([vs] if vs else [])
    recs, calls, prows, un = {}, {}, {}, {}
    for s in names:
        rr = series_runs(s, root)
        recs[s] = [r for _, r in rr if r]
        calls[s], prows[s] = {}, {}
        for run, _ in rr:
            cs, msgs = load_run(root / run)
            calls[s][run] = cs
            prows[s][run] = phase_rows(cs, msgs, ctx["stage_ms"])
            un[run] = overlap.unneeded(cs)
    out = [f"SERIES {base}" + (f" vs {vs}" if vs else "")]
    for s in names:
        out.append(f"  {s}: {len(calls[s])} run(s) ({', '.join(calls[s]) or 'none'}), {len(recs[s])} run record(s)")
    if vs and recs[base] and recs[vs]:
        out += ["", runs.series_compare(vs, recs[vs], base, recs[base])]
    out += ["", "(a) PHASES per run"]
    for s in names:
        out += phase_table(prows[s])
    out += ["", "    phase means"] + phase_means({s: prows[s] for s in reversed(names)})
    out += ["", "(b) UNNEEDED MODEL TURNS per run (overlap.py categories)"]
    for s in names:
        for run in calls[s]:
            out.append(f"  {run:12} " + overlap.unneeded_summary(un[run]))
    out += overlap.top_refusals({run: un[run] for s in names for run in calls[s]})
    out += ["", f"(c) LIVE SIGNATURES ({base}" + (f"; was = {vs})" if vs else ")")]
    out += signature_lines({s: calls[s] for s in names}, ctx, verbose)
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("series", help="series base name (runtime/sessions/<name>)")
    ap.add_argument("--vs", help="the series to compare with (e.g. naut2)")
    ap.add_argument("--sessions", type=Path, help="sessions root (default <$BG3AI_RUNTIME, else the main tree's runtime>/sessions)")
    ap.add_argument("-v", "--verbose", action="store_true", help="every run's signature evidence")
    a = ap.parse_args(argv)
    print(report(a.series, a.vs, a.sessions or sessions_root(), a.verbose))
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""One arena match, turn by turn and side by side, from its run folder (the method: ops/logs/arena-fight-analysis.md in
the website repo). Reads what the runner wrote and nothing else; plays nothing, calls no model.

    uv run python tools/dev/arena_fight.py <run dir> [--json out.json] [--turn-cap S]
    uv run python tools/dev/arena_fight.py --openers <run dir> [<run dir> ...] [--json out.json]

The run dir (bg3harness.arena.live) holds facts.jsonl (the referee's arena_* events, epoch `t`), A.events.jsonl and
B.events.jsonl (each side's client stream: tool_call / tool_result paired by call_id, text, thinking, usage, result),
rolls.jsonl (d20 rows) and arena.json (the manifest: players, costs, invalid). Every side call is put in the phase open
at its time (the last arena_turn at or before it), so a call is the side's own phase's or the other side's. All
printed times ("@") are on one clock: seconds since the run_start mark (facts' game_t), never since the first turn.

Printed (markdown), per phase: who acted, the side's calls with latency and the model time between them, refusals,
actions sent in the other side's phase, forced/passed/stalled turns; then per side: tool latency (median, p90), calls,
refusals, repeated reads, model time per own phase against the cap; the view check (a character's status words one
side's combat-state read shows and the other's doesn't, in the same phase: to be judged by hand, an own side may
rightly see more); the leak scan (the other side's session id, or a 40+ character piece of its own text/thinking, in a
side's tool results); rolls per side; the end and the costs. Heuristics are named as such in the output.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import re
import statistics
import sys
from pathlib import Path
from typing import Any

from bg3harness.triage import failed_lines

SIDES = ("A", "B")
# Calls that only read or wait. Anything else is an action (it changes the game or ends a turn).
READS = frozenset(
    {
        "get_combat_state",
        "get_character",
        "look",
        "get_layout",
        "look_at",
        "get_nearby",
        "get_actions",
        "ability_preview",
        "wait_for",
        "get_state",
        "get_party",
        "get_inventory",
        "get_spells",
        "get_map",
        "describe",
        "inspect",
        "get_events",
        "status",
        "get_status",
        "path_preview",
        "route",
    }
)
STATUS_WORDS = (
    "downed",
    "dead",
    "ended",
    "current",
    "can_act",
    "sleeping",
    "prone",
    "hidden",
    "invisible",
    "raging",
    "blinded",
    "frightened",
    "IN_COMBAT",
)
LEAK_MIN = 40
# The once-per-match openers counted per first turn (the seat sims' OFF_BALANCED and DAZED terms): a cast's spell
# name, as the bridge's `cast=` shows it, by prefix. Any other cast is "other".
OPENERS = (("Rush", "Rush_"), ("Opening Attack", "Target_OpeningAttack"), ("Pommel Strike", "Target_PommelStrike"))
CAST = re.compile(r"\bcast=(\S+)")


def rows(path: Path) -> list[dict[str, Any]]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    out = []
    for line in text.splitlines():
        if line.strip():
            try:
                out.append(json.loads(line))
            except ValueError:
                continue  # a torn last line
    return out


def calls(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The side's tool calls in order: {t, tool, args, result_t, ms, text, error, think_s} (think_s: model time from
    the previous result, or the stream's start, to this call)."""
    res = {e.get("call_id"): e for e in events if e.get("kind") == "tool_result"}
    out, last = [], next((e["t"] for e in events if e.get("t") is not None), None)
    for e in events:
        if e.get("kind") != "tool_call":
            continue
        r = res.get(e.get("call_id")) or {}
        rt = r.get("t")
        text = str(r.get("text") or "")
        tool = e.get("tool") or "?"
        # the bridge's own failure rule (the evidence video's too): its markers and failed steps anywhere in the
        # result, EVENTS blocks (another member's background failures) and failed ability checks not counted
        failed = failed_lines(tool, text)
        out.append(
            {
                "t": e["t"],
                "tool": tool,
                "args": e.get("args") or {},
                "result_t": rt,
                "ms": None if rt is None else round((rt - e["t"]) * 1000),
                "text": text,
                "error": bool(r.get("is_error")) or bool(failed),
                "failed": failed,
                "think_s": None if last is None else round(e["t"] - last, 1),
            }
        )
        last = rt if rt is not None else e["t"]
    return out


def phases(facts: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """The referee's phases in order, each {t, end_t, side, round, phase, members, forced, passed, stall}, and the
    forced, passed and stall records that came before the first phase ({forced, passed, stall})."""
    ev = [f for f in facts if f.get("kind") == "event" and str(f.get("ev", "")).startswith("arena_")]
    end_t = next((f["t"] for f in ev if f["ev"] == "arena_end"), None)
    out: list[dict[str, Any]] = []
    before: dict[str, Any] = {"forced": [], "passed": [], "stall": None}
    for f in ev:
        if f["ev"] == "arena_turn":
            members = f.get("members") or [f.get("character")]
            out.append(
                {
                    "t": f["t"],
                    "side": f.get("side"),
                    "round": f.get("round"),
                    "phase": f.get("phase"),
                    "members": members,
                    "forced": [],
                    "passed": [],
                    "stall": None,
                }
            )
        elif f["ev"] == "arena_turn_forced":
            (out[-1] if out else before)["forced"].append(f.get("character"))
        elif f["ev"] == "arena_turn_passed":
            (out[-1] if out else before)["passed"].append(f"{f.get('character')} ({f.get('why')})")
        elif f["ev"] == "arena_stall":
            (out[-1] if out else before)["stall"] = f.get("stall") or "?"
    for i, p in enumerate(out):
        p["end_t"] = out[i + 1]["t"] if i + 1 < len(out) else end_t
    return out, before


def phase_of(ps: list[dict[str, Any]], t: float) -> int | None:
    idx = None
    for i, p in enumerate(ps):
        if p["t"] <= t:
            idx = i
    return idx


def _pct(xs: list[float], q: float) -> float | None:
    if not xs:
        return None
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(q * len(xs)))]


def view_words(cs: list[dict[str, Any]], names: list[str]) -> dict[str, set[str]]:
    """Heuristic: the status words a side's get_combat_state results show on the lines naming each character."""
    out: dict[str, set[str]] = {n: set() for n in names}
    for c in cs:
        if c["tool"] != "get_combat_state":
            continue
        for line in c["text"].splitlines():
            for n in names:
                if re.search(rf"\b{re.escape(n)}\b", line):
                    out[n] |= {w for w in STATUS_WORDS if re.search(rf"\b{w}\b", line)}
    return out


def pieces(events: list[dict[str, Any]]) -> list[str]:
    """A side's own words (its text and thinking), cut into LEAK_MIN-character pieces to look for elsewhere."""
    out = []
    for e in events:
        if e.get("kind") in ("text", "thinking"):
            s = re.sub(r"\s+", " ", str(e.get("text") or "")).strip()
            out += [s[i : i + LEAK_MIN] for i in range(0, max(0, len(s) - LEAK_MIN + 1), LEAK_MIN)]
    return [p for p in out if len(p) == LEAK_MIN]


def own_casts(text: str) -> list[str]:
    """The spells a result says its own call cast (`cast=`), outside its EVENTS blocks (the header and the indented
    lines under it: what else happened meanwhile, not this call's)."""
    out, in_events = [], False
    for raw in text.splitlines():
        if raw.strip() == "EVENTS":
            in_events = True
        elif not raw.startswith("  "):
            in_events = False
        if not in_events:
            out += CAST.findall(raw)
    return out


def opener_of(spell: str) -> str:
    return next((name for name, prefix in OPENERS if spell.startswith(prefix)), "other")


def handles(cs: list[dict[str, Any]], names: list[str]) -> dict[str, str]:
    """A side's entity handles for its own members ("e1" -> "Ash"), read from its results' "e1 Ash" pairs: a call's
    `who` may be the handle (seat run r12) or the name. The pairing seen most wins."""
    if not names:
        return {}
    pair = re.compile(rf"\b(e\d+) ({'|'.join(map(re.escape, names))})\b")
    seen: dict[tuple[str, str], int] = {}
    for c in cs:
        for h, n in pair.findall(c["text"]):
            seen[h, n] = seen.get((h, n), 0) + 1
    out: dict[str, str] = {}
    for (h, n), _ in sorted(seen.items(), key=lambda x: x[1]):
        out[h] = n
    return out


def openers(
    ps: list[dict[str, Any]], cs: dict[str, list[dict[str, Any]]], teams: dict[str, list[str]]
) -> dict[str, Any]:
    """Each character's first turn (the first phase naming it as a member) and what it cast there: the casts of its
    side's calls in that phase made by it (`who` as a name or the side's handle for it, or the phase's only member
    when a call names nobody), refused calls cast nothing. `use` is the openers cast in that turn, else ["other"]
    (casts, no opener) or ["none"]. `all` lists every opener cast in the match as (round, character), any turn;
    `no_turn` the team members never named in a phase (downed before their first turn: the referee passed their
    slot). `unknown`: the calls with casts whose `who` names no member of their phase (none given in a phase of
    several members, or a handle the side's results never paired), as {side, round, phase, casts}; their openers
    are in `all` as character "unknown", and no first turn counts them (REVIEW-18 §6a)."""
    first: list[dict[str, Any]] = []
    seen: set[str] = set()
    every: dict[str, list[list[Any]]] = {name: [] for name, _ in OPENERS}
    unknown: list[dict[str, Any]] = []
    hs = {s: handles(cs.get(s, []), teams.get(s, [])) for s in SIDES}
    for i, p in enumerate(ps):
        mine = [c for c in cs.get(p["side"], []) if c["phase"] == i and not c["error"]]
        only = p["members"][0] if len(p["members"]) == 1 else None
        for c in mine:
            who = str(c["args"].get("who") or only)
            if hs.get(p["side"], {}).get(who, who) not in p["members"] and (sp := own_casts(c["text"])):
                unknown.append({"side": p["side"], "round": p["round"], "phase": p["phase"], "casts": sp})
                for kind in map(opener_of, sp):
                    if kind != "other":
                        every[kind].append([p["round"], "unknown"])
        for m in p["members"]:
            spells = [
                sp
                for c in mine
                if hs.get(p["side"], {}).get(who := str(c["args"].get("who") or only), who) == m
                for sp in own_casts(c["text"])
            ]
            for sp in spells:
                if (kind := opener_of(sp)) != "other":
                    every[kind].append([p["round"], m])
            if m in seen:
                continue
            seen.add(m)
            used = list(dict.fromkeys(k for k in map(opener_of, spells) if k != "other"))
            first.append(
                {
                    "side": p["side"],
                    "character": m,
                    "round": p["round"],
                    "phase": p["phase"],
                    "passed": any(x.split(" (")[0] == m for x in p["passed"]),
                    "casts": spells,
                    "use": used or (["other"] if spells else ["none"]),
                }
            )
    no_turn = [[s, n] for s in SIDES for n in teams.get(s, []) if n not in seen]
    return {"first_turns": first, "all": every, "no_turn": no_turn, "unknown": unknown}


def analyse(run: Path, turn_cap: float | None = None) -> dict[str, Any]:
    facts = rows(run / "facts.jsonl")
    manifest: dict[str, Any] = {}
    with contextlib.suppress(OSError, ValueError):
        manifest = json.loads((run / "arena.json").read_text(encoding="utf-8"))
    start = next((f for f in facts if f.get("ev") == "arena_start"), {})
    end = next((f for f in facts if f.get("ev") == "arena_end"), {})
    teams = start.get("teams") or {}
    ps, before = phases(facts)
    t0 = next((f["t"] for f in facts if f.get("mark") == "run_start"), facts[0]["t"] if facts else 0.0)
    ev = {s: rows(run / f"{s}.events.jsonl") for s in SIDES}
    cs = {s: calls(ev[s]) for s in SIDES}
    sid = {s: next((e.get("session_id") for e in ev[s] if e.get("session_id")), None) for s in SIDES}
    # live.play's arena.json names the arena (a string); the turn cap is then --turn-cap's
    arena_rec = manifest.get("arena")
    cap = turn_cap if turn_cap is not None else arena_rec.get("turn_cap_s") if isinstance(arena_rec, dict) else None
    for s in SIDES:
        for c in cs[s]:
            i = phase_of(ps, c["t"])
            c["phase"] = i
            c["own"] = i is not None and ps[i]["side"] == s
            c["action"] = c["tool"] not in READS
            c["at"] = round(c["t"] - t0, 1)
    report: dict[str, Any] = {
        "run": str(run),
        "arena": start.get("arena"),
        "sides": start.get("sides"),
        "before_first_phase": before,
        "phases": [],
        "per_side": {},
        "views": [],
        "leaks": [],
        "end": end,
    }
    for i, p in enumerate(ps):
        row = {k: p[k] for k in ("side", "round", "phase", "members", "forced", "passed", "stall")}
        row["at"] = round(p["t"] - t0, 1)
        row["secs"] = None if p["end_t"] is None else round(p["end_t"] - p["t"], 1)
        row["calls"] = {s: [c for c in cs[s] if c["phase"] == i] for s in SIDES}
        mine = row["calls"].get(p["side"]) or []
        row["model_s"] = round(sum(c["think_s"] or 0 for c in mine), 1)
        row["over_cap"] = cap is not None and row["model_s"] > cap * len(p["members"])
        row["off_turn_actions"] = [c for s in SIDES if s != p["side"] for c in row["calls"][s] if c["action"]]
        names = [*teams.get("A", []), *teams.get("B", [])]
        va, vb = (view_words(row["calls"][s], names) for s in SIDES)
        for n in names:
            if va[n] != vb[n] and (va[n] or vb[n]):
                report["views"].append(
                    {
                        "round": p["round"],
                        "phase": p["phase"],
                        "character": n,
                        "A_only": sorted(va[n] - vb[n]),
                        "B_only": sorted(vb[n] - va[n]),
                    }
                )
        report["phases"].append(row)
    for s in SIDES:
        other = "B" if s == "A" else "A"
        text = "\n".join(c["text"] for c in cs[s])
        osid = sid[other]
        if osid and osid in text:
            report["leaks"].append({"side": s, "what": f"the other side's session id {osid}"})
        for piece in pieces(ev[other]):
            if piece in re.sub(r"\s+", " ", text):
                report["leaks"].append({"side": s, "what": f"the other side's own words: {piece!r}"})
                break
        ms = [c["ms"] for c in cs[s] if c["ms"] is not None]
        by: dict[str, list[int]] = {}
        for c in cs[s]:
            if c["ms"] is not None:
                by.setdefault(c["tool"], []).append(c["ms"])
        seen: dict[int, set[str]] = {}
        repeats = 0
        for c in cs[s]:
            key = c["tool"] + json.dumps(c["args"], sort_keys=True)
            if c["action"]:
                seen.pop(c["phase"], None)  # a read after an action may see something new
            elif key in seen.setdefault(c["phase"], set()):
                repeats += 1
            else:
                seen[c["phase"]].add(key)
        report["per_side"][s] = {
            "player": (start.get("sides") or {}).get(s),
            "calls": len(cs[s]),
            "actions": sum(1 for c in cs[s] if c["action"]),
            "refusals": [
                {"tool": c["tool"], "text": (c["failed"][0] if c["failed"] else c["text"])[:160]}
                for c in cs[s]
                if c["error"]
            ],
            "off_turn_actions": sum(1 for c in cs[s] if c["action"] and not c["own"]),
            "repeated_reads": repeats,
            "ms_median": statistics.median(ms) if ms else None,
            "ms_p90": _pct(ms, 0.9),
            "tool_ms_median": {k: statistics.median(v) for k, v in sorted(by.items())},
            "cost": (manifest.get("costs") or {}).get(s, ((manifest.get("sides") or {}).get(s) or {}).get("cost_usd")),
        }
    uu = start.get("uuids") or {}
    rl = [r for r in rows(run / "rolls.jsonl") if r.get("natural") is not None]
    report["rolls"] = {
        s: [
            r["natural"]
            for r in rl
            if r.get("roller") in (uu.get(s) or ()) and not r.get("advantage") and not r.get("disadvantage")
        ]
        for s in SIDES
    }
    # attack rolls: the cast record's cast_roll (natural, total, dc = the target's AC, advantage), live since window 6
    report["attacks"] = {
        s: [
            {
                "spell": r.get("spell"),
                "natural": cr.get("natural"),
                "total": cr.get("total"),
                "ac": cr.get("dc"),
                "adv": bool(cr.get("advantage")),
                "dis": bool(cr.get("disadvantage")),
                "hit": cr.get("critical") == "Success"
                or (cr.get("natural") != 1 and None not in (cr.get("total"), cr.get("dc")) and cr["total"] >= cr["dc"]),
            }
            for r in rows(run / "rolls.jsonl")
            if (cr := r.get("cast_roll")) and r.get("roller") in (uu.get(s) or ())
        ]
        for s in SIDES
    }
    report["rolls_other"] = sum(1 for r in rl if not any(r.get("roller") in (uu.get(s) or ()) for s in SIDES))
    report["invalid"] = manifest.get("invalid")
    report["openers"] = openers(ps, cs, teams)
    return report


def _call_line(c: dict[str, Any]) -> str:
    args = json.dumps(c["args"], separators=(",", ":"))[:80]
    flag = " **REFUSED**" if c["error"] else ""
    # a failed result shows its first failed line (a batch's later step), else its first line
    first = c["failed"][0] if c["failed"] else c["text"].strip().splitlines()[0] if c["text"].strip() else ""
    first = first[:100]
    return f"    - @{c['at']} s +{c['think_s']}s `{c['tool']}` {args} -> {c['ms']} ms{flag}: {first}"


def markdown(r: dict[str, Any]) -> str:
    out = [f"# {r['run']}", f"arena {r['arena']}, sides {r['sides']}, invalid {r['invalid']}", "", "## Phases"]
    out.append("(@ = seconds since the run_start mark, one clock for phases and calls)")
    b = r["before_first_phase"]
    if b["forced"] or b["passed"] or b["stall"]:
        out.append(f"- before the first phase: forced {b['forced']}, passed {b['passed']}, stall {b['stall']}")
    for p in r["phases"]:
        out.append(
            f"- r{p['round']} p{p['phase']} {p['side']} {p['members']} @{p['at']} s, {p['secs']} s, "
            f"model {p['model_s']} s"
            + (" **over cap**" if p["over_cap"] else "")
            + (f", forced {p['forced']}" if p["forced"] else "")
            + (f", passed {p['passed']}" if p["passed"] else "")
            + (f", **stall {p['stall']}**" if p["stall"] else "")
        )
        for s in SIDES:
            if p["calls"][s]:
                out.append(f"  - {s} ({'own' if s == p['side'] else 'other side'}'s phase)")
                out += [_call_line(c) for c in p["calls"][s]]
    out += ["", "## Per side"]
    for s, d in r["per_side"].items():
        out.append(
            f"- {s} {d['player']}: {d['calls']} calls ({d['actions']} actions), {len(d['refusals'])} refused, "
            f"{d['off_turn_actions']} actions in the other side's phase, {d['repeated_reads']} repeated reads, "
            f"ms median {d['ms_median']} p90 {d['ms_p90']}, cost {d['cost']}"
        )
        out.append(f"  - tool ms medians: {d['tool_ms_median']}")
        out += [f"  - refused `{x['tool']}`: {x['text']}" for x in d["refusals"]]
    out += ["", "## View check (heuristic: status words on one side's combat-state lines only; judge by hand)"]
    out += [
        f"- r{v['round']} p{v['phase']} {v['character']}: A only {v['A_only']}, B only {v['B_only']}"
        for v in r["views"]
    ] or ["- none"]
    out += ["", "## Leak scan (heuristic)"] + ([f"- side {x['side']}: {x['what']}" for x in r["leaks"]] or ["- none"])
    out += ["", "## Openers (each character's first turn: its casts; Rush / Opening Attack / Pommel Strike / other)"]
    for f in r["openers"]["first_turns"]:
        out.append(
            f"- r{f['round']} p{f['phase']} {f['side']} {f['character']}: {', '.join(f['use'])}"
            + (" (passed)" if f["passed"] else "")
            + f"; casts {f['casts']}"
        )
    out.append(f"- every opener cast, any turn (round, character): {r['openers']['all']}")
    out.append(f"- no turn (downed first): {r['openers']['no_turn'] or 'none'}")
    unk = r["openers"]["unknown"]
    out += [f"- unknown caster: r{u['round']} p{u['phase']} {u['side']}: casts {u['casts']}" for u in unk]
    rl = r["rolls"]
    out += ["", "## Attacks (cast_roll: natural/total vs AC, a = advantage, d = disadvantage)"]
    for s in SIDES:
        at = r["attacks"][s]
        cells = [
            f"{x['spell']} {x['natural']}{'a' if x['adv'] else ''}{'d' if x['dis'] else ''}/{x['total']} v {x['ac']}"
            + (" hit" if x["hit"] else " miss")
            for x in at
        ]
        out.append(f"- {s}: {sum(x['hit'] for x in at)} hits in {len(at)}: " + "; ".join(cells))
    out += ["", f"## Rolls: A {rl['A']}, B {rl['B']}, neither side's {r['rolls_other']}", "", f"## End: {r['end']}"]
    return "\n".join(out) + "\n"


def opener_tally(reports: list[dict[str, Any]]) -> str:
    """Markdown: every run's first turns as rows, then per category the first turns that used it (a turn with two
    openers counts in both), and every opener cast by round over all turns."""
    out = [
        "| run | round | phase | side | character | use | casts |",
        "|---|---|---|---|---|---|---|",
    ]
    names = [n for n, _ in OPENERS]
    turns = 0
    no_turn = 0
    unknown = 0
    count = dict.fromkeys([*names, "other", "none"], 0)
    by_round: dict[str, dict[Any, int]] = {n: {} for n in names}
    for r in reports:
        run = Path(r["run"]).name
        for f in r["openers"]["first_turns"]:
            turns += 1
            for u in f["use"]:
                count[u] += 1
            use = ", ".join(f["use"]) + (" (passed)" if f["passed"] else "")
            out.append(
                f"| {run} | {f['round']} | {f['phase']} | {f['side']} | {f['character']} | {use} | "
                f"{' '.join(f['casts'])} |"
            )
        no_turn += len(r["openers"]["no_turn"])
        unknown += len(r["openers"]["unknown"])
        for n in names:
            for rnd, _ in r["openers"]["all"][n]:
                by_round[n][rnd] = by_round[n].get(rnd, 0) + 1
    out += ["", f"First turns: {turns} in {len(reports)} runs; {no_turn} characters downed before their first turn."]
    out += [f"- {k}: {v}/{turns}" for k, v in count.items()]
    out.append(f"- calls with casts and no known caster (not in the first turns above): {unknown}")
    out += ["", "Every opener cast, any turn, by round:"]
    out += [f"- {n}: {sum(by_round[n].values())} ({dict(sorted(by_round[n].items()))})" for n in names]
    return "\n".join(out) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="One arena match, turn by turn and side by side, from its run folder.")
    ap.add_argument("run", type=Path, nargs="+", help="a run folder; several only with --openers")
    ap.add_argument("--json", type=Path)
    ap.add_argument("--turn-cap", type=float)
    ap.add_argument("--openers", action="store_true", help="only the first-turn opener tally, over every run given")
    a = ap.parse_args(argv)
    if len(a.run) > 1 and not a.openers:
        ap.error("several run folders need --openers")
    for run in a.run:
        if not (run / "facts.jsonl").is_file():
            print(f"arena_fight: no facts.jsonl in {run}", file=sys.stderr)
            return 2
    reports = [analyse(run, a.turn_cap) for run in a.run]
    if a.json:
        out: Any = reports if a.openers else reports[0]
        a.json.write_text(json.dumps(out, indent=1, default=str) + "\n", encoding="utf-8")
    sys.stdout.write(opener_tally(reports) if a.openers else markdown(reports[0]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

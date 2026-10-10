"""Post-session checks: the 'Needs a live test' items (research/live-todo.md) that a finished session's own files
can answer. Reads files only (no game, no bridge): safe at any time, including during the next session.

    python3 tools/dev/check_session.py [--run runtime/sessions/claude-run-2] [--session S015] [-v]

Default: the newest finished session (one with Sxxx.latency.json) of the newest run directory. Inputs:
Sxxx.latency.json, Sxxx.jsonl (the claude transcript), Sxxx.turns.jsonl, Sxxx.metrics.jsonl, runtime/actions.jsonl
(save_game.bg records in the session's window), runtime/runner.log (the session's lines) and the bg3le log of the
game the session played on. Prints `PASS|WARN|FAIL|INFO name: detail` lines (check_all's `session` row parses
them) and exits 1 on any FAIL, 2 when there is no finished session. A check whose input is missing says INFO.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
RUNTIME = Path(os.environ.get("BG3AI_RUNTIME", REPO / "runtime"))

# Verdict bands (live-todo.md says where each number comes from).
TOOLSEARCH_OK = 2            # act: ToolSearch preload, ~0-2 per session
NOOP_SLOW_MS = 1500.0        # act: a no-op wait_for returns in ~0.1-0.5 s (+0.5 s grace after a job)
CONNS_WARN, CONNS_FAIL = 40, 100   # act/se: bg3le debug clients should stay in the low tens
LEAK_PER_LOAD_WARN = 50      # se: patch 0005 parks old-generation releases; only a View replacement leaks
SAVE_MENU_WARN_S = 3.0       # se: background save, menu_s ~2 s: ~1.2 s of menus + the game's ~1 s Save state (frozen)
OPS_MS = {"nav.reach_toward": 30.0, "nav.frontier": 40.0, "nav.hazards": 10.0}  # vis: game-side read cost
FIRST_ACTION_S = 60.0        # setup: session start ≤ 1 min to the first game action
CLUTTER_RE = re.compile(r"^\s*\+ .*\b(Candle|Tin Cup|Stool|Bottle|Plate|Bowl|Cup|Mug)\b", re.M)
STAT_BLOCK_RE = re.compile(r"after=\{hp=")
TAG_RE = re.compile(r"← \w[\w ]* check:")
RUNNER_TS = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) runner: (.*)$")


@dataclass
class Session:
    run: Path
    name: str
    lat: dict = field(default_factory=dict)
    uses: list = field(default_factory=list)      # (name, input, result text, is_error) in order
    turns: list = field(default_factory=list)
    metrics: list = field(default_factory=list)

    def path(self, suffix: str) -> Path:
        return self.run / f"{self.name}{suffix}"


def read_jsonl(p: Path) -> list[dict]:
    out = []
    try:
        with open(p, errors="replace") as f:
            for line in f:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    pass
    except OSError:
        pass
    return out


def result_text(c) -> str:
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return "".join(x.get("text", "") for x in c if isinstance(x, dict))
    return ""


def tool_uses(transcript: list[dict]) -> list[tuple[str, dict, str, bool]]:
    """(tool name, input, result text, is_error) per tool call, in call order, from a stream-json transcript."""
    order, by_id = [], {}
    for r in transcript:
        content = (r.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue
        for b in content:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "tool_use":
                by_id[b.get("id")] = [b.get("name", ""), b.get("input") or {}, "", False]
                order.append(b.get("id"))
            elif b.get("type") == "tool_result" and b.get("tool_use_id") in by_id:
                rec = by_id[b["tool_use_id"]]
                rec[2], rec[3] = result_text(b.get("content")), bool(b.get("is_error"))
    return [tuple(by_id[i]) for i in order]


def load(run: Path, name: str) -> Session:
    s = Session(run, name)
    try:
        s.lat = json.loads(s.path(".latency.json").read_text())
    except (OSError, ValueError):
        pass
    s.uses = tool_uses(read_jsonl(s.path(".jsonl")))
    s.turns = read_jsonl(s.path(".turns.jsonl"))
    s.metrics = read_jsonl(s.path(".metrics.jsonl"))
    return s


def latest(runtime: Path | None = None) -> tuple[Path, str] | None:
    runtime = runtime or RUNTIME
    runs = [d for d in (runtime / "sessions").glob("*") if d.is_dir()]
    for run in sorted(runs, key=lambda d: d.stat().st_mtime, reverse=True):
        lats = sorted(run.glob("S*.latency.json"), key=lambda p: p.stat().st_mtime)
        if lats:
            return run, lats[-1].name.split(".")[0]
    return None


def body(text: str) -> str:
    """An MCP result's text: {"result": "..."} unwrapped."""
    try:
        v = json.loads(text)
        return v["result"] if isinstance(v, dict) and isinstance(v.get("result"), str) else text
    except (ValueError, TypeError):
        return text


def failed(u) -> bool:
    return u[3] or body(u[2]).startswith(("FAILED", "NOT READY"))


def bg(name: str) -> bool:
    return name.startswith("mcp__bg3__")


def short(name: str) -> str:
    return name.removeprefix("mcp__bg3__")


# ---------------------------------------------------------------------------------------- checks
# Each returns (verdict, name, detail).

def c_first_call(s: Session):
    calls = [u for u in s.uses if bg(u[0])]
    if not calls:
        return "INFO", "first_call", "no bg3 tool calls in the transcript"
    name, _, text, err = calls[0]
    bad = failed(calls[0])
    detail = f"{short(name)} -> {body(text)[:90]!r}"
    return ("WARN" if bad else "PASS"), "first_call", detail


def c_first_action(s: Session):
    start = s.lat.get("start")
    uses = [r for r in s.turns if r.get("type") == "use"]
    # turns.jsonl names bg3 tools without the mcp__bg3__ prefix; claude's own tools are capitalized (Read, Edit)
    acts = [r for r in uses if short(r.get("name") or "X")[:1].islower() and short(r["name"]) not in READ_TOOLS]
    if not start or not acts:
        return "INFO", "first_action", "no start time or no game action"
    dt = acts[0]["t"] - start
    return ("PASS" if dt <= FIRST_ACTION_S else "WARN"), "first_action", \
        f"first game action ({short(acts[0]['name'])}) {dt:.0f} s after the session start (goal ≤ {FIRST_ACTION_S:.0f} s)"


READ_TOOLS = {"look", "get_status", "get_nearby", "get_frontier", "route", "get_combat_state", "look_at", "get_map",
              "get_party", "get_character", "get_inventory", "get_actions", "get_dialogue", "list_saves", "wiki_page",
              "wiki_search", "session_clock", "screenshot", "get_journal", "wait_for"}


def c_session_clock(s: Session):
    n = sum(1 for u in s.uses if short(u[0]) == "session_clock")
    return ("PASS" if n == 0 else "WARN"), "session_clock", f"{n} session_clock calls (the terse ⏱ line should make it unneeded)"


def c_toolsearch(s: Session):
    n = sum(1 for u in s.uses if u[0] == "ToolSearch")
    return ("PASS" if n <= TOOLSEARCH_OK else "WARN"), "toolsearch", f"{n} ToolSearch calls (preload: ≤ {TOOLSEARCH_OK})"


def c_wait_for_noop(s: Session):
    if not s.lat:
        return "INFO", "wait_for_noop", "no latency.json"
    if "wait_for_noop" not in s.lat:
        return "WARN", "wait_for_noop", "latency.json has no wait_for_noop (bridge/metrics older than f92972b?)"
    wn = s.lat.get("wait_for_noop") or {}
    slow = [r for r in s.metrics if r.get("type") == "tool" and r.get("tool") == "wait_for" and r.get("noop")
            and (r.get("ms") or 0) > NOOP_SLOW_MS]
    nothing4 = [u for u in s.uses if short(u[0]) == "wait_for" and "nothing is running" in u[2]]
    detail = f"{wn.get('n', 0)}/{wn.get('of', 0)} wait_for returned at once ({wn.get('by')})"
    if slow:
        return "WARN", "wait_for_noop", detail + f"; {len(slow)} no-op waits took > {NOOP_SLOW_MS:.0f} ms (max " \
            f"{max(r['ms'] for r in slow):.0f})"
    return "PASS", "wait_for_noop", detail + f"; {len(nothing4)} 'nothing is running' answers"


def c_pipelining_fields(s: Session):
    p = s.lat.get("pipelining") or {}
    if not s.lat:
        return "INFO", "pipelining", "no latency.json"
    missing = [k for k in ("dialogue", "enemy_turn", "strict_pct") if k not in p]
    if "think_overlap_pct" not in s.lat and "think_overlap_pct" not in p:
        missing.append("think_overlap_pct")
    if missing:
        return "WARN", "pipelining", "latency.json lacks " + ", ".join(missing)
    return "PASS", "pipelining", f"pct {p.get('pct')}, strict {p.get('strict_pct')}, dialogue {p.get('dialogue')}, " \
        f"enemy_turn {p.get('enemy_turn')}, think overlap {s.lat.get('think_overlap_pct', p.get('think_overlap_pct'))}"


def c_wrapup(s: Session):
    if not s.lat:
        return "INFO", "wrapup", "no latency.json"
    rec = any(r.get("type") == "wrapup" for r in s.turns)
    w = s.lat.get("wrapup_s")
    if w is None or not rec:
        return "WARN", "wrapup", f"wrapup_s={w}, wrapup record in turns.jsonl: {rec} (no WRAP UP NOW clock line seen?)"
    return "PASS", "wrapup", f"wrap-up {w:.0f} s counted apart (configured ~180 s)"


def c_api(s: Session):
    api = s.lat.get("api") or {}
    if not s.lat:
        return "INFO", "api", "no latency.json"
    if api.get("source") in (None, "none"):
        return "WARN", "api", "latency.json api block has no source (no result records?)"
    return "PASS", "api", f"{api.get('api_s_per_turn')} s/turn, {api.get('ms_per_out_tok')} ms/out-tok ({api.get('source')})"


def c_read_rt(s: Session):
    rd = ((s.lat.get("tool_rt_ms") or {}).get("read") or {})
    if not rd.get("n"):
        return "INFO", "read_rt", "no read tool records"
    med, p90 = rd.get("median"), rd.get("p90")
    v = "PASS" if med < 100 else "FAIL" if med > 300 else "WARN"
    return v, "read_rt", f"bridge read round trip median {med} ms, p90 {p90} ms (goal < 100 ms), n={rd['n']}"


def c_ops(s: Session):
    """Game-side op cost from the tool records' ops: evals that ran one op alone."""
    alone: dict[str, list[float]] = {}
    for r in s.metrics:
        for op in r.get("ops") or []:
            if isinstance(op, list) and len(op) == 2 and op[0] in OPS_MS:
                alone.setdefault(op[0], []).append(float(op[1]))
    if not alone:
        return "INFO", "ops", "no lone nav.reach_toward/frontier/hazards evals in the metrics"
    parts, v = [], "PASS"
    for op, ms in sorted(alone.items()):
        med = statistics.median(ms)
        if med > OPS_MS[op]:
            v = "WARN"
        parts.append(f"{op} {med:.0f} ms (n={len(ms)}, ≤{OPS_MS[op]:.0f})")
    return v, "ops", "; ".join(parts)


def c_pturn(s: Session):
    cs = [r for r in s.metrics if r.get("type") == "sample" and r.get("combat")]
    if not cs:
        return "INFO", "pturn", "no combat samples"
    n = sum(1 for r in cs if "pturn" in r)
    return ("PASS" if n == len(cs) else "WARN"), "pturn", f"{n}/{len(cs)} combat samples carry pturn; " \
        f"enemy turns excluded {s.lat.get('enemy_turn_s')} s"


def c_arrivals(s: Session):
    texts = [u[2] for u in s.uses if bg(u[0])]
    clutter = sum(len(CLUTTER_RE.findall(t)) for t in texts)
    props = sum(t.count("props ") for t in texts)
    stat = sum(1 for u in s.uses if short(u[0]) in ("walk_toward", "interact", "move_to", "travel_to")
               and STAT_BLOCK_RE.search(u[2]))
    v = "WARN" if clutter or stat else "PASS"
    return v, "arrivals", f"{clutter} clutter '+ Candle/Cup/...' lines, {props} 'props N' counts, " \
        f"{stat} walk/interact results with the after={{hp=…}} stat block"


def c_combat_turn_size(s: Session):
    sizes = [len(u[2]) for u in s.uses if short(u[0]) == "combat_turn"]
    if len(sizes) < 2:
        return "INFO", "combat_turn", f"{len(sizes)} combat_turn calls"
    return "INFO", "combat_turn", f"{len(sizes)} calls, first {sizes[0]} chars, median after it " \
        f"{statistics.median(sizes[1:]):.0f} (expect ~1-2 KB shorter than before f59518d7)"


def c_dialogue(s: Session):
    tags = sum(len(TAG_RE.findall(u[2])) for u in s.uses if bg(u[0]))
    chooses = [u for u in s.uses if short(u[0]) == "choose_dialogue"]
    bad = [u for u in chooses if failed(u) or "changing" in body(u[2])[:200]]
    nearby = sum(1 for u in s.uses if short(u[0]) == "get_nearby" and "char" in json.dumps(u[1]))
    v = "WARN" if chooses and len(bad) * 4 > len(chooses) else "PASS"
    return v, "dialogue", f"{len(chooses)} choose_dialogue ({len(bad)} failed/changing), {tags} skill-check " \
        f"annotations; get_nearby(characters) {nearby}x"


def c_saves(s: Session, actions: Path):
    a, b = s.lat.get("start"), s.lat.get("end")
    if not a or not b:
        return "INFO", "saves", "no session window"
    recs = [r for r in read_jsonl(actions) if r.get("tool") == "save_game.bg" and a <= (r.get("t") or 0) <= b + 60]
    if not recs:
        return "INFO", "saves", "no background saves in the session window"
    bad = [r for r in recs if not r.get("ok")]
    slow = [r for r in recs if (r.get("menu_s") or 0) > SAVE_MENU_WARN_S]
    menus = [r.get("menu_s") for r in recs if r.get("menu_s") is not None]
    v = "FAIL" if bad else "WARN" if slow else "PASS"  # a lost save breaks crash recovery: loud
    return v, "saves", f"{len(recs)} background saves, {len(bad)} failed, menu_s max {max(menus or [0]):.2f} s " \
        f"(≲ {SAVE_MENU_WARN_S} s)" + (f"; '{bad[-1].get('save')}' not found on disk after {bad[-1].get('disk_s')} s: "
                     f"{bad[-1].get('detail')}" + (f"; seen: {bad[-1]['seen']}" if bad[-1].get("seen") else "") if bad else "")


def runner_lines(s: Session, runner_log: Path) -> list[str]:
    """runner.log lines from this session's start to the next session's start."""
    try:
        lines = runner_log.read_text(errors="replace").splitlines()
    except OSError:
        return []
    out, on = [], False
    for line in lines:
        m = RUNNER_TS.match(line)
        msg = m.group(2) if m else ""
        if re.match(rf"{s.name}: claude starting", msg):
            on = True
        elif on and re.match(r"S\d+: claude starting", msg):
            break
        if on:
            out.append(line)
    return out


def c_runner(s: Session, runner_log: Path):
    lines = runner_lines(s, runner_log)
    if not lines:
        return "INFO", "runner", f"no runner.log lines for {s.name}"
    txt = "\n".join(lines)
    hang = [l for l in lines if "agent_hang" in l or "silent" in l.lower()]
    seen = [k for k, pat in (("check_all", r"check_all: \d+ pass"), ("hot reload", r"mod hot-reloaded"),
                             ("self-restart", r"runner code changed: restarting"),
                             ("crash recovery", r"recovering: crashed"), ("splash", r"splash screen .*pressed"))
            if re.search(pat, txt)]
    if hang:
        return "WARN", "runner", f"agent silence/hang flagged: {hang[0][20:140]}"
    return "PASS", "runner", "no agent hang flagged; seen: " + (", ".join(seen) or "nothing notable")


def bg3le_log_for(s: Session, logs: Path) -> Path | None:
    """The biggest bg3le log of the launch the session played on (named by launch time; still written then)."""
    a = s.lat.get("start")
    best = None
    for p in logs.glob("bg3le-*.log*"):
        m = re.match(r"bg3le-(\d{8}-\d{6})\.log", p.name)
        if not m:
            continue
        launched = datetime.strptime(m.group(1), "%Y%m%d-%H%M%S").timestamp()
        if a and (launched > a or p.stat().st_mtime < a):
            continue
        key = (launched, p.stat().st_size)
        if best is None or key > best[0]:
            best = (key, p)
    return best[1] if best else None


CONN_RE = re.compile(r"debug: client (?:attached \((\d+) connected\)|detached \((\d+) remaining\))")
GEN_RE = re.compile(r"noesis: UI generation (\d+) \((\w+)\);.*?(\d+) leaked in all")
# patch 0005: "...; 3 queued, 120 parked, 2300 released late, 11 leaked in all; leaked: Grid 4, ..."
LATE_RE = re.compile(r"noesis: UI generation \d+ .*?(\d+) parked, (\d+) released late, \d+ leaked in all"
                     r"(?:; leaked: (.*))?")


def c_bg3le(s: Session, logs: Path) -> list[tuple[str, str, str]]:
    p = bg3le_log_for(s, logs)
    if p is None:
        return [("INFO", "connections", "no bg3le log for the session")]
    text = p.read_bytes().decode(errors="replace")
    out = []
    crash = re.search(r".*crash: signal \d+.*", text)
    if crash:
        out.append(("FAIL", "bg3le_crash", f"{p.name} (the whole launch, maybe after this session): "
                    f"{crash.group(0).strip()[:120]}"))
    counts = [int(a or b) for a, b in CONN_RE.findall(text)]
    if counts:
        mx, last = max(counts), counts[-1]
        v = "FAIL" if mx > CONNS_FAIL else "WARN" if mx > CONNS_WARN else "PASS"
        out.append((v, "connections", f"bg3le debug clients max {mx}, now {last} ({p.name}; low tens expected)"))
    else:
        out.append(("INFO", "connections", f"no debug client lines in {p.name}"))
    gens = [(int(g), kind, int(leak)) for g, kind, leak in GEN_RE.findall(text)]
    loads = [g for g in gens if g[1] == "StartLoading"]
    if len(gens) >= 2:
        per = (gens[-1][2] - gens[0][2]) / max(1, len(loads))
        v = "WARN" if per > LEAK_PER_LOAD_WARN else "PASS"
        late = LATE_RE.findall(text)
        extra = ""
        if late:
            parked, released, types = late[-1]
            extra = f"; parked {parked}, released late {released}" + (f"; leaked: {types.strip()}" if types else "")
        out.append((v, "ui_leak", f"{len(gens)} UI generations, {len(loads)} loads, leaked {gens[0][2]} -> "
                    f"{gens[-1][2]} ({per:.0f}/load, ≤ {LEAK_PER_LOAD_WARN}){extra}"))
    hooked = re.search(r"noesis: (\d+) of (\d+) View input entry points hooked", text)
    if hooked:
        out.append(("PASS" if hooked.group(1) == hooked.group(2) else "WARN", "input_hooks",
                    f"{hooked.group(1)} of {hooked.group(2)} View input entry points hooked"))
    return out


def run_checks(s: Session, runtime: Path | None = None) -> list[tuple[str, str, str]]:
    runtime = runtime or RUNTIME
    rows = [f(s) for f in (c_first_call, c_first_action, c_session_clock, c_toolsearch, c_wait_for_noop,
                            c_pipelining_fields, c_wrapup, c_api, c_read_rt, c_ops, c_pturn, c_arrivals,
                            c_combat_turn_size, c_dialogue)]
    rows.append(c_saves(s, runtime / "actions.jsonl"))
    rows.append(c_runner(s, runtime / "runner.log"))
    rows += c_bg3le(s, runtime / "logs")
    return rows


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run", type=Path, help="run directory (default: the newest under runtime/sessions)")
    ap.add_argument("--session", help="Sxxx (default: the newest with a latency.json)")
    a = ap.parse_args(argv)
    if a.run and a.session:
        run, name = a.run, a.session
    else:
        found = latest()
        if found is None:
            print("INFO session: no finished session (no Sxxx.latency.json)")
            return 2
        run, name = a.run or found[0], a.session or found[1]
    s = load(run, name)
    print(f"session {run.name}/{name}: {len(s.uses)} tool calls, {len(s.metrics)} metrics records")
    rows = run_checks(s)
    if not s.uses:
        rows.insert(0, ("WARN", "session", f"{name} made no tool calls (a dud session: see {name}.log)"))
    for v, n, d in rows:
        print(f"{v} {n}: {d}")
    return 1 if any(v == "FAIL" for v, _, _ in rows) else 0


if __name__ == "__main__":
    sys.exit(main())

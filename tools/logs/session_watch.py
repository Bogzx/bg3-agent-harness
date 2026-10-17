#!/usr/bin/env python3
"""Check one session against docs/SESSION-WATCH.md signatures: per item, the number of calls with a trigger / PASS /
FAIL match and the first example of each (#call +s tool: text), so the logs worker can write PROVED / FAILED /
NOT-SEEN with evidence. Only the bg3 tools' own calls and results are matched: not memory/Read texts (playbooks quote
the signatures), not thinking signatures, and each call once (the stream repeats a result in tool_use_result).

Usage: tools/logs/session_watch.py S028 [run=claude-run-2]
The rows come from docs/SESSION-WATCH.md itself (the main tree's, also from a worktree): a new row counts at once.
Its backtick signatures become regexes (N = a number, eN = an id, X/<…> = text, … = a gap); signatures read from
R/B/L/M, or after "no"/"not", are not matched here (verdict 'by hand' when nothing else is left).
"""
import json, re, sys, time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "runner"))
import triage  # noqa: E402

NOT_GAME = {"Read", "Edit", "Write", "Glob", "Grep", "ToolSearch", "Bash", "note", "session_clock", "session_start",
            "end_session"}

def main_tree(root=ROOT):
    """The main checkout: root itself, or for a worktree (.git is a file "gitdir: <main>/.git/worktrees/<name>") the
    main tree, which holds the untracked docs/SESSION-WATCH.md and runtime/."""
    git = root / ".git"
    return Path(git.read_text().split(":", 1)[1].strip()).parents[2] if git.is_file() else root


WATCH_FILE = None   # tests point this at a fixed copy


def watch_path(root=ROOT):
    if WATCH_FILE:
        return Path(WATCH_FILE)
    p = root / "docs/SESSION-WATCH.md"
    return p if p.exists() else main_tree(root) / "docs/SESSION-WATCH.md"


def cells(line):
    """A table row's cells: split on | outside backticks (a `a|b` signature stays whole); \\| is a literal |."""
    out, cur, code = [], "", False
    i = 0
    while i < len(line):
        ch = line[i]
        if ch == "\\" and line[i + 1:i + 2] == "|":
            cur += "|"
            i += 2
            continue
        if ch == "`":
            code = not code
        if ch == "|" and not code:
            out.append(cur.strip())
            cur = ""
        else:
            cur += ch
        i += 1
    out.append(cur.strip())
    return out[1:-1] if line.strip().startswith("|") else out


# a placeholder inside a signature -> regex: N = a number, eN/eA… = an entity id, X/Y/<…> = any short text, … = gap
HOLES = [(r"…", r".{0,200}?"), (r"<[^>]{1,30}>", r".{1,80}?"),
         (r"\be[NABMXY]\b", r"e\\d+"), (r"\bN+(?=\b|[ms]\b)", r"\\d+(?:\\.\\d+)?"), (r"\b[XY]\b", r".{1,60}?")]
SOURCE_RX = re.compile(r"(?:\b[RBLMT]|ram\.log|jsonl|stage_ms|nav\.log|metrics)\s*$")  # `x` read from another file
NEGATED_RX = re.compile(r"(?:\bno|\bnot|\bwithout|0×)\s*(?:\w+\s){0,2}$")
COND_RX = re.compile(r"\b(?:whose|while|after|before|within|in a row|twice|again|repeated|more than|then|with no|"
                     r"when|where|unless|still|each|every|both|same|once|next|alone|bare|followed|inside|that|"
                     r"for an?|did not|didn't|omitted|ordinary|at the start)\b|[≥>≤<×]|\d\s*-\s*\d|"
                     r"\b\d+\s*(?:s|ms|m|x)?\s*``", re.I)
WEAK_RX = re.compile(r"^[\w.]+=?$")   # a bare key (`tries=`, `hover_miss`): only a qualifier, never a signature alone


def frag_rx(frag):
    """One backtick signature as a regex over a call's text, or None when it can't be matched in a transcript."""
    frag = re.sub(r"\s*(?:…|\.\.\.)\s*", "…", frag.strip()).strip("…")
    literal = re.sub(r"<[^>]*>|…|\be[NABMXY]\b|\bN+(?=\b|[ms]\b)|\b[XY]\b|\W", "", frag)
    if len(literal) < 6 or frag.startswith(("#", "python3", "grep", "Mods.", "BG3AI_")):
        return None
    rx = re.escape(frag)
    for hole, sub in HOLES:
        rx = re.sub(hole, sub, rx)
    return re.sub(r"(?:\\ )+", r"\\s+", rx)


def split_out(text, seps):
    """text split after any of seps, outside backticks."""
    parts, cur, code = [], "", False
    for ch in text:
        code = not code if ch == "`" else code
        cur += ch
        if not code and cur.endswith(seps):
            parts.append(cur)
            cur = ""
    return parts + [cur]


def clauses(col):
    """A PASS/FAIL cell -> alternatives (any one matches); each = the regexes that must all match one call. The cell
    splits on ; into sentences and those on ' or ' '(or ' '/' into alternatives (outside backticks). A sentence
    whose prose sets a condition this check can't judge (counts, order, `x` > 40, 'right after', 'with no' …) is
    dropped, so is a signature after 'no'/'not'/'without' or read from R/B/L/M, and an alternative of bare keys."""
    out = []
    for sentence in split_out(col, (";",)):
        if COND_RX.search(re.sub(r"`[^`]*`", "``", sentence)):
            continue
        for part in split_out(sentence, (" or ", "(or ", "/")):
            rxs, strong = [], False
            for m in re.finditer(r"`([^`]+)`", part):
                before = part[:m.start()]
                if SOURCE_RX.search(before) or NEGATED_RX.search(before):
                    continue
                r = frag_rx(m.group(1))
                if r:
                    rxs.append(r)
                    strong = strong or not WEAK_RX.match(m.group(1).strip())
            if rxs and strong:
                out.append(rxs)
    return out


def parse_watch(path=None):
    """SESSION-WATCH.md's 4-column table rows: {section, item, when, pass, fail, series, heads}. pass/fail =
    clauses(); heads = the `tool:` names its signatures start with (the tool names check() knows of);
    series = the section is checked by tools/vis/series_report.py (its intro says so): review_session reads those
    through series_report's own SIGNATURES."""
    rows, section, series = [], "", False
    for line in (path or watch_path()).read_text().splitlines():
        if line.startswith("#"):
            section, series = line.lstrip("# ").strip(), False
        elif "series_report.py" in line and not line.startswith("|"):
            series = True
        elif line.startswith("|") and not line.startswith("|---"):
            c = cells(line)
            if len(c) != 4 or c[0].lower().startswith("item"):
                continue
            rows.append({"section": section, "item": c[0], "when": c[1], "pass": clauses(c[2]),
                         "fail": clauses(c[3]), "series": series,
                         "heads": set(re.findall(r"`([a-z_]+):", c[2] + c[3]))})
    return rows


BATCH = {"do_actions", "queue_actions", "wait_for"}


def row_tools(row, names):
    """The bg3 tools a row is about: the known tool names (this session's + the signatures' heads) its item/when
    text mentions."""
    text = row["item"] + " " + row["when"]
    return {n for n in names if re.search(rf"(?<![\w-]){re.escape(n)}(?![\w-])", text)}


def call_text(c):
    """What a signature sees of one call: the tool's stream name, its args (JSON) and its result text."""
    return f"mcp__bg3__{c['name']} {json.dumps(c['input'] or {}, ensure_ascii=False)}\n{c['text']}"


def is_game(c):
    return c["name"] not in NOT_GAME and not c["name"].startswith(("memory_", "wiki_"))


def check(calls, rows=None):
    """[(item, trig, pass, fail, verdict, example)] per SESSION-WATCH.md row (series_report's section left out):
    counts are calls with a match. trig = a call of a tool the row names, or any PASS/FAIL match; a clause matches a
    call when all its signatures do. verdict FAIL? / PASS? / seen / NOT-SEEN, or 'by hand' (no signature a
    transcript can show: R/B/L/M or prose). A row whose item/when names tools is matched on their calls and on
    batches (do_actions, queue_actions, wait_for) only. The example is the first FAIL, else PASS, else trigger call, as
    '#k +Ns tool: text' (k = index in calls)."""
    t0 = next((c["t"] for c in calls if c.get("t")), None)
    texts = [(k, c, call_text(c)) for k, c in enumerate(calls) if is_game(c)]
    rows = rows if rows is not None else parse_watch()
    names = {c["name"] for _, c, _ in texts}.union(*(r.get("heads", ()) for r in rows))
    out = []
    for row in rows:
        if row["series"]:
            continue
        tools = row_tools(row, names)
        hits = {"pass": [], "fail": [], "trig": []}
        for k, c, t in texts:
            if tools and c["name"] not in tools | BATCH:   # a row about named tools: their calls (or a batch's)
                continue
            for kind in ("fail", "pass"):
                for rxs in row[kind]:
                    ms = [re.search(rx, t) for rx in rxs]
                    if all(ms):
                        hits[kind].append((k, c, t, ms[0]))
                        break
            if c["name"] in tools or any(h[0] == k for h in hits["pass"] + hits["fail"]):
                hits["trig"].append((k, c, t, None))
        nt, np_, nf = (len(hits[x]) for x in ("trig", "pass", "fail"))
        verdict = ("FAIL?" if nf else "PASS?" if np_ else "seen" if nt else
                   "NOT-SEEN" if row["pass"] or row["fail"] else "by hand")
        src = hits["fail"] or hits["pass"] or hits["trig"]
        ex = ""
        if src:
            k, c, t, m = src[0]
            at = f" +{c['t'] - t0:.0f}s" if c.get("t") and t0 else ""
            frag = t[m.start():m.end() + 100] if m else c["text"][:180]
            ex = f"#{k}{at} {c['name']}: " + frag[:220].replace("\n", " | ")
        out.append((row["item"], nt, np_, nf, verdict, ex))
    return out


def log_span(path):
    """(first, last) wall time of a bg3le log: its [monotonic] stamps shifted so the last line = the file's mtime."""
    first = last = None
    with open(path, errors="replace") as f:
        for line in f:
            m = re.match(r"\[ *([\d.]+)\]", line)
            if m:
                last = float(m.group(1))
                first = last if first is None else first
    if last is None:
        return None
    off = path.stat().st_mtime - last
    return first + off, last + off, off


def covering_logs(t0, t1, logs_dir=None):
    """The bg3le logs whose span overlaps [t0, t1] (not every log written after t0)."""
    out = []
    for p in (logs_dir or ROOT / "runtime/logs").glob("bg3le-*.log.*"):
        if p.stat().st_size <= 5000 or p.stat().st_mtime < t0:
            continue
        span = log_span(p)
        if span and span[0] <= t1 and span[1] >= t0:
            out.append((p, span[2]))
    return sorted(out, key=lambda x: x[0].stat().st_mtime)


def main():
    sid = sys.argv[1]
    run = sys.argv[2] if len(sys.argv) > 2 else "claude-run-2"
    RT = (ROOT if (ROOT / "runtime").exists() else main_tree()) / "runtime"
    SD = RT / "sessions" / run
    calls, _ = triage.load_calls(SD / f"{sid}.jsonl", SD / f"{sid}.turns.jsonl")
    tools = [c["name"] for c in calls if is_game(c)]
    print(f"# {sid} SESSION-WATCH check ({len(tools)} bg3 tool calls; counts = calls with a match)")
    print(f"{'item':38} {'trig':>5} {'pass':>5} {'fail':>5}  first example")
    rows = check(calls)
    for v in ("FAIL?", "PASS?"):
        for name, nt, np_, nf, verdict, ex in rows:
            if verdict == v:
                print(f"{name[:38]:38} {nt:5} {np_:5} {nf:5}  {verdict:8} {ex}")
    for v, why in (("seen", "a tool it names ran, no PASS/FAIL signature"), ("NOT-SEEN", ""),
                   ("by hand", "no transcript signature: check from R/B/L/M")):
        names = [r[0].split(" (")[0] + (f" ×{r[1]}" if v == "seen" else "") for r in rows if r[4] == v]
        print(f"\n{v} ({len(names)}{'; ' + why if why else ''}): {'; '.join(names)}")
    # clicks / screenshots
    clicks = tools.count("click")
    shots = tools.count("screenshot")
    print(f"\nhand clicks: {clicks}, screenshots: {shots} (S026: 46 / 55 per session)")

    # session span from turns.jsonl
    ts = []
    with open(SD / f"{sid}.turns.jsonl") as f:
        for line in f:
            try:
                ts.append(json.loads(line)["t"])
            except (ValueError, KeyError):
                pass
    t0, t1 = min(ts), max(ts)
    mins = (t1 - t0) / 60
    print(f"span {time.strftime('%H:%M', time.localtime(t0))}-{time.strftime('%H:%M', time.localtime(t1))} "
          f"({mins:.1f} min); bg3 calls/min {len(tools)/mins:.2f}; all tool calls/min {len(calls)/mins:.2f} (baseline ~11, target 8.3)")

    # bg3le log covering the span
    for B, off in covering_logs(t0, t1, RT / "logs"):
        crashes, guards, cleared, gens, vis, nav = [], 0, 0, [], [], []
        with open(B, errors="replace") as f:
            for line in f:
                m = re.match(r"\[ *([\d.]+)\]", line)
                if not m or not (t0 <= float(m.group(1)) + off <= t1):
                    continue
                if "crash: signal" in line or "crash:   pc" in line:
                    crashes.append(line.strip()[:160])
                elif "MultiBindingExpression guarded" in line:
                    guards += 1
                elif "had a destroyed tree element" in line:
                    cleared += 1
                elif "UI generation" in line:
                    gens.append(line.strip())
                elif "Dispatching event Tick" in line:
                    m = re.search(r"\((.*?)\).*took ([\d.]+) ms", line)
                    if m:
                        (vis if "Visibility" in m.group(1) else nav if "Navigation" in m.group(1) else []).append(float(m.group(2)))
        print(f"\nB {B.name}: crashes {len(crashes)} {crashes[:2]}; guard at start {guards}, cleared {cleared}")
        if gens:
            parked = [int(x) for g in gens for x in re.findall(r"(\d+) parked", g)]
            running = [int(x) for g in gens if "(Running)" in g for x in re.findall(r"(\d+) parked", g)]
            print(f"  UI generations in span {len(gens)}; parked max {max(parked, default=0)}, while Running min {min(running, default=0)}")
            print(f"  last: {gens[-1][:160]}")
        print(f"  Visibility Tick logged >threshold: {len(vis)} (max {max(vis, default=0):.1f} ms); Navigation: {len(nav)} (max {max(nav, default=0):.1f} ms)")

    # RAM in the span
    rss, avail = [], []
    ram = RT / "ram.log"
    for line in (open(ram) if ram.exists() else []):
        try:
            t = time.mktime(time.strptime(line[:19], "%Y-%m-%d %H:%M:%S"))
        except ValueError:
            continue
        if t0 <= t <= t1:
            avail.append(int(re.search(r"avail=(\d+)", line).group(1)))
            rss.append(int(re.search(r"game_rss=(\d+)", line).group(1)))
    if rss:
        print(f"\nRAM: game_rss {rss[0]}→{rss[-1]} MB (max {max(rss)}), avail min {min(avail)} MB, {len(rss)} samples")

    # runner latency line
    with open(RT / "run-cont.log", errors="replace") as f:
        lines = [l.rstrip() for l in f if sid in l and ("latency" in l or "calls/min" in l or "killed" in l or "fix pass" in l)]
    print("\nrun-cont:", *lines[-6:], sep="\n  ")


if __name__ == "__main__":
    main()

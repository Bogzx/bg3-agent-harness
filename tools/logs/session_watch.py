#!/usr/bin/env python3
"""Check one session against docs/SESSION-WATCH.md signatures: per item, counts of trigger / PASS / FAIL matches with
the first example of each, so the logs worker can write PROVED / FAILED / NOT-SEEN with evidence. Streams files.

Usage: tools/logs/session_watch.py S028 [run=claude-run-2]
The patterns are a hand-kept copy of SESSION-WATCH.md's signatures: update them when that file changes.
"""
import json, re, sys, time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sid = sys.argv[1]
run = sys.argv[2] if len(sys.argv) > 2 else "claude-run-2"
SD = ROOT / "runtime/sessions" / run
J = SD / f"{sid}.jsonl"

# (item, trigger, pass, fail) regexes over the transcript text (tool results are JSON-escaped: \n is literal "\\n")
ITEMS = [
    ("lockpick right-click menu", r"mcp__bg3__lockpick", r"lockpick: done[^\"]{0,200}", r"lockpick: FAILED[^\"]{0,200}"),
    ("camera center before hover", r"click=\{", r"camera=\[center[^\"]{0,80}", r"reason=off_screen[^\"]{0,40}tries=0"),
    ("roll_check retrigger", r"mcp__bg3__roll_check", r"roll_check: done[^\"]{0,120}", r"roll_check: TIMEOUT[^\"]{0,120}"),
    ("trap stop on a walk", r"(?i)trap", r"TRAP spotted on the way:[^\"]{0,160}", r"NOMATCH^"),
    ("ESCAPE in a conversation", r"ESCAPE", r"closed it again[^\"]{0,120}", r"STILL OPEN[^\"]{0,120}"),
    ("fast_travel leaves rest/camp to agent", r"mcp__bg3__fast_travel", r"not answered; decide with confirm\([^\"]{0,120}", r"fast_travel[^\"]{0,300}rest type=[^\"]{0,80}"),
    ("fast_travel waits for the load", r"mcp__bg3__fast_travel", r"fast_travel: done[^\"]{0,200}arrived[^\"]{0,40}", r"still not back in the game 2 min later|fast_travel: FAILED[^\"]{0,160}|can't fast travel now[^\"]{0,40}"),
    ("feed_gale", r"mcp__bg3__feed_gale|screen=donate", r"feed_gale: done[^\"]{0,120}", r"feed_gale: FAILED[^\"]{0,160}"),
    ("long answer lists", r"\(\+\d+ more below|choose_dialogue", r"choose_dialogue: done[^\"]{0,80}", r"\(\+\d+ more below[^\"]{0,60}|no option \d+[^\"]{0,60}"),
    ("cinematic lines skipped", r"line still playing", r"NOMATCH^", r"line still playing[^\"]{0,120}"),
    ("Exit TB reflex", r"(?i)turn.based", r"turn_based_off[^\"]{0,120}", r"turn-based mode outside combat[^\"]{0,120}|Exit TB Mode \(~0\.[^\"]{0,60}"),
    ("load_game press retry", r"mcp__bg3__load_game", r"pressed 2 times[^\"]{0,120}", r"the Load press didn't start a load[^\"]{0,80}"),
    ("load_game crash during load", r"mcp__bg3__load_game", r"CRASHED during the load[^\"]{0,120}", r"did not come back to Running within 120 s"),
    ("NotReady while mod down", r"still loading|index a nil value|server context runs", r"the game is still loading, or at the main menu[^\"]{0,80}", r"attempt to index a nil value \(field 'BG3AI'\)|server context runs once a save is loaded"),
    ("doorway blockers", r"in the way|path blocked", r"in the way: [^\"]{0,160}", r"stopped_short reason=path blocked[^\"]{0,80}"),
    ("camp follow / trailing follower", r"fell behind|PARTY APART", r"fell behind: walked back to[^\"]{0,80}", r"fell behind; the walk back failed[^\"]{0,200}|PARTY APART[^\"]{0,120}"),
    ("los_spot step", r"line of sight", r"los[_ ]spot[^\"]{0,160}", r"no line of sight[^\"]{0,160}"),
    ("jump gaps in WAYS", r"\| jump:", r"jump_to: done[^\"]{0,160}", r"jump_to: FAILED[^\"]{0,160}"),
    ("camp member LEVEL UP", r"LEVEL UP", r"level_up: done[^\"]{0,120}", r"level_up: FAILED[^\"]{0,200}"),
    ("level_up plan refusals", r"not offered|already known", r"NOMATCH^", r"(?:not offered|already known)[^\"]{0,120}"),
    ("who= names on fresh bridge", r"unknown target", r"NOMATCH^", r"unknown target '[A-Z][a-z][^']*'[^\"]{0,60}"),
    ("pickpocket by member", r"mcp__bg3__pickpocket", r"pickpocket: done[^\"]{0,120}", r"no pickpocket window[^\"]{0,80}|pickpocket: FAILED[^\"]{0,120}"),
    ("identical items not ambiguous", r"ambiguous|mcp__bg3__manage_inventory|equip", r"NOMATCH^", r"is ambiguous[^\"]{0,120}"),
    ("sell=junk noise", r"sell=junk|\"sell\": ?\"junk\"", r"sold[^\"]{0,160}", r"NOMATCH^"),
    ("roll reroll", r"reroll_on_fail", r"reroll \(1 Inspiration\)[^\"]{0,160}", r"the game kept the first result[^\"]{0,80}"),
    ("split box", r"SplitItem|how many of the stack", r"how many of the stack[^\"]{0,120}|closed_split_box", r"panels_open=\[SplitItem\]|no confirmation box is open"),
    ("long_rest greyed", r"mcp__bg3__long_rest", r"doesn't allow a long rest right now[^\"]{0,160}|long_rest: done[^\"]{0,120}", r"rest screen didn't open[^\"]{0,200}"),
    ("talk_to no window", r"mcp__bg3__talk_to", r"\(no conversation window opened[^\"]{0,80}", r"NOMATCH^"),
    ("look_at carried item", r"mcp__bg3__look_at", r"NOMATCH^", r"look_at[^\"]{0,60}FAILED: not visible"),
    ("out-of-sight character", r"out of sight|not visible", r"is out of sight: going to where you last saw them[^\"]{0,120}", r"talk_to: FAILED — target is not visible[^\"]{0,80}"),
    ("far talk_to/interact", r"away|too far", r"away: walking there first[^\"]{0,120}", r"is too far away \(\d+(?:\.\d+)?m from this character, max 40m\)"),
    ("static targets remembered", r"mcp__bg3__interact|mcp__bg3__route", r"interact: done[^\"]{0,60}", r"interact: FAILED — target is not visible|route[^\"]{0,80}not visible"),
    ("trade hand-over (donate)", r"donat", r"trade: done — gave[^\"]{0,120}donated[^\"]{0,60}", r"not confirmed: the trader has no gold left[^\"]{0,60}"),
    ("give a worn item", r"op\\?\": ?\\?\"give", r"unequipped from [^\"]{0,40} first", r"unequip it first"),
    ("Extra Attack free", r"extra \d+:|Extra Attack", r"extra_attack=true[^\"]{0,80}", r"extra \d+: [^\"]{0,80}FAILED — not enough action"),
    ("summon's own attack", r"Spiritual Weapon|summon", r"its own weapon attack '[^\"]{0,120}", r"does not have that action|unknown action \w*Attack"),
    ("chain skips ended member", r"already ended its turn", r"skipped — [^\"]{0,60}already ended its turn", r"CHAIN STOPPED after 0 of \d+"),
    ("touch cast walks into range", r"move closer first|walked_into_range", r"walked_into_range=[^\"]{0,80}", r"move closer first"),
    ("lost target, no walk", r"no other hostile is in sight|target is not visible", r"no other hostile is in sight[^\"]{0,80}", r"move [^\"]{0,30}: FAILED — target is not visible"),
    ("session-start news", r"older changes|harness news|HARNESS NEWS", r"\(\+\d+ older changes[^\"]{0,80}", r"NOMATCH^"),
    ("retired quirks quoted", r"Exit TB Mode \(~0\.|give is bugged|lantern must be held", r"NOMATCH^", r"Exit TB Mode \(~0\.[^\"]{0,60}|give is bugged[^\"]{0,60}|lantern must be held[^\"]{0,60}"),
]


def first(rx, text):
    m = re.search(rx, text)
    return m.group(0)[:220].replace("\\n", " | ") if m else ""


def main():
    full = J.read_text(errors="replace")
    tools = re.findall(r'"name":"mcp__bg3__([a-z_]+)"', full)
    # match signatures only in tool results and tool calls (not the prompt / hook text that quotes them)
    text = "\n".join(l for l in full.splitlines() if '"tool_result"' in l or '"tool_use"' in l)
    print(f"# {sid} SESSION-WATCH check ({len(tools)} bg3 tool calls)")
    print(f"{'item':38} {'trig':>5} {'pass':>5} {'fail':>5}  first example")
    for name, trig, ok, bad in ITEMS:
        nt, np_, nf = (len(re.findall(r, text)) for r in (trig, ok, bad))
        verdict = "FAIL?" if nf else ("PASS?" if np_ else ("seen" if nt else "NOT-SEEN"))
        ex = first(bad, text) if nf else first(ok, text) if np_ else first(trig + r"[^\"]{0,120}", text) if nt else ""
        print(f"{name:38} {nt:5} {np_:5} {nf:5}  {verdict:8} {ex}")
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
    allcalls = len(re.findall(r'"type":"tool_use"', full))
    print(f"span {time.strftime('%H:%M', time.localtime(t0))}-{time.strftime('%H:%M', time.localtime(t1))} "
          f"({mins:.1f} min); bg3 calls/min {len(tools)/mins:.2f}; all tool_use/min {allcalls/mins:.2f} (baseline ~11, target 8.3)")

    # bg3le log covering the span
    logs = [p for p in (ROOT / "runtime/logs").glob("bg3le-*.log.*") if p.stat().st_size > 5000 and p.stat().st_mtime >= t0]
    for B in sorted(logs, key=lambda p: p.stat().st_mtime):
        crashes, guards, cleared, gens, vis, nav = [], 0, 0, [], [], []
        last = 0.0  # monotonic stamp of the last line ≈ the file's mtime
        with open(B, errors="replace") as f:
            for line in f:
                m = re.match(r"\[ *([\d.]+)\]", line)
                if m:
                    last = float(m.group(1))
        off = B.stat().st_mtime - last
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
    with open(ROOT / "runtime/ram.log") as f:
        for line in f:
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
    with open(ROOT / "runtime/run-cont.log", errors="replace") as f:
        lines = [l.rstrip() for l in f if sid in l and ("latency" in l or "calls/min" in l or "killed" in l or "fix pass" in l)]
    print("\nrun-cont:", *lines[-6:], sep="\n  ")


if __name__ == "__main__":
    main()

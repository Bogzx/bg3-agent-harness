#!/usr/bin/env python3
"""Fight miner: every fight of a session (combat spans from Sxxx.metrics.jsonl samples), with rounds, party HP lost,
downs, deaths, enemies, our abilities, the outcome (won / reload / wipe) and whether the harness showed a debrief or
lesson. Appends to runtime/mined/fights.jsonl and prints the cross-session history per enemy group: did a retry go
better (rounds, HP lost, downs)? same tactic failing twice?

Usage: tools/logs/fights.py S033 [S034 ...] [--run claude-run-2] [--history]
"""
import argparse, json, re, sys, time
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
import repo_paths  # noqa: E402
SESSIONS = repo_paths.runtime_dir() / "sessions"   # the main tree's from a worktree
OUT = repo_paths.runtime_dir() / "mined/fights.jsonl"
GAP_S = 25          # combat samples further apart than this split fights
RELOAD_AFTER_S = 120  # a load_game this soon after a fight's end counts as its reload

HP_RE = re.compile(r"(e\d+) ([A-Z][\w' -]*?) (?:moved [^,\n]*, )?HP (\d+)->(\d+)/(\d+)")
DOWN_RE = re.compile(r"(e\d+) ([A-Z][\w' -]*?) [^\n]*\+Downed")
DIED_RE = re.compile(r"died name=([^\n}]+?) (e\d+)")
DIED2_RE = re.compile(r"died (e\d+) name=([^\n}]+)")
# compacted results (bridge compact.py): "died e34 Imp" / "died e67" (named earlier in the result), or no died event
# at all when CHANGES already says "~ e32 Imp DEAD"; names come from any "eN Name" line of the session
DIED3_RE = re.compile(r"^\s*died (e\d+)(?: ([A-Z][^\n;=]*))?$", re.M)
_WORDS = r"([A-Z][a-z'][\w'-]*(?: [A-Z][a-z'][\w'-]*)*)"  # a display name: capitalized words, not DEAD/HP flags
DEAD_RE = re.compile(r"^\s*~ (e\d+) " + _WORDS + r"[ ,][^\n]*\bDEAD\b(?! END)", re.M)
NAMED_RE = re.compile(r"(?:^|[\s{(\[])(e\d+) (?:name=)?" + _WORDS + r"(?=[\s,;:}\])]|$)")
OURS_RE = re.compile(r"(?:PARTY [+-] e\d+ ([A-Z][\w' -]*)|e\d+ ([A-Z][\w' -]*?) \(yours\))")
TURN_RE = re.compile(r"TURN (?:round (\d+) turn=)?(e\d+) ([A-Z][\w' -]*?)(?: \(round (\d+)\)|\n|$)")
ROUND_RE = re.compile(r"round[ =](\d+)")
PARTY_RE = re.compile(r"PARTY \*?(e\d+) ([A-Z][\w'-]*)")
# combat_turn's own turn headers are always ours ("TURN e1 Corran Vell (round 1)"); PARTY lines give one word only
OUR_TURN_RE = re.compile(r"^TURN e\d+ ([A-Z][\w' -]*?) \(round \d+\)", re.M)
# the harness's own fight summary: outcome, rounds, enemies ("vs Imp x3, Commander Zhalk +1 more"), our names
DEBRIEF_RE = re.compile(r"FIGHT DEBRIEF \(([\w ]+), (\d+) rounds?, [^)]*\) vs ([^\n]+)")
TOOK_RE = re.compile(r"· took ([^\n]+)")
KILLED_RE = re.compile(r"FIGHT DEBRIEF \([^\n]*\n(?:  [^\n]*\n)*?  killed (\d+)/(\d+)")
# the debrief block's "  downed: Wyll r2 by Orin, Karlach r6 by Orin (Slay), Shadowheart r7 by Death's Head of Bhaal
# Fustian": one entry per down, the harness's complete list (S065's results showed no +Downed for its 1 and 4 downs)
DEBRIEF_BLOCK_RE = re.compile(r"FIGHT DEBRIEF \([^\n]*(?:\n  [^\n]*)*")
DOWNED_ENTRY_RE = re.compile(r"(?:^|, )([A-Z][\w' -]*?) r\d+\b")
# summons are listed as party ("PARTY e267 Spiritual Weapon L1 HP 20/20 summon of e267", "PARTY + e472 Spiritual
# Weapon HP 20/20"); their expiry is not a party death
SUMMON_RE = re.compile(r"PARTY (?:\+ )?\*?e\d+ ([A-Z][\w' -]*?) (?:L\d+ )?HP [^\n]*?summon of|PARTY \+ e\d+ ([A-Z][\w' -]*?) HP")
WHERE_RE = re.compile(r"WHERE ([^|\n·]+)")
ABIL_RE = re.compile(r'"ability\\?"\s*:\s*\\?"([^"\\]+)')
CAST_RE = re.compile(r"cast=([A-Za-z_]+)")
# compacted action lines drop "cast=X" when X is the action id: "action: Target_MainHandAttack e31: done — target=..",
# "bonus: Target_PommelStrike e67: done — statuses=[..]"
ACTION_DONE_RE = re.compile(r"(?:(?:action|bonus): |→ )([A-Z][A-Za-z]*_\w+)(?: e\d+)?: done\b(?![^\n]*cast=)")
FLAGS = {"debrief": re.compile(r"(?i)debrief"), "last_attempt": re.compile(r"LAST ATTEMPT"),
         "lesson": re.compile(r"(?i)\blessons?\b"), "repeat_warning": re.compile(r"WARNING: [^\n]*last time (?:vs these enemies )?too")}


def deaths(text, names=None):
    """{(name, handle)} of everything that died in these results, old (`died name=X eN`, `died eN name=X`) and
    compacted forms (`died eN X`, `died eN`, CHANGES `~ eN X ... DEAD`). names: handle -> name for bare handles."""
    names = names or {}
    out = {(n.strip(), e) for n, e in DIED_RE.findall(text)} | {(n.strip(), e) for e, n in DIED2_RE.findall(text)}
    out |= {((n or names.get(e, e)).strip(), e) for e, n in DIED3_RE.findall(text)}
    out |= {(n.strip(), e) for e, n in DEAD_RE.findall(text)}
    by_handle = {}
    for n, e in out:  # one entry per handle: a real name beats a bare handle
        if e not in by_handle or by_handle[e] == e:
            by_handle[e] = n
    return {(n, e) for e, n in by_handle.items()}


def handle_names(text):
    """handle -> the last display name a result gave it (`e34 Imp`, `e2 name=Brannoch Vael`, `name=Imp e34`)."""
    seen = defaultdict(Counter)
    for e, n in NAMED_RE.findall(text):
        seen[e][n] += 1
    for n, e in DIED_RE.findall(text):
        seen[e][n.strip()] += 1
    return {e: c.most_common(1)[0][0] for e, c in seen.items()}


def casts_of(text):
    return Counter(CAST_RE.findall(text)) + Counter(ACTION_DONE_RE.findall(text))


def debrief(text):
    """(outcome, rounds, [enemy names]) of the first FIGHT DEBRIEF in text, or None."""
    m = DEBRIEF_RE.search(text)
    if not m:
        return None
    names = [re.sub(r" x\d+$", "", n.strip()) for n in re.sub(r" \+\d+ more$", "", m.group(3).strip()).split(", ")]
    return m.group(1), int(m.group(2)), [n for n in names if n]


def debrief_downs(text):
    """Counter of the names in the first FIGHT DEBRIEF's `downed:` line (party and summons alike; empty when nobody
    went down), or None without a debrief."""
    m = DEBRIEF_BLOCK_RE.search(text)
    if not m:
        return None
    line = next((ln[len("  downed: "):] for ln in m.group(0).split("\n") if ln.startswith("  downed: ")), "")
    return Counter(base(n) for n in DOWNED_ENTRY_RE.findall(line))


def party_names(calls):
    """Our characters by full name. PARTY lines give the first word only ("PARTY e1 Corran Vell ..." -> Corran), so
    full names come from combat_turn turn headers and the debrief's "took A n, B m", kept when their first word is a
    PARTY name (a summon's turn header, "Spiritual Weapon", is not a member); no PARTY line at all: kept as they are."""
    first, full, summons = set(), set(), set()
    for c in calls:
        first.update(n for _, n in PARTY_RE.findall(c["res"]))
        summons.update(base(a or b) for a, b in SUMMON_RE.findall(c["res"]))
        if c["tool"] in ("combat_turn", "end_turn"):
            full.update(base(n) for n in OUR_TURN_RE.findall(c["res"]))
        for m in TOOK_RE.findall(c["res"]):
            full.update(base(re.sub(r" \d+$", "", x.strip())) for x in m.split(","))
    full = {n for n in full if n and (not first or n.split()[0] in first)}
    return (first | full) - summons - {n.split()[0] for n in summons}


def base(name):
    return re.sub(r"\s+\d+$", "", name.strip())


def load_session(run, sid):
    sd = SESSIONS / run
    t_use, t_res = {}, {}
    with open(sd / f"{sid}.turns.jsonl") as f:
        for line in f:
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if e.get("type") == "use":
                t_use[e["id"]] = e["t"]
            elif e.get("type") == "result":
                t_res[e["id"]] = e["t"]
    calls = {}
    with open(sd / f"{sid}.jsonl", errors="replace") as f:
        for line in f:
            try:
                d = json.loads(line)
            except ValueError:
                continue
            msg = d.get("message") or {}
            content = msg.get("content")
            if not isinstance(content, list):
                continue
            for c in content:
                if c.get("type") == "tool_use":
                    calls[c["id"]] = {"tool": c["name"].replace("mcp__bg3__", ""), "args": json.dumps(c.get("input", {})),
                                      "t": t_use.get(c["id"]), "res": ""}
                elif c.get("type") == "tool_result" and c.get("tool_use_id") in calls:
                    body = c.get("content")
                    if isinstance(body, list):
                        body = "\n".join(x.get("text", "") for x in body if isinstance(x, dict))
                    body = str(body)
                    try:  # MCP results are {"result": "..."}
                        body = json.loads(body).get("result", body)
                    except (ValueError, AttributeError):
                        pass
                    calls[c["tool_use_id"]]["res"] = body
                    calls[c["tool_use_id"]]["t1"] = t_res.get(c["tool_use_id"])
    calls = [c for c in calls.values() if c["t"]]
    calls.sort(key=lambda c: c["t"])
    spans, cur, last_t = [], None, 0.0
    with open(sd / f"{sid}.metrics.jsonl") as f:
        for line in f:
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if e.get("type") != "sample":
                continue
            last_t = max(last_t, e.get("t", 0))
            if e.get("combat"):
                if cur and e["t"] - cur[1] <= GAP_S:
                    cur[1] = e["t"]
                else:
                    cur = [e["t"], e["t"]]
                    spans.append(cur)
    return calls, spans, last_t


def outcome_of(reloaded, downed_all, killed, cut):
    """reload / wipe / unfinished (still in combat when the session's samples stop: S045 Ketheric try 2 was called
    "won") / won (enemies died) / ended."""
    if reloaded:
        return "reload"
    if downed_all:
        return "wipe"
    if cut:
        return "unfinished"
    return "won" if killed else "ended"


def mine(run, sid):
    calls, spans, last_t = load_session(run, sid)
    party = party_names(calls)
    ours = set()
    for c in calls:
        for a, b in OURS_RE.findall(c["res"]):
            ours.add(base(a or b))
    names = handle_names("\n".join(c["res"] for c in calls))
    fights = []
    where = "?"
    ci = 0
    for start, end in spans:
        if end - start < 5:
            continue
        inside = [c for c in calls if start - 10 <= c["t"] <= end + 5]
        before = [c for c in calls if c["t"] < start]
        for c in inside + list(reversed(before)):
            m = WHERE_RE.search(c["res"])
            if m:
                where = m.group(1).split("->")[-1].strip()
                break
        text = "\n".join(c["res"] for c in inside)
        rounds = max([int(x) for x in ROUND_RE.findall(text)] or [0])
        hp_lost = Counter()
        seen = set()
        for eid, name, a, b, mx in HP_RE.findall(text):
            key = (eid, a, b)
            if key in seen:
                continue
            seen.add(key)
            if base(name) in party and int(b) < int(a):
                hp_lost[base(name)] += int(a) - int(b)
        downs = Counter()
        seen = set()
        for c in inside:
            for eid, name in DOWN_RE.findall(c["res"]):
                if (eid, c["t"]) not in seen and base(name) in party:
                    seen.add((eid, c["t"]))
                    downs[base(name)] += 1
        died = deaths(text, names)
        party_died = sorted({base(n) for n, _ in died if base(n) in party})
        enemies = Counter()
        for n, e in died:
            if base(n) not in party and base(n) not in ours:
                enemies[base(n)] += 1
        for m in TURN_RE.finditer(text):
            n = base(m.group(3))
            if n not in party and n not in ours:
                enemies[n] += 0  # present, not killed
        after = [c for c in calls if end < c["t"] <= end + RELOAD_AFTER_S]
        db = debrief(text + "\n" + "\n".join(c["res"] for c in after))
        km = KILLED_RE.search(text + "\n" + "\n".join(c["res"] for c in after))
        dd = debrief_downs(text + "\n" + "\n".join(c["res"] for c in after))
        if dd is not None:  # the harness's own list is complete, like its rounds (S065: 0 downs mined, it had 1 and 4)
            downs = Counter({n: k for n, k in dd.items() if n in party})
        if db:  # the harness's own list is complete (compacted results may name no death / no enemy turn)
            rounds = db[1]
            for n in db[2]:
                enemies[base(n)] += 0
        enemy_names = sorted(enemies, key=lambda n: (-enemies[n], n))
        abilities = Counter()
        for c in inside:
            if c["tool"] in ("combat_turn", "use_ability", "do_actions"):
                for a in ABIL_RE.findall(c["args"]):
                    abilities[a] += 1
        casts = casts_of(text)
        reload_c = next((c for c in inside + after if c["tool"] == "load_game"), None)
        downed_all = len(party) and len(downs) >= min(4, len(party)) and not enemies
        cut = last_t - end <= GAP_S  # combat still on at the last sample: the session ended mid-fight
        graded = db[0] if db else None   # debrief.py grades: clean / costly / near_wipe / wipe / reloaded
        outcome = outcome_of(bool(reload_c) or graded == "reloaded", bool(downed_all) or graded == "wipe",
                             sum(enemies.values()) > 0 or graded in ("clean", "costly", "near_wipe"), cut)
        flags = {k: bool(rx.search(text + "\n".join(c["res"] for c in after))) for k, rx in FLAGS.items()}
        key = ", ".join(sorted(enemy_names[:3])) or "?"  # enemy group; the place is shown per attempt
        fights.append({
            "run": run, "session": sid, "debrief": db[0] if db else None, "start": time.strftime("%H:%M:%S", time.localtime(start)), "t": start,
            "dur_s": round(end - start), "where": where, "key": key, "enemies": dict(enemies), "rounds": rounds,
            "hp_lost": sum(hp_lost.values()), "hp_lost_by": dict(hp_lost), "downs": sum(downs.values()),
            "downs_by": dict(downs), "party_died": party_died, "outcome": outcome,
            "reload_to": (re.search(r"loaded=([^\n ]+)", reload_c["res"]) or [None, None])[1] if reload_c else None,
            "abilities": dict(abilities.most_common(8)), "casts": dict(casts.most_common(8)), "harness": flags,
            "killed": int(km.group(1)) if km else None,
        })
    # party deaths outside any fight span (falls, traps, walks): logged as incidents with the next reload
    for c in calls:
        if any(st - 10 <= c["t"] <= en + 5 for st, en in spans):
            continue
        for n, e in sorted(deaths(c["res"], names)):
            if base(n) in party:
                rl = next((d for d in calls if c["t"] < d["t"] <= c["t"] + 600 and d["tool"] == "load_game"), None)
                fights.append({"run": run, "session": sid, "start": time.strftime("%H:%M:%S", time.localtime(c["t"])), "t": c["t"],
                               "dur_s": 0, "where": where, "key": "INCIDENT: " + base(n) + " died out of combat",
                               "enemies": {}, "rounds": 0, "hp_lost": 0, "hp_lost_by": {}, "downs": 0, "downs_by": {},
                               "party_died": [base(n)], "outcome": "reload" if rl else "death",
                               "reload_to": (re.search(r"loaded=([^\n ]+)", rl["res"]) or [None, None])[1] if rl else None,
                               "abilities": {}, "casts": {}, "harness": {}, "cause_tool": c["tool"], "cause_args": c["args"][:120]})
    fights.sort(key=lambda f: f["t"])
    return fights


def bad(f):
    """A fight to learn from: reloaded / wiped / a death or down, or the harness's debrief graded it bad
    (bg3ai.debrief.BAD: costly, near_wipe, wipe, reloaded)."""
    return (f["outcome"] in ("reload", "wipe", "death") or f["downs"] > 0 or bool(f["party_died"])
            or f.get("debrief") in ("costly", "near_wipe", "wipe", "reloaded"))


def retry_verdict(p, f):
    """An attempt vs the earlier one at the same group: "better" (won, fewer downs, or as many and less HP lost),
    "further (killed 2 vs 0)" when it got more enemies killed but cost as much or more (S065 Orin try 2: 2 Reapers
    and Orin to 21 % with a death; S061 09:37: Gortash killed, then the wipe), else "NOT better"."""
    if (f["outcome"] == "won" and p["outcome"] != "won") or f["downs"] < p["downs"] or \
            (f["downs"] == p["downs"] and f["hp_lost"] < p["hp_lost"]):
        return "better"
    if f.get("killed") is not None and p.get("killed") is not None and f["killed"] > p["killed"]:
        return f"further (killed {f['killed']} vs {p['killed']})"
    return "NOT better"


def same_tactic(p, f):
    """The top abilities both failed attempts share (>= 3 of the top 4), only when the later one made no progress:
    a class's damage kit repeats in a win too (S062's win shared Divine Smite / Frenzied Strike / Main Hand with
    S061 09:37's wipe; the plan changed in target order, position and counterspells)."""
    if retry_verdict(p, f) != "NOT better" or not (bad(p) and bad(f)):
        return []
    same = set(list(p["abilities"])[:4]) & set(list(f["abilities"])[:4])
    return sorted(same) if len(same) >= 3 else []


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("sessions", nargs="*")
    ap.add_argument("--run", default="claude-run-2")
    ap.add_argument("--history", action="store_true", help="print the cross-session table only")
    a = ap.parse_args()
    store = []
    if OUT.exists():
        store = [json.loads(l) for l in OUT.open()]
    for sid in a.sessions:
        new = mine(a.run, sid)
        store = [f for f in store if (f.get("run", "claude-run-2"), f["session"]) != (a.run, sid)] + new
        print(f"# {sid}: {len(new)} fights")
        for f in new:
            mark = "BAD " if bad(f) else "    "
            print(f"{mark}{f['start']} {f['dur_s']:4d}s r{f['rounds']:<2} hp-{f['hp_lost']:<4} downs {f['downs']} "
                  f"{f['outcome']:6} {f['key']}  downs_by={f['downs_by']} died={f['party_died']} harness={[k for k, v in f['harness'].items() if v]}")
    if a.sessions:
        OUT.parent.mkdir(parents=True, exist_ok=True)
        with OUT.open("w") as fo:
            for f in sorted(store, key=lambda f: f["t"]):
                fo.write(json.dumps(f) + "\n")
    # history per enemy group (same region + same top enemies), only groups fought more than once or with a bad fight;
    # this playthrough only (records before 2026-10-17 have no run: claude-run-2)
    store = [f for f in store if f.get("run", "claude-run-2") == a.run]
    groups = defaultdict(list)
    for f in sorted(store, key=lambda f: f["t"]):
        groups[f["key"]].append(f)
    print("\n# retries (the next fight after a reload, >= 2 shared enemy names)")
    seq = sorted(store, key=lambda f: f["t"])
    for i, f in enumerate(seq):
        if f["outcome"] != "reload":
            continue
        nxt = next((g for g in seq[i + 1:] if len(set(g["enemies"]) & set(f["enemies"])) >= 2), None)
        if not nxt:
            print(f"  {f['session']} {f['start']} reload → no retry seen yet")
            continue
        same = same_tactic(f, nxt)
        print(f"  {f['session']} {f['start']} r{f['rounds']} hp-{f['hp_lost']} downs {f['downs']} {f['outcome']}  →  "
              f"{nxt['session']} {nxt['start']} r{nxt['rounds']} hp-{nxt['hp_lost']} downs {nxt['downs']} {nxt['outcome']}: "
              f"{retry_verdict(f, nxt)}{'; SAME TACTIC ' + str(same) if same else ''}")
    print("\n# history per enemy group (attempts in order)")
    for key, fs in groups.items():
        if len(fs) < 2 and not any(bad(f) for f in fs):
            continue
        print(f"\n{key}")
        prev = None
        for f in fs:
            note = ""
            if prev:
                note = retry_verdict(prev, f)
                same = same_tactic(prev, f)
                if same:
                    note += f"; REPEATED TACTIC {same}"
            print(f"  {f['session']} {f['start']} [{f['where'][:28]}] r{f['rounds']} hp-{f['hp_lost']} downs {f['downs']} {f['outcome']}"
                  f"{' → ' + f['reload_to'] if f['reload_to'] else ''}  top={list(f['abilities'])[:4]}  {note}")
            prev = f


if __name__ == "__main__":
    main()

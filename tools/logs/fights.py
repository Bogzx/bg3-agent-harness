#!/usr/bin/env python3
"""Fight miner: every fight of a session (combat spans from Sxxx.metrics.jsonl samples), with rounds, party HP lost,
downs, deaths, enemies, our abilities, the outcome (won / reload / wipe) and whether the harness showed a debrief or
lesson. Appends to runtime/mined/fights.jsonl and prints the cross-session history per enemy group: did a retry go
better (rounds, HP lost, downs)? same tactic failing twice?

Usage: tools/logs/fights.py S033 [S034 ...] [--run claude-run-2] [--history]
"""
import argparse, json, re, time
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "runtime/mined/fights.jsonl"
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
WHERE_RE = re.compile(r"WHERE ([^|\n·]+)")
ABIL_RE = re.compile(r'"ability\\?"\s*:\s*\\?"([^"\\]+)')
CAST_RE = re.compile(r"cast=([A-Za-z_]+)")
# compacted action lines drop "cast=X" when X is the action id: "action: Target_MainHandAttack e31: done — target=.."
ACTION_DONE_RE = re.compile(r"(?:action: |→ )([A-Z][A-Za-z]*_\w+)(?: e\d+)?: done\b(?![^\n]*cast=)")
FLAGS = {"debrief": re.compile(r"(?i)debrief"), "last_attempt": re.compile(r"LAST ATTEMPT"),
         "lesson": re.compile(r"(?i)\blessons?\b"), "repeat_warning": re.compile(r"(?i)repeated mistake|same as last time")}


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


def base(name):
    return re.sub(r"\s+\d+$", "", name.strip())


def load_session(run, sid):
    sd = ROOT / "runtime/sessions" / run
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
    party = set()
    ours = set()
    for c in calls:
        for _, n in PARTY_RE.findall(c["res"]):
            party.add(n)
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
        enemy_names = sorted(enemies, key=lambda n: (-enemies[n], n))
        abilities = Counter()
        for c in inside:
            if c["tool"] in ("combat_turn", "use_ability", "do_actions"):
                for a in ABIL_RE.findall(c["args"]):
                    abilities[a] += 1
        casts = casts_of(text)
        after = [c for c in calls if end < c["t"] <= end + RELOAD_AFTER_S]
        reload_c = next((c for c in inside + after if c["tool"] == "load_game"), None)
        downed_all = len(party) and len(downs) >= min(4, len(party)) and not enemies
        cut = last_t - end <= GAP_S  # combat still on at the last sample: the session ended mid-fight
        outcome = outcome_of(bool(reload_c), downed_all, sum(enemies.values()) > 0, cut)
        flags = {k: bool(rx.search(text + "\n".join(c["res"] for c in after))) for k, rx in FLAGS.items()}
        key = ", ".join(sorted(enemy_names[:3])) or "?"  # enemy group; the place is shown per attempt
        fights.append({
            "session": sid, "start": time.strftime("%H:%M:%S", time.localtime(start)), "t": start,
            "dur_s": round(end - start), "where": where, "key": key, "enemies": dict(enemies), "rounds": rounds,
            "hp_lost": sum(hp_lost.values()), "hp_lost_by": dict(hp_lost), "downs": sum(downs.values()),
            "downs_by": dict(downs), "party_died": party_died, "outcome": outcome,
            "reload_to": (re.search(r"loaded=([^\n ]+)", reload_c["res"]) or [None, None])[1] if reload_c else None,
            "abilities": dict(abilities.most_common(8)), "casts": dict(casts.most_common(8)), "harness": flags,
        })
    # party deaths outside any fight span (falls, traps, walks): logged as incidents with the next reload
    for c in calls:
        if any(st - 10 <= c["t"] <= en + 5 for st, en in spans):
            continue
        for n, e in sorted(deaths(c["res"], names)):
            if base(n) in party:
                rl = next((d for d in calls if c["t"] < d["t"] <= c["t"] + 600 and d["tool"] == "load_game"), None)
                fights.append({"session": sid, "start": time.strftime("%H:%M:%S", time.localtime(c["t"])), "t": c["t"],
                               "dur_s": 0, "where": where, "key": "INCIDENT: " + base(n) + " died out of combat",
                               "enemies": {}, "rounds": 0, "hp_lost": 0, "hp_lost_by": {}, "downs": 0, "downs_by": {},
                               "party_died": [base(n)], "outcome": "reload" if rl else "death",
                               "reload_to": (re.search(r"loaded=([^\n ]+)", rl["res"]) or [None, None])[1] if rl else None,
                               "abilities": {}, "casts": {}, "harness": {}, "cause_tool": c["tool"], "cause_args": c["args"][:120]})
    fights.sort(key=lambda f: f["t"])
    return fights


def bad(f):
    return f["outcome"] in ("reload", "wipe", "death") or f["downs"] > 0 or bool(f["party_died"])


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
        store = [f for f in store if f["session"] != sid] + new
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
    # history per enemy group (same region + same top enemies), only groups fought more than once or with a bad fight
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
        better = (nxt["outcome"] == "won") or nxt["downs"] < f["downs"] or (nxt["downs"] == f["downs"] and nxt["hp_lost"] < f["hp_lost"])
        same = set(list(f["abilities"])[:4]) & set(list(nxt["abilities"])[:4])
        print(f"  {f['session']} {f['start']} r{f['rounds']} hp-{f['hp_lost']} downs {f['downs']} {f['outcome']}  →  "
              f"{nxt['session']} {nxt['start']} r{nxt['rounds']} hp-{nxt['hp_lost']} downs {nxt['downs']} {nxt['outcome']}: "
              f"{'better' if better else 'NOT better'}{'; SAME TACTIC ' + str(sorted(same)) if len(same) >= 3 and bad(nxt) else ''}")
    print("\n# history per enemy group (attempts in order)")
    for key, fs in groups.items():
        if len(fs) < 2 and not any(bad(f) for f in fs):
            continue
        print(f"\n{key}")
        prev = None
        for f in fs:
            note = ""
            if prev:
                better = (f["outcome"] == "won" and prev["outcome"] != "won") or \
                         (f["downs"] < prev["downs"]) or (f["downs"] == prev["downs"] and f["hp_lost"] < prev["hp_lost"])
                note = "better" if better else "NOT better"
                if bad(prev) and bad(f):
                    same = set(list(prev["abilities"])[:4]) & set(list(f["abilities"])[:4])
                    if len(same) >= 3:
                        note += f"; REPEATED TACTIC {sorted(same)}"
            print(f"  {f['session']} {f['start']} [{f['where'][:28]}] r{f['rounds']} hp-{f['hp_lost']} downs {f['downs']} {f['outcome']}"
                  f"{' → ' + f['reload_to'] if f['reload_to'] else ''}  top={list(f['abilities'])[:4]}  {note}")
            prev = f


if __name__ == "__main__":
    main()

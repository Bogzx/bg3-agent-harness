#!/usr/bin/env python3
"""Context-bloat trims (vis, 10:31-11:45 2026-10-10) applied to the tool results of sessions recorded before them:
estimated chars saved per trim, per session and per API call (turn). Text-level approximations of each rule:
  ids      ability ids "(Target_X)" in action lists (ACTION_IDS False; kept where two share a name: ignored)
  party    per-member "eN Name moved Nm D" lines -> one "PARTY moved ~Nm D" line (when >= 2 in a result)
  flicker  a status gained and lost by the same target in one EVENTS batch (_drop_flicker)
  aura     aura/light statuses (Moonshield ...) on/off lines after the first per result (StatusFlap mute + crowd)
  props    turn-order props (initiative=-20 neutral items) in get_combat_state -> names in one OBJECTS line
  unavail  "- Name" gone lines in action deltas for actions now unavailable (not measurable from text: skipped)
Usage: python3 tools/logs/trim_estimate.py claude-run-2 S025 S026
"""
import collections, json, os, re, sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ID_RX = re.compile(r" \((?:Target|Projectile|Shout|Zone|Throw|Rush|Wall|Teleportation|Cone|Interrupt)_[A-Za-z0-9_]+\)")
MOVE_RX = re.compile(r"^  e\d+ [^\n]+? moved \d+(?:\.\d+)?m [NSEW]{1,2}$", re.M)
STATUS_RX = re.compile(r"^  status_(applied|removed) name=([^ ]+) [^\n]*?target=\{name=([^}]+)\}[^\n]*$", re.M)
AURA = {"Moonshield", "Shadow-Cursed", "Blessing", "Lightbearer"}
PROP_RX = re.compile(r"initiative=-20 hp_pct=\d+ name=([^ ]+(?: [A-Z][a-z]+)*) from=\{[^}]*\} e\d+ relation=neutral @\([^)]*\);? ?")


def results(path):
    names, out, calls = {}, [], set()
    for line in open(path, errors="replace"):
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if d.get("type") == "assistant":
            calls.add(d["message"].get("id"))
            for c in d["message"].get("content", []):
                if c.get("type") == "tool_use":
                    names[c["id"]] = c["name"].replace("mcp__bg3__", "")
        elif d.get("type") == "user" and isinstance(d["message"].get("content"), list):
            for c in d["message"]["content"]:
                if c.get("type") == "tool_result":
                    t = c.get("content")
                    t = "".join(x.get("text", "") for x in t if isinstance(x, dict)) if isinstance(t, list) else str(t)
                    try:
                        t = json.loads(t).get("result", t)
                    except (ValueError, AttributeError):
                        pass
                    out.append((names.get(c["tool_use_id"], "?"), t))
    return out, len(calls)


def saved(tool, t):
    s = collections.Counter()
    s["ids"] = sum(len(m) for m in ID_RX.findall(t))
    moves = MOVE_RX.findall(t)
    if len(moves) >= 2:
        s["party"] = sum(len(m) + 1 for m in moves) - 26
    lines = STATUS_RX.finditer(t)
    seen, aura_first = {}, set()
    for m in lines:
        kind, name, tgt = m.groups()
        k = (name, tgt)
        if k in seen and seen[k][0] != kind:
            s["flicker"] += len(m.group(0)) + seen.pop(k)[1] + 2
            continue
        seen[k] = (kind, len(m.group(0)))
    for m in STATUS_RX.finditer(t):
        if m.group(2) in AURA:
            if m.group(2) in aura_first:
                s["aura"] += len(m.group(0)) + 1
            aura_first.add(m.group(2))
    s["aura"] = max(0, s["aura"] - s["flicker"])  # don't count a line twice
    if tool in ("get_combat_state", "combat_turn"):
        props = PROP_RX.findall(t)
        if props:
            s["props"] = sum(len(m.group(0)) for m in PROP_RX.finditer(t)) - (40 + sum(len(n) + 2 for n in set(props)))
    return s


def main():
    run, sids = sys.argv[1], sys.argv[2:]
    for sid in sids:
        res, calls = results(os.path.join(ROOT, "runtime/sessions", run, sid + ".jsonl"))
        total = sum(len(t) for _, t in res)
        tot, by_tool = collections.Counter(), collections.Counter()
        for tool, t in res:
            s = saved(tool, t)
            tot.update(s)
            by_tool[tool] += sum(s.values())
        cut = sum(tot.values())
        print(f"== {sid}: {len(res)} tool results, {total/1000:.0f}k chars, {calls} API calls; trims save {cut/1000:.1f}k "
              f"({100*cut/max(total,1):.1f}%), {cut/max(calls,1):.0f} chars/turn of {total/max(calls,1):.0f}")
        print("   by trim: " + ", ".join(f"{k} {v/1000:.1f}k" for k, v in tot.most_common()))
        print("   by tool: " + ", ".join(f"{k} {v/1000:.1f}k" for k, v in by_tool.most_common(6)))


if __name__ == "__main__":
    main()

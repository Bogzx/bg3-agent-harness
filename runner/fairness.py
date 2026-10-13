"""Per-session fairness audit: scan every game-tool result the playing agent got for things a human player
couldn't have seen (docs/FAIRNESS.md). Text rules over the transcript, so it audits what actually reached the
agent, whatever layer produced it.

    python runner/fairness.py runtime/sessions/<playthrough>/S026 [...]   # prints the section per session

triage.py adds the section to every Sxxx.triage.md. Two severities:
    LEAK   information a player can't have (fix the layer that let it through)
    CHECK  judgment calls worth a look (internal ids the UI never shows, hidden-state words on a listing)
Results of the agent's own memory and wiki tools (memory_read, memory_search, wiki_page, ...) are not game observations and
are skipped; so are screenshots (the rendered frame is fair by construction).
"""
from __future__ import annotations

import re
import sys
from collections import defaultdict
from pathlib import Path

# Tools whose results are not game observations (memory, wiki, harness meta).
NOT_GAME = {"Read", "Grep", "Glob", "Edit", "Write", "MultiEdit", "TodoWrite", "ToolSearch", "Task", "Agent",
            "wiki_page", "wiki_search", "note", "session_clock", "screenshot", "zoom",
            "memory_list", "memory_read", "memory_search", "memory_write", "memory_edit", "memory_append",
            "session_start", "end_session"}

_GUID = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b")
_FILE_PATH = re.compile(r"(?:/[\w.\-]+)+\.(?:jpg|png|jsonl?|txt)")
_DC = re.compile(r"\b(?:DC ?\d+|dc=\d+)")
# Engine status ids (status=SCL_MOONSHIELD, statuses=[id=MAG_..._BUFF]): the UI shows only the name.
_STATUS_ID = re.compile(r"\b(?:status|id)=([A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+)\b")
# Template / resource / flag names (S_GOB_DrowCommander, LOOT_..., Quest flags). Spell and action ids
# (Target_/Projectile_/Shout_...) are the agent's own action vocabulary. WAYP_ ids and level ids
# (WLD_Main_A) are LEAKs since 2026-10-10 (#8): get_map/level_changed show the map's names and
# fast_travel takes them.
_ACTION_ID = re.compile(r"\b(?:Target|Projectile|Shout|Zone|Throw|Rush|Wall|Teleportation|Cone|Multicast|Storm"
                        r"|Interrupt|Passive)_\w+")
_TEMPLATE = re.compile(r"\b(?:S|TUT|DEN|GOB|CHA|FOR|UND|CRE|MOO|BGO|LOW|WYR|TWN|MAG|LOOT|ORI|QUEST|GLO|HAG|BLD"
                       r"|FX|CINE|DLG|FLAG|GEN|CAMP|SHA|COL|END)_[A-Za-z0-9]+(?:_[A-Za-z0-9]+)*\b")
_JUDGED_ID = re.compile(r"\b(?:WAYP_[A-Za-z0-9_]+|[A-Z]{3}_Main_[A-Z])\b")
_FLAG_WORDS = re.compile(r"\b(?:story ?flag|dialog(?:ue)? ?resource|DialogResource|questflag|GlobalFlag)\b", re.I)
# A listing line about an entity (handle e12) that names a hidden state.
_HIDDEN_STATE = re.compile(r"\b(?:invisible|hidden|sneaking|stealthed|ambushing)\b", re.I)
_HANDLE = re.compile(r"\be\d+\b")
# The roll screen shows the DC: roll_check results and ROLL/roll={...} data are fair.
_ROLL_SCREEN = re.compile(r"^\s*ROLL\b|\broll=\{|\bphase=(?:intro|result|reroll)\b|context=DialogCheck")
# The agent's own memory printed back (LESSONS / LAST ATTEMPT / RETRY lines, "- WHEN …: …" lesson rows): a DC the agent
# read off a roll screen and wrote down is not a leak (S051-S055: 13+ false "dc_outside_roll" hits, all
# "- WHEN Aelis …: failed Deception DC18 on Aelis started a fight").
_MEMORY_LINE = re.compile(r"^\s*(?:-\s*)?WHEN\b|\bLESSONS?\b|\bLAST ATTEMPT\b|^\s*RETRY\b")
# (Traps: listings tag "trap" only once the engine says it's discovered, so a text rule can't tell; the
# mod's gate is tested offline: tools/vis/test_route_lua.lua "combat: an undiscovered trap ...".)
_ROLL_SUBJECT = re.compile(r"roll_result[^|\n]*\b(?:subject|event|vs)=")

LEAK, CHECK = "LEAK", "CHECK"
RULES = {  # rule -> (severity, what it means)
    "guid": (LEAK, "raw GUID in a result (scrub missed it)"),
    "dc_outside_roll": (LEAK, "a DC outside the roll screen"),
    "roll_subject": (LEAK, "a passive roll's subject/event"),
    "flag": (LEAK, "story flag / dialogue resource named"),
    "template": (LEAK, "internal template/resource name"),
    "status_id": (LEAK, "engine status id (the UI shows only the name; fixed 2026-10-10)"),
    "hidden_state": (CHECK, "hidden-state word on an entity line (invisible/sneaking/ambushing)"),
    "judged_id": (LEAK, "waypoint/level id (the map shows names; fixed 2026-10-10)"),
}


def _lines(text: str) -> list[str]:
    return text.replace("\\n", "\n").split("\n")


def audit_calls(calls: list[dict]) -> dict[str, list[tuple[dict, str]]]:
    """{rule: [(call, snippet), ...]} over the calls' result texts, in call order."""
    hits: dict[str, list[tuple[dict, str]]] = defaultdict(list)

    def hit(rule: str, call: dict, line: str, m: re.Match) -> None:
        a, b = max(0, m.start() - 70), min(len(line), m.end() + 50)
        hits[rule].append((call, line[a:b].strip()))

    for c in calls:
        name = c.get("name") or ""
        text = c.get("text") or ""
        if name in NOT_GAME or not text:
            continue
        for line in _lines(text):
            clean = _FILE_PATH.sub("", line)
            for m in _GUID.finditer(clean):
                hit("guid", c, clean, m)
            if name != "roll_check" and not _ROLL_SCREEN.search(clean) and not _MEMORY_LINE.search(clean):
                for m in _DC.finditer(clean):
                    hit("dc_outside_roll", c, clean, m)
            for m in _ROLL_SUBJECT.finditer(clean):
                hit("roll_subject", c, clean, m)
            for m in _FLAG_WORDS.finditer(clean):
                hit("flag", c, clean, m)
            noact = _ACTION_ID.sub("", clean)
            for m in _STATUS_ID.finditer(noact):
                hit("status_id", c, noact, m)
            nojudged = _JUDGED_ID.sub("", _STATUS_ID.sub("", noact))
            for m in _TEMPLATE.finditer(nojudged):
                hit("template", c, nojudged, m)
            for m in _JUDGED_ID.finditer(noact):
                hit("judged_id", c, noact, m)
            if not re.search(r"spotted|status_removed|dialogue_line|text=|^\s*\d+\. ", clean):
                if _HANDLE.search(clean) and not re.search(r"Potion of Invisibility|name=Invisib", clean):
                    for m in _HIDDEN_STATE.finditer(clean):
                        hit("hidden_state", c, clean, m)
    return hits


def section(calls: list[dict], hm=lambda t: "--:--:--", examples: int = 3) -> list[str]:
    """Markdown lines for triage: one row per rule that fired, with a few examples."""
    hits = audit_calls(calls)
    leaks = sum(len(v) for k, v in hits.items() if RULES[k][0] == LEAK)
    checks = sum(len(v) for k, v in hits.items() if RULES[k][0] == CHECK)
    out = [f"## Fairness audit: {leaks} leak(s), {checks} to check", ""]
    if not hits:
        return out + ["Nothing a player couldn't see was found in the game-tool results.", ""]
    for rule in sorted(hits, key=lambda k: (RULES[k][0] != LEAK, -len(hits[k]))):
        sev, what = RULES[rule]
        rows = hits[rule]
        tools = sorted({c.get("name") for c, _ in rows})
        out.append(f"- **{sev} {rule}** ×{len(rows)}: {what} (tools: {', '.join(tools)})")
        seen: set[str] = set()
        for c, snip in rows:
            key = re.sub(r"\d+", "#", snip)
            if key in seen:
                continue
            seen.add(key)
            out.append(f"  - {hm(c.get('t'))} {c.get('name')}: `{snip.replace('`', chr(39))}`")
            if len(seen) >= examples:
                break
    return out + [""]


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import triage  # noqa: E402

    for arg in sys.argv[1:]:
        base = Path(re.sub(r"\.(jsonl|log|triage\.md)$", "", arg))
        calls, _ = triage.load_calls(Path(f"{base}.jsonl"), Path(f"{base}.turns.jsonl"))
        print(f"# {base.name}")
        print("\n".join(section(calls, triage.hm)))

"""Tool-result compaction: say each thing once, short (the model side of live-feel latency).

Every agent turn re-reads every earlier tool result (the context is one growing prompt), so result size drives
time-to-first-token and cost. The naut series (research/impl-vis.md "Observation size") showed ~20 % of result
text repeating verbatim what an earlier result said (hints, the party/alert lines, the clock, legends) and much of
the rest spent on syntax: `{name=Lae'zel e29}`, `type= amount=0`, an engine spell id next to its display name.

`Compactor.apply(tool, text)` rewrites a finished result (server._guard, once per top-level call) with rules that
drop no fact a player sees:
* shorter syntax for the same facts (REFS, EMPTIES, CAST_IDS, NUMBERS, GONE_GROUP, REPEATS, MERGE_BLOCKS);
* combat text (the naut gap, research/impl-vis.md "combat compaction"): an actor's cast and its damage on one line
  with its turn start (COMBAT_NARRATIVE, TURN_ENDS, TURN_WORDS), MOVE_PHRASES / COMBAT_PHRASES / ACTION_DUPS
  (COMBAT_MOVE_POS, no end positions on combat move lines, is off), the spent turn's leftover resources
  (ENDED_RESOURCES), RESOURCES_LINE, TURN_NAMES, NEXT_ORDER, KIND_GROUP;
* text this session already showed in full, said again only as a short form: hints after HINT_FULL_TIMES
  showings, the advisory ALERTS/PARTY lines while unchanged ("(unchanged)"), the layout legend, a clock line
  identical to the last one, a dialogue line the same result (or the previous one) already quoted, CHAR/ITEM
  listing lines shown in full in the last minute (BLOCK_AGAIN).
Each rule has its own constant; BG3AI_COMPACT=0 turns all of it off, BG3AI_COMPACT_OFF=RULE1,RULE2 single rules
(both read at bridge start; unknown names are warned in the log). `self.elided` lists, per call, the text a
rule left out because the agent saw it before: tools/vis/obs_size.py's fact checker verifies each was shown
verbatim earlier and that every fact of the original result survives in the result or in those fragments.
"""

from __future__ import annotations

import logging
import os
import re
import time

log = logging.getLogger(__name__)

COMPACT = os.environ.get("BG3AI_COMPACT", "1") != "0"

REFS = True          # {name=Lae'zel e29} / {e29 name=Lae'zel} / name=Lae'zel e29 -> e29 Lae'zel
EMPTIES = True       # "type= amount=0" -> "amount=0"; "combat_started []" -> "combat_started"
CAST_IDS = True      # an engine spell id next to the display name (cast events; action: X ... cast=X)
NUMBERS = True       # hp=21 max_hp=24 -> hp=21/24; damage=[target=e33 type=Slashing name=Imp amount=7] -> [e33 Imp 7 Slashing]
GONE_GROUP = True    # GONE e16 Pod, e18 Pod, e19 Pod -> GONE e16/e18/e19 Pod
SAME_GROUP = True    # "  + e80 Dead Thrall neutral dead 16.8m" x3 -> "  + Dead Thrall neutral dead: e80 16.8m, e82 19.2m, ..";
                     # "ITEM + e44 Camp Supply Sack (Lae'zel)" x10 -> "ITEM + (Lae'zel): e44 Camp Supply Sack, .."
KIND_GROUP = True    # CHANGES "+" lines of one tag (<item>, <usable>, <door>, neutral dead, ..) on one line:
                     # "  + e113 Mucoid Shell <item> 22.9m" / "  + e114 Caustic Bulb <item> 23.2m" ->
                     # "  + <item>: e113 Mucoid Shell 22.9m, e114 Caustic Bulb 23.2m" (living characters keep their rows)
REPEATS = True       # the same line twice in a row -> "line (x2)"
MERGE_BLOCKS = True  # CHANGES ... CHANGES (back to back) -> one block; same for EVENTS
HINTS = True         # instructions in results: full text HINT_FULL_TIMES times per session, then the short form
HINT_FULL_TIMES = 1
STATIC = True        # ALERTS / PARTY: advisory lines: "(unchanged)" while identical, in full again after STATIC_RESHOW_S
STATIC_RESHOW_S = 600.0
CLOCK = True         # a "⏱ 16:38 · 25m left" line identical to the last one shown is left out
LAYOUT_LEGEND = True  # get_layout's symbol key in full once, then "key as before"
DIALOGUE_DEDUP = True  # dialogue_line events quoting a line this result (or the last one) already shows
EMPTY_LINES = True   # "ALSO " with nothing after it
EVENT_LINES = True   # damage target=e2 X source=e34 Imp type=Fire amount=3 -> damage e34 Imp -> e2 X 3 Fire (fixed kinds)
NAMES_ONCE = True    # in EVENTS, a handle named earlier in the same result is written without its name again
QUEST_DEDUP = True   # a quest text this session already showed verbatim -> "(text as before)"
PHRASES = True       # shorter wording of fixed phrases (PHRASE_RULES), every time
COMBAT_ROWS = True   # "initiative=4 hp_pct=100 e28 can_act @(..) name=Lae'zel from={e2=[1.6,1]} relation=party"
                     # -> "e28 Lae'zel party init 4 hp 100% can_act @(..) from e2 1.6m" (los 0 -> "no-LOS")
COORD_DECIMALS = 0   # positions reported as "@(x,y,z)" (where someone got to) rounded to this many decimals;
                     # None keeps them. Positions offered as targets ("closest walkable spot (x,y,z)") keep 0.1 m.
COORD_OWN = True     # ..except the acting character's own position (a tool's own outcome line: "interact: done ..
                     # @(..)", "move ..", "leg 1: ..", "2. jump_to: ..", "STOPPED: ..", job_finished): it keeps 0.1 m
                     # (naut: jump_to targets and lesson notes were built from it; research/impl-vis.md audit)
DUP_FIELDS = True    # "move e32: done — target=e32", "total_damage=9" next to the one damage=[.. 9 ..]
EVENTS_VS_CHANGES = True  # "died e32" / "status_applied e2: Burning" when CHANGES of the same result says DEAD / +Burning;
                          # handles CHANGES already named lose their names in EVENTS (NAMES_ONCE)
ACTIONS_AGAIN = True  # a YOUR ACTIONS list identical to one shown before for that member -> "(actions as listed before)"
ACTIONS_MIN_LINES = 8  # only whole lists (a short spent-action variant is cheaper to repeat than to look up)
# Harness bookkeeping a player never sees (the NOW line shows what is left): the guard's charge per action, and a
# finished walk leg's end position + arrival error when a later leg follows (the last position stays).
HARNESS_FIELDS = True
PATH_LEGS = True     # route's "  1. 17.6m W" / "  2. 19.5m W" -> "  1-2. 17.6m, 19.5m W" (same heading, nothing else)
# --- combat (combat_turn / end_turn / wait_for text in a fight) ---
COMBAT_NARRATIVE = True  # EVENTS: an actor's cast and the damage it dealt on one line, its turn_started folded in:
                         # "turn_started e86 / damage e86 -> e67 12 Slashing / damage e86 -> e67 3 Fire / cast e86: Main
                         # Hand Attack (attack)" -> "turn e86: Main Hand Attack (attack) -> e67 12 Slashing + 3 Fire"
TURN_ENDS = True     # "turn_ended X" in an EVENTS block that starts a turn (each end is followed by the next start),
                     # unless it is the only line of the result naming one of its handles
TURN_WORDS = True    # "turn_started e28; e2" -> "turn e28; e2", "round_started 2" -> "round 2"
COMBAT_MOVE_POS = False  # True: no @(x,y,z) on the move lines of a combat turn (CHANGES says who moved how far).
                         # Off since the naut audit: the agent built combat waypoints from those end positions
MOVE_PHRASES = True  # "FAILED — beyond movement (path 7.6 m, 1.5 m left) → partial 1.2m: done — .. short of ≤1.5m: out
                     # of movement (1.5m left, route needs 8.5m)" -> "beyond movement (..) → partial 1.2m: done — ..
                     # short of ≤1.5m, route needs 8.5m" (the partial walk's outcome stays; "left" said once)
COMBAT_PHRASES = True  # "end_turn: done — requested still_to_act=[e2] ended" -> "end_turn: done (still to act: e2)"
ACTION_DUPS = True   # "→ Projectile_FireBolt e68: done — cast=Projectile_FireBolt"; "target=e32 dead damage=[e32 ..]";
                     # "statuses=[target=e2 status=Dash]" -> "statuses=[e2 Dash]"; a damage list names its target once
                     # -> "damage=[e32 ..] dead"
ENDED_RESOURCES = True  # "NOW e28 hp=24/24 action=0 bonus=1 movement=0.3 reaction=1" right after "end_turn: done": the
                        # spent turn's leftover action/bonus/movement (gone from the HUD once the turn passes) is not
                        # said; hp and the reaction (usable in the enemies' turns) stay: "NOW e28 hp=24/24 reaction=1"
RESOURCES_LINE = True  # "YOUR ACTIONS (e2)" + "NOW Action 1/1 · Bonus 1/1 · Move 9/9m · Reaction 1/1" -> one line
                       # "YOUR ACTIONS (e2) NOW full: Action 1 · Bonus 1 · Move 9m · Reaction 1" (full when all n/n);
                       # the same full line as that member's last one -> "YOUR ACTIONS (e2) NOW full (as last turn)"
TURN_NAMES = True    # "NOW e2 Corran Vell's turn" when the result already said "turn=e2 Corran Vell" -> "NOW e2's turn"
NEXT_ORDER = True    # NEXT turn order: a combatant named earlier in the result is its handle alone; the same order as
                     # the last NEXT shown -> "NEXT (order as before), then e28 Lae'zel (yours)"
BLOCK_AGAIN = True   # CHAR/ITEM listing lines shown in full in the last BLOCK_AGAIN_S (get_nearby right after a
                     # look full=true, live 20261016: the same 13 lines) -> "(same as the look a moment ago: 13 lines)";
                     # lines that changed (a distance, a new one) stay in full under it
BLOCK_AGAIN_S = 60.0
BLOCK_AGAIN_MIN_LINES = 4  # fewer same lines are cheaper to repeat than to look up
_BLOCK_PREFIXES = ("CHAR ", "ITEM ")
# Results quoting files or pages (markdown the agent or the wiki wrote): only the clock rule applies.
PROSE_TOOLS = {"memory_read", "memory_search", "memory_list", "memory_write", "memory_edit", "memory_append",
               "wiki_page", "wiki_search", "recall", "session_start", "end_session"}

_H = r"[eo]\d+"
_NAME = r"[^{}=;,:\[\]\n]+?"

# (key, pattern, short form). The pattern matches only the instruction text; named groups hold facts and the short
# form keeps every one of them (tests/test_compact.py checks it). Matched spans are not facts (the checker skips them).
HINT_RULES: list[tuple[str, str, str]] = [
    ("early_wake", r"\(early wake, lead_s=(?P<lead>[\d.]+)\): plan the next step now\. What you issue(?: for [^\n]*?)?"
                   r" runs after the walk arrives \(it does not cancel it\)(?:; wait_for again blocks to the end)?\.",
     r"(early wake, lead_s=\g<lead>): plan the next step; it runs after the walk."),
    ("next_walk", r"NEXT: queue_actions what to do on arrival, then look/plan/notes while it runs; wait_for only when "
                  r"you are out of work", ""),
    ("notes_window", r"NOTES WINDOW: ~(?P<s>[\d.]+)s of walking left: note\(\) what happened / plan now \(not after it "
                     r"ends, when the character would stand idle\); queue_actions the arrival first",
     r"NOTES WINDOW: ~\g<s>s of walking left: note/plan now"),
    ("appends", r" \(queue_actions appends after it\)", ""),
    ("pace", r"PACE: you waited right after issuing\. Next time first do independent work \(queue the arrival steps, "
             r"look, plan, notes\): the character acts while you think\.",
     "PACE: you waited right after issuing (do independent work first)."),
    ("actions_before", r"\(other actions as listed before for (?P<h>e\d+); get_combat_state lists them all\)",
     r"(other actions of \g<h> as before)"),
    ("plan_while", r"\. Plan it while they act and send its combat_turn now \(it waits for the turn\)",
     ": send its combat_turn now"),
    ("combat_started", r"COMBAT STARTED \(no get_combat_state needed\):", "COMBAT STARTED:"),
    ("no_change", r"No change since your last look \(full=true shows everything\)\.", "No change since your last look."),
    ("saving", r" in the background \(~2 s with the game's save freeze; your next call waits for them\)\. Keep playing; "
               r"SAVED shows on a later result\.", " in the background; SAVED shows later."),
    ("shared_turn", r" can all act now, in any order \(combat_turn who=\.\.\., end each one's turn\)",
     " can all act now, any order"),
    ("more_nearby", r"; get_nearby / get_frontier\)", ")"),
    ("more_items", r" item changes; get_inventory\)", " item changes)"),
    ("plan_them", r" — plan them from this state \(combat_turn again\)\.", " — plan them (combat_turn again)."),
    ("retarget", r" \(retarget:false keeps it\)", ""),
    ("climb", r" \[interact to climb\]", ""),
    ("sell_junk", r" \(sell junk: trade sell=junk\)", ""),
    ("combat_over", r"combat is over: loot, heal, then end_combat_report", "combat is over"),
    ("floors_rel", r" \(m, relative to you\)", ""),
    ("walks_it", r"^NEXT: travel_to/move_to walks it$", ""),
    ("flapping", r" note=on/off on 3\+ others nearby \(aura/light\): muted for non-party", " (muted)"),
    ("hud_end", r"(?:'s End Turn)? \(the game ignored the order\)", ""),
    ("plan_it", r"(?<=\S)'s turn: plan it$", "'s turn"),
]
_HINTS = [(k, re.compile(p, re.M), r) for k, p, r in HINT_RULES]

# Fixed wording said shorter every time (no facts in the patterns; groups are kept).
PHRASE_RULES: list[tuple[str, str]] = [
    (r"destination is beyond remaining movement \(", "beyond movement ("),
    (r" toward it: ", ": "),
    (r"stopped short of ", "short of "),
    (r"the route needs ", "route needs "),
    (r" — melee is out of reach this turn", " — no melee this turn"),
    (r"the plan now aims at the nearest visible hostile", "nearest visible hostile"),
    (r"ended via=the HUD's End Turn \(the game ignored the order\)", "ended via the HUD (the game ignored the order)"),
    (r"(DEAD), no longer in_combat", r"\1"),
    (r"turn NOT ended: decide, then continue \(combat_turn again, or single tools\)", "turn NOT ended: decide, then continue"),
]
_PHRASES_BASE = [(re.compile(p), r) for p, r in PHRASE_RULES]

# Combat wording said shorter every time (COMBAT_PHRASES; same contract as PHRASE_RULES).
COMBAT_PHRASE_RULES: list[tuple[str, str]] = [
    (r"^(end_turn: done) — requested still_to_act=\[([^\]]*)\] ended$", r"\1 (still to act: \2)"),
    (r"^(target: e\d+ [^→\n]*?) is dead → ", r"\1 dead → "),
    (r"^(end_turn: done) — still_to_act=\[([^\]]*)\] ended requested$", r"\1 (still to act: \2)"),
]
_COMBAT_PHRASES = [(re.compile(p, re.M), r) for p, r in COMBAT_PHRASE_RULES]


def phrases() -> list:
    """The active (pattern, short form) rewrites: PHRASES and COMBAT_PHRASES."""
    return (_PHRASES_BASE if PHRASES else []) + (_COMBAT_PHRASES if COMBAT_PHRASES else [])


_PHRASES = _PHRASES_BASE

_LAYOUT_RE = re.compile(r"^(LAYOUT [^;\n]*; )(# wall .*)$")
_CLOCK_RE = re.compile(r"^⏱ \d\d:\d\d · \d+m left$")
_STATIC_PREFIXES = ("ALERTS ", "PARTY: ")
_HEADERS = ("CHANGES", "EVENTS")


def enabled() -> bool:
    return COMPACT


# ---------------------------------------------------------------------------
# line rewrites (same facts, fewer tokens)
# ---------------------------------------------------------------------------

_REF_BRACE_A = re.compile(r"\{name=(" + _NAME + r") (" + _H + r")\}")
_REF_BRACE_B = re.compile(r"\{(" + _H + r") name=(" + _NAME + r")\}")
_REF_BARE_A = re.compile(r"\bname=(" + _NAME + r") (" + _H + r")(?=[;\]}\n]|$)")
_REF_BARE_B = re.compile(r"\b(" + _H + r") name=")
_REF_ONLY = re.compile(r"=\{(" + _H + r")\}")


def refs(line: str) -> str:
    line = _REF_BRACE_A.sub(r"\2 \1", line)
    line = _REF_BRACE_B.sub(r"\1 \2", line)
    line = _REF_BARE_A.sub(r"\2 \1", line)
    line = _REF_BARE_B.sub(r"\1 ", line)
    return _REF_ONLY.sub(r"=\1", line)


_EMPTY_KV = re.compile(r" [a-z_]+=(?= [a-z_]+=|$)")   # an empty value before the next key or the end: not a hint's
# "narrow with radius= or kinds=)" (claude-run-2 S030/S058 replay: it came out as "narrow with or kinds=")
_EMPTY_LIST = re.compile(r"^(\s+[a-z_]+) \[\]$")


def empties(line: str) -> str:
    line = _EMPTY_KV.sub("", line)
    return _EMPTY_LIST.sub(r"\1", line)


_ACTION_CAST = re.compile(r"^(action: (\S+) .*?: done — )cast=\2 ")


def cast_ids(line: str) -> str:
    if line.startswith("  cast ") and " name=" in line:
        line = re.sub(r" spell=\S+", "", line)
    return _ACTION_CAST.sub(r"\1", line)


_HP = re.compile(r"\bhp=(\d+) max_hp=(\d+)")
_GUARD = re.compile(r"charged_by_guard=\[(?:amount=(\d+) resource=([\w ]+?)|resource=([\w ]+?) amount=(\d+))\]")
_DMG = re.compile(r"damage=\[([^\[\]]*)\]")
_DMG_KEYS = {"target", "type", "name", "amount"}


def _damage_entry(entry: str) -> str | None:
    kv = {}
    for part in re.finditer(r"(\w+)=([^=]*?)(?= \w+=|$)", entry.strip()):
        kv[part.group(1)] = part.group(2)
    if not kv or set(kv) - _DMG_KEYS or "amount" not in kv:
        return None
    return " ".join(x for x in (kv.get("target"), kv.get("name"), kv.get("amount"), kv.get("type")) if x)


def _damage(m: re.Match) -> str:
    parts = [_damage_entry(e) for e in m.group(1).split("; ")]
    if any(p is None for p in parts):
        return m.group(0)
    return "damage=[" + "; ".join(parts) + "]"


def numbers(line: str) -> str:
    line = _HP.sub(r"hp=\1/\2", line)
    line = _GUARD.sub(lambda m: f"charged_by_guard=[{m.group(2) or m.group(3)} {m.group(1) or m.group(4)}]", line)
    return _DMG.sub(_damage, line)


_GONE = re.compile(r"^(\s*GONE )(.*)$")


def gone_group(line: str) -> str:
    m = _GONE.match(line)
    if not m:
        return line
    items = m.group(2).split(", ")
    order: list[str] = []
    by_name: dict[str, list[str]] = {}
    for it in items:
        hm = re.fullmatch(r"(" + _H + r") (.+)", it)
        if not hm:
            return line
        if hm.group(2) not in by_name:
            order.append(hm.group(2))
            by_name[hm.group(2)] = []
        by_name[hm.group(2)].append(hm.group(1))
    return m.group(1) + ", ".join("/".join(by_name[n]) + " " + n for n in order)


_AT = re.compile(r"@\((-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?)\)")


def _round(v: str, nd: int) -> str:
    x = round(float(v), nd)
    t = f"{x:.{nd}f}" if nd else str(int(x))
    return "0" if t in ("-0", "-0.0") else t


# a tool's own outcome line (where the acting character got to), not a row about someone else
_OWN_POS = re.compile(r"^(?:\d+\. )?(?:move |leg \d|[a-z_]+(?: e\d+[^:\n]*)?: |STOPPED: )|^\s+job_finished ")


def coords(line: str, nd: int | None = None) -> str:
    nd = COORD_DECIMALS if nd is None else nd
    if nd is None or COORD_OWN and _OWN_POS.match(line):
        return line
    return _AT.sub(lambda m: "@(" + ",".join(_round(v, nd) for v in m.groups()) + ")", line)


_MOVE_TARGET = re.compile(r"^(move (e\d+): .*?) target=\2\b")
_DEAD_REF = re.compile(r"=\{(?:(e\d+) (dead|downed)|(dead|downed) (e\d+))\}")
_TOTAL = re.compile(r" total_damage=(\d+)(?=.*damage=\[[^;\]]* \1(?: \w+)?\])|(?<=damage=\[)([^;\]]* (\d+)(?: \w+)?)\](.*?) total_damage=\3\b")


def dup_fields(line: str) -> str:
    line = _MOVE_TARGET.sub(r"\1", line)
    line = _DEAD_REF.sub(lambda m: "=" + (f"{m.group(1)} {m.group(2)}" if m.group(1) else f"{m.group(4)} {m.group(3)}"),
                         line)
    m = re.search(r" total_damage=(\d+)", line)
    d = re.findall(r"damage=\[([^\]]*)\]", line)
    if m and len(d) == 1 and ";" not in d[0] and re.search(r"(?:^| )" + m.group(1) + r"(?: \w+)?$", d[0]):
        line = line[:m.start()] + line[m.end():]
    return line


# --- combatant rows under COMBAT -------------------------------------------------------------------------------
_ROW_KEYS = ("initiative", "hp_pct", "name", "race", "relation", "from")
_ROW_TOK = re.compile(r"from=\{[^}]*\}|@\([^)]*\)|[a-z_]+=(?:\[[^\]]*\]|\{[^}]*\}|\S+)|\S+")
_ROW_FLAGS = {"can_act", "is_current", "acted", "ended", "dead", "downed", "unseen", "summon", "hidden", "invisible"}


def combat_row(line: str) -> str:
    if not line.startswith("  ") or "initiative=" not in line or "relation=" not in line:
        return line
    kv: dict[str, str] = {}
    flags, extra, handle, pos = [], [], None, None
    last = None
    for tok in _ROW_TOK.findall(line.strip()):
        m = re.match(r"^([a-z_]+)=(.*)$", tok)
        if m and not tok.startswith("@"):
            kv[m.group(1)] = m.group(2)
            last = m.group(1) if m.group(1) in ("name", "race") else None
        elif tok.startswith("@("):
            pos, last = tok, None
        elif re.fullmatch(r"e\d+", tok) and handle is None:
            handle = tok
            last = "name" if "name" not in kv else None  # "e27 Lae'zel" (refs() already folded name=)
            if last:
                kv["name"] = ""
        elif tok in _ROW_FLAGS:
            flags.append("current" if tok == "is_current" else tok)
            last = None
        elif last:
            kv[last] = (kv[last] + " " + tok).strip()
        else:
            extra.append(tok)
    if handle is None or set(kv) - set(_ROW_KEYS) - {"hp", "max_hp"}:
        return line
    name = kv.get("name", "")
    out = [handle] + ([name] if name else [])
    if kv.get("race") and kv["race"] != name:
        out.append(f"({kv['race']})")
    if kv.get("relation"):
        out.append(kv["relation"])
    if "initiative" in kv:
        out.append(f"init {kv['initiative']}")
    if "hp_pct" in kv:
        out.append(f"hp {kv['hp_pct']}%")
    out += [f"{k}={kv[k]}" for k in ("hp", "max_hp") if k in kv]
    out += flags + extra
    if pos:
        out.append(pos)
    fr = kv.get("from", "")
    pairs = re.findall(r"(e\d+)=\[(-?[\d.]+),([01])\]", fr)
    if pairs:
        out.append("from " + ", ".join(f"{h} {d}m" + ("" if los == "1" else " no-LOS") for h, d, los in pairs))
    elif fr:
        out.append(f"from={fr}")
    return "  " + " ".join(out)


_GUARD_FIELD = re.compile(r" (?:movement_)?charged_by_guard=(?:\[[^\]]*\]|\S+)")


def harness_fields(lines: list[str]) -> list[str]:
    out = [_GUARD_FIELD.sub("", ln) for ln in lines]
    for i, ln in enumerate(out[:-1]):
        if ln.startswith("leg ") and out[i + 1].startswith("leg ") and ": done" in ln:
            out[i] = re.sub(r" dist=[\d.]+", "", re.sub(r" @\([^)]*\)", "", ln))
    return out


_LEG = re.compile(r"^  (\d+)(?:-(\d+))?\. ((?:[\d.]+m, )*[\d.]+m) ([NESW]{1,2})$")


def path_legs(lines: list[str]) -> list[str]:
    out: list[str] = []
    for ln in lines:
        m = _LEG.match(ln)
        p = _LEG.match(out[-1]) if out and m else None
        if p and p.group(4) == m.group(4) and int(m.group(1)) == int(p.group(2) or p.group(1)) + 1:
            out[-1] = f"  {p.group(1)}-{m.group(1)}. {p.group(3)}, {m.group(3)} {m.group(4)}"
            continue
        out.append(ln)
    return out


def _rewrite(line: str) -> str:
    if REFS:
        line = refs(line)
    if EMPTIES:
        line = empties(line)
    if CAST_IDS:
        line = cast_ids(line)
    if NUMBERS:
        line = numbers(line)
    if GONE_GROUP:
        line = gone_group(line)
    if DUP_FIELDS:
        line = dup_fields(line)
    if COMBAT_ROWS:
        line = combat_row(line)
    line = coords(line)
    for rx, r in phrases():
        line = rx.sub(r, line)
    if MOVE_PHRASES:
        line = move_phrases(line)
    return line


def _merge_blocks(lines: list[str]) -> list[str]:
    out: list[str] = []
    block = None  # header of the block the last line belongs to
    for ln in lines:
        if ln in _HEADERS:
            if block == ln:
                continue
            block = ln
        elif not ln.startswith("  "):
            block = None
        out.append(ln)
    return out


def _repeats(lines: list[str]) -> list[str]:
    out: list[str] = []
    n = 1
    for i, ln in enumerate(lines):
        if i + 1 < len(lines) and lines[i + 1] == ln and ln.strip() and ln not in _HEADERS:
            n += 1
            continue
        out.append(f"{ln} (x{n})" if n > 1 else ln)
        n = 1
    return out


# --- EVENTS lines: "  kind key=value key=value bare" (values may hold spaces; refs already "e2 Name") ---------
_KV = re.compile(r"(?:^| )([a-z_]+)=(.*?)(?= [a-z_]+=|$)")
_REF = re.compile(r"^(" + _H + r") (.+)$")


_FLAGS = re.compile(r"(?:^| )(success|failed)(?= |$)")


def _parse_event(rest: str) -> tuple[dict[str, str], str]:
    flags = " ".join(_FLAGS.findall(rest))
    rest = _FLAGS.sub("", rest).strip()
    kv = {m.group(1): m.group(2) for m in _KV.finditer(rest)}
    bare = (_KV.sub("", rest).strip() + " " + flags).strip()
    return kv, bare


class _Names:
    """Handles already named in this result: later mentions are the handle alone (NAMES_ONCE)."""

    def __init__(self):
        self.named: set[str] = set()

    def __call__(self, value: str) -> str:
        parts = []
        for v in value.split("; "):
            m = _REF.match(v)
            if m and NAMES_ONCE:
                if m.group(1) in self.named:
                    v = m.group(1)
                else:
                    self.named.add(m.group(1))
            parts.append(v)
        return "; ".join(parts)


def event_line(line: str, names: _Names) -> str:
    m = re.match(r"^(\s+)([a-z_]+)(?: (.*))?$", line)
    if not m:
        return line
    ind, kind, rest = m.group(1), m.group(2), m.group(3) or ""
    kv, bare = _parse_event(rest)
    keys = set(kv)
    out = None
    if kind == "damage" and keys <= {"target", "source", "type", "amount"} and "amount" in kv and not bare:
        src = names(kv["source"]) + " -> " if kv.get("source") else ""
        out = f"damage {src}{names(kv.get('target', '?'))} {kv['amount']}" + (f" {kv['type']}" if kv.get("type") else "")
    elif kind == "cast" and keys <= {"caster", "roll", "name", "spell"} and "name" in kv and not bare:
        out = (f"cast {names(kv.get('caster', '?'))}: {kv['name']}" + (f" ({kv['roll']})" if kv.get("roll") else ""))
    elif kind == "used" and keys <= {"item", "char"} and "item" in kv:
        out = f"used {names(kv.get('char', '?'))}: {names(kv['item'])}" + (f" {bare}" if bare else "")
    elif kind in ("status_applied", "status_removed") and keys == {"target", "name"} and not bare:
        out = f"{kind} {names(kv['target'])}: {kv['name']}"
    elif kind == "item_added" and keys <= {"item", "owner", "count"} and "item" in kv and not bare:
        out = (f"item_added {names(kv.get('owner', '?'))}: {names(kv['item'])}"
               + (f" count={kv['count']}" if kv.get("count") else ""))
    elif kind == "round_started" and keys == {"round"} and not bare:
        out = f"round_started {kv['round']}"
    elif not kv and bare and _REF.match(bare.split("; ")[0]):  # turn_started e28 Lae'zel; e2 Name / died e8 Name
        out = f"{kind} {names(bare)}"
    return ind + out if out is not None else line


_STATUS_CHANGE = re.compile(r"(?:^|[ ,])([+-])([A-Z][\w' ]*?)(?=,| [+-][A-Z]|$)")
_CHANGE_HANDLE = re.compile(r"^\s+(?:~ |\+ |PARTY \+ )?(e\d+) ")
_NAMED = re.compile(r"\b(e\d+) [A-Z]")


def _changes_facts(lines: list[str]) -> tuple[set[str], set[tuple[str, str, str]], set[str]]:
    """(handles CHANGES calls DEAD, {(handle, +/-, status)}, handles named outside EVENTS) of one result."""
    dead, sts, named = set(), set(), set()
    block = None
    for ln in lines:
        if ln in _HEADERS:
            block = ln
            continue
        if not ln.startswith("  "):
            block = None
        if block != "EVENTS":
            named.update(_NAMED.findall(ln))
        if block == "CHANGES":
            m = _CHANGE_HANDLE.match(ln)
            if m:
                if re.search(r"\bDEAD\b", ln):
                    dead.add(m.group(1))
                for sign, st in _STATUS_CHANGE.findall(ln[m.end():]):
                    sts.add((m.group(1), sign, st.strip()))
    return dead, sts, named


_EV_DIED = re.compile(r"^\s+died (e\d+)(?: [^;]*)?$")
_EV_STATUS = re.compile(r"^\s+status_(applied|removed) (e\d+)(?: [^:]*)?: (.+)$")


def _events(lines: list[str]) -> list[str]:
    names = _Names()
    dead, sts, named = _changes_facts(lines) if EVENTS_VS_CHANGES else (set(), set(), set())
    names.named |= named
    out, block = [], None
    for ln in lines:
        if ln in _HEADERS:
            block = ln
        elif not ln.startswith("  "):
            block = None
        elif block == "EVENTS":
            ln = event_line(ln, names)
            m = _EV_DIED.match(ln)
            if m and m.group(1) in dead:
                continue  # CHANGES: "~ e32 Imp DEAD"
            m = _EV_STATUS.match(ln)
            if m and (m.group(2), "+" if m.group(1) == "applied" else "-", m.group(3)) in sts:
                continue  # CHANGES: "e2 Name +Burning"
        out.append(ln)
    return [ln for j, ln in enumerate(out)
            if not (ln == "EVENTS" and (j + 1 == len(out) or not out[j + 1].startswith("  ")))]


_QUEST_TEXT = re.compile(r"^(\s+quest_update .*?text=)(.{40,}?)((?: title=.*)?)$|^(QUEST [^:\n]+: )(.{40,})$")


_PLUS = re.compile(r"^(  \+ )(e\d+) (.+?) (\d+(?:\.\d+)?m)$")
_ITEM_OWNER = re.compile(r"^(ITEM \+ )(e\d+ .+?) (\([^()]+\))$")


def _same_group(lines: list[str]) -> list[str]:
    """Lines that differ only in handle (+ distance) said once with the list (SAME_GROUP)."""
    groups: dict[tuple, list[int]] = {}
    for i, ln in enumerate(lines):
        m = _PLUS.match(ln)
        if m:
            groups.setdefault(("+", m.group(3)), []).append(i)
            continue
        m = _ITEM_OWNER.match(ln)
        if m:
            groups.setdefault(("item", m.group(3)), []).append(i)
    drop: set[int] = set()
    repl: dict[int, str] = {}
    for (kind, key), idx in groups.items():
        if len(idx) < 3:
            continue
        if kind == "+":
            parts = [f"{_PLUS.match(lines[i]).group(2)} {_PLUS.match(lines[i]).group(4)}" for i in idx]
            repl[idx[0]] = f"  + {key}: " + ", ".join(parts)
        else:
            parts = [_ITEM_OWNER.match(lines[i]).group(2) for i in idx]
            repl[idx[0]] = f"ITEM + {key}: " + ", ".join(parts)
        drop.update(idx[1:])
    return [repl.get(i, ln) for i, ln in enumerate(lines) if i not in drop]


_KIND = re.compile(r"^(\s*)\+ (e\d+ [^<\n]+?) (<[\w ]+>|(?:neutral|hostile|ally) dead)((?: [^\n]*)?)$")


def kind_group(lines: list[str]) -> list[str]:
    """KIND_GROUP over each CHANGES block (and look's unindented delta): '+' lines sharing a tag said once with the
    list (2 or more)."""
    groups: dict[tuple, list[int]] = {}
    block = -1
    for i, ln in enumerate(lines):
        if ln in _HEADERS:
            block = i
        elif not ln.startswith("  "):
            block = -1
        m = _KIND.match(ln)
        if m and (block >= 0 and lines[block] == "CHANGES" or not m.group(1)):
            groups.setdefault((block if m.group(1) else -1, m.group(1), m.group(3)), []).append(i)
    repl: dict[int, str] = {}
    drop: set[int] = set()
    for (_b, ind, tag), idx in groups.items():
        if len(idx) < 2:
            continue
        items = [_KIND.match(lines[i]) for i in idx]
        repl[idx[0]] = f"{ind}+ {tag}: " + ", ".join(m.group(2) + m.group(4) for m in items)
        drop.update(idx[1:])
    return [repl.get(i, ln) for i, ln in enumerate(lines) if i not in drop]


# an action list line: "Fire Bolt [A] 18m", "Jump [3m+B] 4.5m", "UNAVAILABLE: ...", "Main Hand Attack (id) [A] 1.5m"
_ACTION_LINE = re.compile(r"^(?:UNAVAILABLE: .*|[^\s\[][^\[\n]*\[[^\]\n]*\].*)$")


_DLG_EVENT = re.compile(r"^\s+dialogue_line speaker=(.*?) text=(.+)$")


# --- combat text (COMBAT_* rules) ---------------------------------------------------------------------------------
_TURN_HDR = re.compile(r"^TURN e\d+ ")
_AT_FIELD = re.compile(r" @\([^)]*\)")


def combat_move_pos(lines: list[str]) -> list[str]:
    """COMBAT_MOVE_POS: a combat turn's move lines without the @(x,y,z) the walk ended at."""
    if not any(_TURN_HDR.match(ln) for ln in lines):
        return lines
    return [_AT_FIELD.sub("", ln).replace("—,", "—") if ln.startswith("move ") else ln for ln in lines]


_MOVE_FAIL = re.compile(r"FAILED — ((?:destination is beyond remaining|beyond) movement \(path [\d.]+ m, [\d.]+ m left\)"
                        r" → partial [\d.]+m(?: toward it)?: )")
_PATH_LEFT = re.compile(r"\(path [\d.]+ m, ([\d.]+) m left\)")
_SHORT_LEFT = re.compile(r"((?:stopped )?short of ≤[\d.]+m): out of movement \(([\d.]+)m left, ((?:the )?route needs "
                         r"[\d.]+m)\)")


def move_phrases(line: str) -> str:
    """MOVE_PHRASES: the partial walk's outcome stands for the FAILED before it; a 'left' said twice is said once."""
    if not line.startswith("move "):
        return line
    line = _MOVE_FAIL.sub(r"\1", line)
    m = _PATH_LEFT.search(line)
    if m:
        left = float(m.group(1))
        line = _SHORT_LEFT.sub(lambda s: f"{s.group(1)}, {s.group(3)}" if float(s.group(2)) == left else s.group(0),
                               line)
    return line


_ARROW_CAST = re.compile(r"(?:^(?:action|bonus): |→ )(\w+)(?: e\d+)?: done — ")
_STATUSES = re.compile(r"statuses=\[((?:target=e\d+ status=[^;\]]+(?:; )?)+)\]")
_TARGET_DMG = re.compile(r"\btarget=(e\d+) (?:(dead|downed) )?(damage=\[\1 [^\]]*\])")


def action_dups(line: str) -> str:
    """ACTION_DUPS: cast=X beside the action X that ran; target=eN beside damage=[eN ..]."""
    for m in _ARROW_CAST.finditer(line):
        head, tail = line[:m.end()], line[m.end():]
        line = head + re.sub(r"(?:^| )cast=" + re.escape(m.group(1)) + r"(?= |$)", "", tail, count=1).lstrip(" ")
    line = _STATUSES.sub(lambda m: "statuses=[" + re.sub(r"target=(e\d+) status=", r"\1 ", m.group(1)) + "]", line)
    line = _DMG.sub(_dmg_names_once, line)
    return _TARGET_DMG.sub(lambda m: m.group(3) + (f" {m.group(2)}" if m.group(2) else ""), line)


def _dmg_names_once(m: re.Match) -> str:
    """damage=[e67 Lesser Imp 0; e67 Lesser Imp 4 Bludgeoning] -> damage=[e67 Lesser Imp 0; e67 4 Bludgeoning]"""
    seen: set[str] = set()
    parts = []
    for e in m.group(1).split("; "):
        h = re.match(r"(e\d+) ([^\d;]+?) (?=\d)", e)
        if h and h.group(1) in seen:
            e = h.group(1) + " " + e[h.end():]
        elif h:
            seen.add(h.group(1))
        parts.append(e)
    return "damage=[" + "; ".join(parts) + "]"


def implied_turn_ends(lines: list[str]) -> list[str]:
    """TURN_ENDS: in an EVENTS block that starts a turn, the turn_ended lines (each end is followed by a start)."""
    out: list[str] = []
    i = 0
    while i < len(lines):
        if lines[i] != "EVENTS":
            out.append(lines[i])
            i += 1
            continue
        j = i + 1
        while j < len(lines) and lines[j].startswith("  "):
            j += 1
        block = lines[i + 1:j]
        if any(re.match(r"^\s+turn_started\b", ln) for ln in block):
            ends = {k for k, ln in enumerate(block) if re.match(r"^\s+turn_ended\b", ln)}
            named = set(_NAMED.findall("\n".join(ln for k, ln in enumerate(lines)
                                                 if not i < k <= j - 1 or k - i - 1 not in ends)))
            keep = set()
            for k in sorted(ends):  # the only line naming a handle stays (else the handle is bare everywhere)
                new = set(_NAMED.findall(block[k])) - named
                if new:
                    keep.add(k)
                    named |= new
            block = [ln for k, ln in enumerate(block) if k not in ends or k in keep]
        out += [lines[i]] + block
        i = j
    return out


_N_CAST = re.compile(r"^(\s+)cast (e\d+)((?: [^:]+)?): (.+)$")
_N_DMG = re.compile(r"^(\s+)damage (e\d+)((?: [^>]+?)?) -> (e\d+)((?: [^;]+?)?) (\d+)(?: ([A-Z]\w*))?$")
_N_TURN = re.compile(r"^(\s+)turn_started (e\d+)((?: [^;]+)?)$")
_N_BOUND = re.compile(r"^\s+(?:turn_started|round_started)\b")


def _narrate(block: list[str]) -> list[str]:
    """COMBAT_NARRATIVE over one EVENTS block's lines (already in EVENT_LINES form)."""
    segs: list[list[int]] = [[]]
    for i, ln in enumerate(block):
        if _N_BOUND.match(ln):
            segs.append([])
        segs[-1].append(i)
    repl: dict[int, str] = {}
    drop: set[int] = set()
    for seg in segs:
        casts: dict[str, list[int]] = {}
        dmg: dict[str, list[int]] = {}
        for i in seg:
            m = _N_CAST.match(block[i])
            if m:
                casts.setdefault(m.group(2), []).append(i)
                continue
            m = _N_DMG.match(block[i])
            if m:
                dmg.setdefault(m.group(2), []).append(i)
        turn = _N_TURN.match(block[seg[0]]) if seg else None
        first_cast = min((ix[0] for ix in casts.values()), default=None)
        for who, ix in casts.items():
            if len(ix) != 1:
                continue
            c = _N_CAST.match(block[ix[0]])
            ind, name, spell = c.group(1), c.group(3), c.group(4)
            hits: dict[str, list] = {}
            for d in dmg.get(who, []):
                dm = _N_DMG.match(block[d])
                name = name or dm.group(3)
                h = hits.setdefault(dm.group(4), [dm.group(5), []])
                h[0] = h[0] or dm.group(5)
                h[1].append(dm.group(6) + (f" {dm.group(7)}" if dm.group(7) else ""))
                drop.add(d)
            tail = (" -> " + "; ".join(f"{t}{n} " + " + ".join(a) for t, (n, a) in hits.items())) if hits else ""
            if turn and turn.group(2) == who and ix[0] == first_cast:
                repl[seg[0]] = f"{ind}turn {who}{name or turn.group(3)}: {spell}{tail}"
                drop.add(ix[0])
            elif tail:
                at = min([ix[0]] + dmg[who])
                repl[at] = f"{ind}cast {who}{name}: {spell}{tail}"
                drop |= {ix[0], *dmg[who]} - {at}
    out = []
    for i, ln in enumerate(block):
        if i in repl:
            out.append(repl[i])
        elif i not in drop:
            out.append(ln)
    return out


def _turn_words(line: str) -> str:
    return re.sub(r"^(\s+)(turn|round)_started ", r"\1\2 ", line)


def combat_events(lines: list[str]) -> list[str]:
    """COMBAT_NARRATIVE and TURN_WORDS over every EVENTS block (TURN_ENDS ran before the names were placed)."""
    out: list[str] = []
    i = 0
    while i < len(lines):
        if lines[i] != "EVENTS":
            out.append(lines[i])
            i += 1
            continue
        j = i + 1
        while j < len(lines) and lines[j].startswith("  "):
            j += 1
        block = _narrate(lines[i + 1:j]) if COMBAT_NARRATIVE else lines[i + 1:j]
        out += [lines[i]] + ([_turn_words(ln) for ln in block] if TURN_WORDS else block)
        i = j
    return out


_ENDED_NOW = re.compile(r"^(NOW e\d+ hp=\d+(?:/\d+| max_hp=\d+)) action=\S+ bonus=\S+ movement=\S+ (reaction=\S+)$")


def ended_resources(lines: list[str]) -> list[str]:
    """ENDED_RESOURCES: after "end_turn: done", the member's NOW line keeps hp and reaction only."""
    return [_ENDED_NOW.sub(r"\1 \2", ln) if i and lines[i - 1].startswith("end_turn: done") else ln
            for i, ln in enumerate(lines)]


_RES_FULL = re.compile(r"^NOW Action (\d+)/\1 · Bonus (\d+)/\2 · Move ([\d.]+)/\3m · Reaction (\d+)/\4$")


def resources_line(lines: list[str], shown: dict[str, str] | None = None, elided: list[str] | None = None) -> list[str]:
    """RESOURCES_LINE: the YOUR ACTIONS header and its NOW resources on one line, 'full' when nothing is spent; with
    `shown` (handle -> the last full line shown), a full line identical to that member's last one is said short."""
    out: list[str] = []
    for ln in lines:
        h = re.fullmatch(r"YOUR ACTIONS \((e\d+)\)", out[-1]) if out and ln.startswith("NOW Action ") else None
        if not h:
            out.append(ln)
            continue
        m = _RES_FULL.match(ln)
        if not m:
            out[-1] += " " + ln
            continue
        full = (f"{out[-1]} NOW full: Action {m.group(1)} · Bonus {m.group(2)} · Move {m.group(3)}m · Reaction "
                f"{m.group(4)}")
        if shown is not None and shown.get(h.group(1)) == full:
            elided.append(full)
            out[-1] += " NOW full (as last turn)"
            continue
        if shown is not None:
            shown[h.group(1)] = full
        out[-1] = full
    return out


_NOW_TURN = re.compile(r"^NOW (e\d+) ([^\n]+?)'s turn")


def turn_names(lines: list[str]) -> list[str]:
    """TURN_NAMES: a NOW line naming whose turn it is drops the name a line above already gave that handle."""
    out: list[str] = []
    for ln in lines:
        m = _NOW_TURN.match(ln)
        if m and any(f"{m.group(1)} {m.group(2)}" in x for x in out):
            ln = f"NOW {m.group(1)}" + ln[m.end(2):]
        out.append(ln)
    return out


_NEXT = re.compile(r"^NEXT ((?:e\d+ [^,]+?|\+\d+ more)(?:, (?:e\d+ [^,]+?|\+\d+ more))*)(, then .*|\..*|:.*)?$")
_YOURS = re.compile(r"^(.*?)( \((?:yours|yours, shared turn)\))?$")


def _next_item(item: str, named: set[str]) -> str:
    """One NEXT entry with the names this result already showed cut to their handle. Keeps "(yours)" and every member
    of a shared turn: "NEXT e6 Claude (yours): plan that turn now" (a party member next, nobody in between) came out
    as "NEXT e6: plan that turn now" (claude-run-2 S050/S058 replay); a shared turn "e1 A + e2 B (yours, shared turn)"
    keeps every member (its comma keeps _NEXT from matching today, so it is shown in full)."""
    body, suffix = _YOURS.match(item).groups()
    parts = [re.sub(r"^(e\d+) .+$", r"\1", p) if p.split(" ")[0] in named else p for p in body.split(" + ")]
    return " + ".join(parts) + (suffix or "")


# ---------------------------------------------------------------------------
# the stateful part (what this session already showed)
# ---------------------------------------------------------------------------

class Compactor:
    def __init__(self, clock=time.time):
        self.clock = clock
        self.hint_seen: dict[str, int] = {}
        self.static: dict[str, tuple[str, float]] = {}
        self.last_clock = ""
        self.legend_shown = ""
        self.prev_text = ""
        self.quests: dict[str, int] = {}  # quest text -> the call that showed it
        self.calls = 0
        self.actions: dict[str, list[list[str]]] = {}  # handle -> the full YOUR ACTIONS lists shown
        self.resources: dict[str, str] = {}  # handle -> its last full YOUR ACTIONS line shown
        self.last_next: tuple[str, str] = ("", "")  # the last NEXT line: (as rendered, as shown)
        self.elided: list[str] = []  # this call's left-out fragments (shown verbatim earlier)
        self.block_lines: dict[str, tuple[float, str]] = {}  # CHAR/ITEM line -> (when shown in full, by which tool)

    def apply(self, tool: str, text: str) -> str:
        self.elided = []
        if not COMPACT or not isinstance(text, str) or not text:
            return text
        if tool in PROSE_TOOLS:
            out = "\n".join(self._clock(text.split("\n"))) if CLOCK else text
            self.prev_text = out
            return out
        lines = [_rewrite(ln) for ln in text.split("\n")]
        if MERGE_BLOCKS:
            lines = _merge_blocks(lines)
        if HARNESS_FIELDS:
            lines = harness_fields(lines)
        if ACTION_DUPS:
            lines = [action_dups(ln) for ln in lines]
        if COMBAT_MOVE_POS:
            lines = combat_move_pos(lines)
        if ENDED_RESOURCES:
            lines = ended_resources(lines)
        if PATH_LEGS:
            lines = path_legs(lines)
        if TURN_ENDS:
            lines = implied_turn_ends(lines)
        if EVENT_LINES:
            lines = _events(lines)
            if COMBAT_NARRATIVE or TURN_WORDS:
                lines = combat_events(lines)
        if SAME_GROUP:
            lines = _same_group(lines)
        if KIND_GROUP:
            lines = kind_group(lines)
        if REPEATS:
            lines = _repeats(lines)
        if QUEST_DEDUP:
            self.calls += 1
            lines = [self._quest(ln) for ln in lines]
        if EMPTY_LINES:
            lines = [ln for ln in lines if ln.strip() != "ALSO"]
        if DIALOGUE_DEDUP:
            lines = self._dialogue(lines)
        if HINTS:
            lines = self._hints(lines)
        if STATIC:
            lines = [self._static(ln) for ln in lines]
        if LAYOUT_LEGEND:
            lines = [self._legend(ln) for ln in lines]
        if ACTIONS_AGAIN:
            lines = self._actions(lines)
        if RESOURCES_LINE:
            lines = resources_line(lines, self.resources, self.elided)
        if TURN_NAMES:
            lines = turn_names(lines)
        if NEXT_ORDER:
            lines = self._next(lines)
        if BLOCK_AGAIN:
            lines = self._block_again(tool, lines)
        if CLOCK:
            lines = self._clock(lines)
        out = "\n".join(lines)
        out = re.sub(r"\n{3,}", "\n\n", out).strip("\n")
        self.prev_text = out
        return out

    def _hints(self, lines: list[str]) -> list[str]:
        out = []
        for ln in lines:
            new = ln
            for key, rx, short in _HINTS:
                def sub(m: re.Match, key=key, short=short) -> str:
                    n = self.hint_seen.get(key, 0)
                    self.hint_seen[key] = n + 1
                    return m.group(0) if n < HINT_FULL_TIMES else m.expand(short)
                new = rx.sub(sub, new)
            if new.strip() or not ln.strip():  # a hint that was the whole line goes with its line
                out.append(new)
        return out

    def _actions(self, lines: list[str]) -> list[str]:
        out: list[str] = []
        i = 0
        while i < len(lines):
            ln = lines[i]
            m = re.match(r"^YOUR ACTIONS \((e\d+)\)$", ln)
            if not m:
                out.append(ln)
                i += 1
                continue
            j = i + 1
            head = []
            if j < len(lines) and lines[j].startswith("NOW "):
                head.append(lines[j])
                j += 1
            k = j
            while k < len(lines) and _ACTION_LINE.match(lines[k]):
                k += 1
            body = lines[j:k]
            out += [ln] + head
            shown = self.actions.setdefault(m.group(1), [])
            if len(body) >= ACTIONS_MIN_LINES and body in shown:
                self.elided.append("\n".join(body))
                out.append("(actions as listed before)")
            else:
                if len(body) >= ACTIONS_MIN_LINES:
                    shown.append(body)
                out += body
            i = k
        return out

    def _next(self, lines: list[str]) -> list[str]:
        out: list[str] = []
        for ln in lines:
            m = _NEXT.match(ln)
            if not m:
                out.append(ln)
                continue
            order, rest = m.group(1), m.group(2) or ""
            if ln == self.last_next[0] and (", " in order or rest.startswith(", then")):   # not "NEXT e4 (yours): …"
                self.elided.append(self.last_next[1])
                out.append(f"NEXT (order as before){rest}")
                continue
            named = set(_NAMED.findall("\n".join(out)))
            shown = ", ".join(_next_item(it, named) for it in order.split(", "))
            self.last_next = (ln, f"NEXT {shown}{rest}")
            out.append(self.last_next[1])
        return out

    def _quest(self, line: str) -> str:
        m = _QUEST_TEXT.match(line)
        if not m:
            return line
        said = m.group(2) or m.group(5)
        if said in self.quests:
            if self.quests[said] != self.calls:  # shown by an earlier result (else: above in this one)
                self.elided.append(said)
            return (m.group(1) + "(text as before)" + m.group(3)) if m.group(1) else m.group(4) + "(text as before)"
        self.quests[said] = self.calls
        return line

    def _static(self, line: str) -> str:
        pre = next((p for p in _STATIC_PREFIXES if line.startswith(p)), None)
        if pre is None:
            return line
        now = self.clock()
        last = self.static.get(pre)
        if last and last[0] == line and now - last[1] < STATIC_RESHOW_S:
            self.elided.append(line)
            return pre + "(unchanged)"
        self.static[pre] = (line, now)
        return line

    def _block_again(self, tool: str, lines: list[str]) -> list[str]:
        """A run of CHAR/ITEM lines: those shown in full in the last BLOCK_AGAIN_S -> one reference line, the rest
        (changed or new) kept in order under it. A line's clock starts when it is shown in full, so a reference never
        points at another reference."""
        now = self.clock()
        self.block_lines = {k: v for k, v in self.block_lines.items() if now - v[0] < BLOCK_AGAIN_S}
        out: list[str] = []
        i = 0
        while i < len(lines):
            if not lines[i].startswith(_BLOCK_PREFIXES):
                out.append(lines[i])
                i += 1
                continue
            j = i
            while j < len(lines) and lines[j].startswith(_BLOCK_PREFIXES):
                j += 1
            run = lines[i:j]
            same = [ln for ln in run if ln in self.block_lines]
            if len(same) >= BLOCK_AGAIN_MIN_LINES:
                src = self.block_lines[same[0]][1]
                src = "the look" if src == "look" else src
                rest = [ln for ln in run if ln not in self.block_lines]
                self.elided += same
                out.append(f"(same as {src} a moment ago: {len(same)} lines)" if not rest else
                           f"({len(same)} lines same as {src} a moment ago; changed or new:)")
                out += rest
                for ln in rest:
                    self.block_lines[ln] = (now, tool)
            else:
                out += run
                for ln in run:
                    self.block_lines[ln] = (now, tool)
            i = j
        return out

    def _legend(self, line: str) -> str:
        m = _LAYOUT_RE.match(line)
        if not m:
            return line
        if self.legend_shown == m.group(2):
            self.elided.append(m.group(2))
            return m.group(1) + "key as before"
        self.legend_shown = m.group(2)
        return line

    def _clock(self, lines: list[str]) -> list[str]:
        out = []
        for ln in lines:
            if _CLOCK_RE.match(ln):
                if ln == self.last_clock:
                    self.elided.append(ln)
                    continue
                self.last_clock = ln
            out.append(ln)
        return out

    def _dialogue(self, lines: list[str]) -> list[str]:
        out: list[str] = []
        for ln in lines:
            m = _DLG_EVENT.match(ln)
            if m:
                said = m.group(2)
                if _quotes("\n".join(out), said):
                    continue  # quoted above in this result
                if _quotes(self.prev_text, said):
                    self.elided.append(said)
                    continue
            out.append(ln)
        # an EVENTS header left with nothing under it
        return [ln for j, ln in enumerate(out)
                if not (ln == "EVENTS" and (j + 1 == len(out) or not out[j + 1].startswith("  ")))]


def _quotes(text: str, said: str) -> bool:
    """`said` appears in text as a quoted line: after "Speaker: " (DIALOGUE) or "text=" (lines=[...], events)."""
    return any(f"{pre}{said}" in text for pre in (": ", "text="))


def switch_off(spec: str) -> list[str]:
    """BG3AI_COMPACT_OFF=RULE1,RULE2: those rules off (COORD_DECIMALS: positions kept). Returns the unknown names."""
    unknown = []
    for name in (n.strip().upper() for n in spec.split(",")):
        if not name:
            continue
        if name == "COORD_DECIMALS":
            globals()[name] = None
        elif isinstance(globals().get(name), bool) and name != "COMPACT" and not name.startswith("_"):
            globals()[name] = False
        else:
            unknown.append(name)
    if unknown:
        log.warning("BG3AI_COMPACT_OFF: unknown rule(s) %s (ignored; rules: %s)", ", ".join(unknown), ", ".join(rules()))
    return unknown


def rules() -> list[str]:
    """The rule switches BG3AI_COMPACT_OFF accepts."""
    return [k for k, v in globals().items() if k.isupper() and not k.startswith("_") and k != "COMPACT"
            and (isinstance(v, bool) or k == "COORD_DECIMALS")]


switch_off(os.environ.get("BG3AI_COMPACT_OFF", ""))  # read at bridge start

DEFAULT = Compactor()


def apply(tool: str, text: str) -> str:
    return DEFAULT.apply(tool, text)

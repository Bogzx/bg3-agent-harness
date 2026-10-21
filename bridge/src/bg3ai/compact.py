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

from .compact_rules import (
    _ACTION_LINE,
    _AT,
    _DLG_EVENT,
    _HEADERS,
    _NAMED,
    _NEXT,
    _OWN_POS,
    _QUEST_TEXT,
    _REF,
    _changes_facts,
    _merge_blocks,
    _narrate,
    _next_item,
    _repeats,
    _round,
    _same_group,
    _turn_words,
    action_dups,
    cast_ids,
    combat_move_pos,
    combat_row,
    dup_fields,
    empties,
    ended_resources,
    event_line,
    gone_group,
    harness_fields,
    implied_turn_ends,
    kind_group,
    move_phrases,
    numbers,
    path_legs,
    refs,
    resources_line,
    turn_names,
)

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
CLOCK = True         # a "⏱ 16:38 · keep playing: …" line identical to the last one shown is left out
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
# before WRAP UP / the fight line / the old terse "22m of play left" (recorded sessions: the golden replays)
_CLOCK_RE = re.compile(r"^⏱ \d\d:\d\d · (?:\d+m (?:of play )?left|keep playing: [^\n]*|in a fight: [^\n]*)$")
_STATIC_PREFIXES = ("ALERTS ", "PARTY: ")


def enabled() -> bool:
    return COMPACT


# ---------------------------------------------------------------------------
# rewrites that read a rule switch (same facts, fewer tokens; the rest: compact_rules.py)
# ---------------------------------------------------------------------------


def coords(line: str, nd: int | None = None) -> str:
    nd = COORD_DECIMALS if nd is None else nd
    if nd is None or COORD_OWN and _OWN_POS.match(line):
        return line
    return _AT.sub(lambda m: "@(" + ",".join(_round(v, nd) for v in m.groups()) + ")", line)


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

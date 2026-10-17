"""Fact checker and replay for compact.py: does every fact of a recorded tool result survive compaction?

A *fact atom* is a word or number of the result (names, handles, statuses, HP, distances, coordinates, event kinds,
dialogue words). The instruction text of compact.HINT_RULES is not a fact (its named groups are), nor are the key
words the rewrites drop (`name=`, `type=`, ...), nor an engine spell id on a cast event that carries the display
name. Checks per result, with `shown` = every earlier *compacted* result of the session (what the model saw):
1. each fragment the compactor left out as already seen (Compactor.elided) is in `shown` verbatim;
2. atoms(original) ⊆ atoms(compacted ∪ elided): nothing dropped;
3. binding: the handles + numbers of each original line sit together on one line of compacted ∪ elided
   (e31's HP stays with e31).
`replay(results)` runs a fresh Compactor over a session's results (tool, text, t) and returns the per-result rows;
tools/vis/obs_size.py and tests/test_compact.py drive it over the naut transcripts.
"""

from __future__ import annotations

import re

from . import compact

_ATOM = re.compile(r"-?\d+(?:\.\d+)?|[A-Za-z_'][A-Za-z0-9_'\-]*")
_NUM = re.compile(r"^-?\d+(?:\.\d+)?$")
_HANDLE = re.compile(r"^[eoj]\d+$")
# key words the line rewrites drop (their values stay)
IGNORED = {"name", "type", "amount", "target", "resource", "max_hp", "spell", "cast",
           # structure words: a header merged into the one above, an empty ALSO, a quoted line's event wrapper
           "ALSO", "EVENTS", "CHANGES", "dialogue_line", "speaker", "text",
           # event keys the EVENTS templates replace by position ("damage S -> T N X", "cast C: N (roll)")
           "source", "caster", "roll", "item", "char", "owner", "count", "round",
           # combat rows ("init 4 hp 100% current"), a total_damage equal to its one damage entry
           "initiative", "hp_pct", "relation", "race", "is_current", "total_damage",
           # an event whose fact CHANGES states in the same result (DEAD, +Status, -Status)
           "died", "status_applied", "status_removed",
           # combat narrative (COMBAT_NARRATIVE / TURN_WORDS): "turn e86: Main Hand Attack (attack) -> e67 12 Slashing"
           # stands for its turn_started/cast/damage events; "round 2" for round_started
           "damage", "turn_started", "round_started",
           # statuses=[target=e2 status=Dash] -> statuses=[e2 Dash] (ACTION_DUPS)
           "status"}
_FROM = re.compile(r"(e\d+)=\[(-?[\d.]+),([01])\]")
_CAST_SPELL = re.compile(r"^(\s+cast\b.*?) spell=\S+(.*name=.*)$")
_PIECE = re.compile(r"[A-Za-z]{1,8}|\d{1,3}|[^\sA-Za-z\d]|\n")
TOKENS_PER_PIECE = 1.6  # fitted on naut/claude-run-2 usage deltas (research/impl-vis.md "Observation size")


def est_tokens(text: str) -> int:
    """Approximate model tokens of a result: 1.6 x pieces (letter runs <= 8, digit runs <= 3, each symbol, newline).
    Fitted on the context growth per tool result in 6 recorded sessions (chars/token varies 1.6-2.4 by content;
    pieces track punctuation-dense game text better)."""
    return round(TOKENS_PER_PIECE * len(_PIECE.findall(text or "")))


def strip_hints(text: str) -> str:
    """The original with each hint's instruction text replaced by its fact groups (and spell ids next to a name)."""
    lines = text.split("\n")
    # combat equivalences: a partial walk's outcome stands for the FAILED before it, a duplicate "Xm left" (MOVE_PHRASES);
    # a turn_ended in a block that starts the next turn (TURN_ENDS)
    if compact.MOVE_PHRASES:
        lines = [compact.move_phrases(ln) for ln in lines]
    if compact.TURN_ENDS:
        lines = compact.implied_turn_ends(compact._merge_blocks(lines))
    # a spent turn's leftover action/bonus/movement after "end_turn: done" (ENDED_RESOURCES)
    if compact.ENDED_RESOURCES:
        lines = compact.ended_resources(lines)
    text = "\n".join(lines)
    for _key, rx, _short in compact._HINTS:
        text = rx.sub(lambda m: " " + " ".join(v for v in m.groupdict().values() if v) + " ", text)
    # equivalences: a from= pair's line-of-sight flag is a word ("no-LOS") when 0; reported positions are rounded
    text = _FROM.sub(lambda m: f"{m.group(1)} {m.group(2)}" + ("" if m.group(3) == "1" else " no-LOS"), text)
    lines = [compact.coords(ln) for ln in text.split("\n")]
    # harness bookkeeping (compact.HARNESS_FIELDS): not a player-visible fact
    text = "\n".join(compact.harness_fields(lines))
    # a combat turn's move lines carry no end position (COMBAT_MOVE_POS: CHANGES says who moved how far)
    if compact.COMBAT_MOVE_POS:
        text = "\n".join(compact.combat_move_pos(text.split("\n")))
    # a PATH leg's ordinal ("  2. 19.5m W") is a label, not a fact: merged legs keep the first and last
    text = re.sub(r"^  \d+\. (?=[\d.]+m [NESW]{1,2}$)", "  ", text, flags=re.M)
    for rx, _r in compact.phrases():
        text = rx.sub(lambda m: " " + " ".join(v for v in m.groups() if v) + " ", text)
    return "\n".join(_CAST_SPELL.sub(r"\1\2", ln) for ln in text.split("\n"))


def atoms(text: str) -> set[str]:
    """The facts of a text. A possessive counts as its name ("Wyll's turn" -> Wyll): "NOW e2 Wyll's turn" compacted
    to "NOW e2's turn" under "turn=e2 Wyll" kept the fact (claude-run-2 Act 2/3 replay: 56 such false alarms)."""
    return {a[:-2] if a.endswith("'s") and len(a) > 2 else a for a in _ATOM.findall(text)} - IGNORED


def _line_keys(line: str) -> frozenset[str]:
    return frozenset(a for a in _ATOM.findall(line) if _NUM.match(a) or _HANDLE.match(a))


def check(orig: str, out: str, elided: list[str], shown: str) -> list[str]:
    """Problems with one compacted result ([] = every fact survived)."""
    problems = [f"elided but never shown: {e[:80]!r}" for e in elided if e not in shown]
    base = strip_hints(orig)
    have = atoms(out) | set().union(*(atoms(e) for e in elided)) if elided else atoms(out)
    lost = atoms(base) - have
    if lost:
        problems.append(f"lost atoms: {sorted(lost)[:12]}")
    lines = [atoms(x) for x in (out + "\n" + "\n".join(elided)).split("\n")]
    for ln in base.split("\n"):
        keys = _line_keys(ln)
        if len(keys) < 2 or not any(_HANDLE.match(k) for k in keys):
            continue
        if not any(keys <= x for x in lines):
            problems.append(f"binding lost: {ln.strip()[:100]!r}")
    return problems


def replay(results: list[dict], compactor: compact.Compactor | None = None) -> list[dict]:
    """results: [{"tool", "text", "t"}] of one session in order. Returns [{"tool", "orig", "out", "elided",
    "problems"}]."""
    now = {"t": 0.0}
    c = compactor or compact.Compactor(clock=lambda: now["t"])
    shown: list[str] = []
    rows = []
    for r in results:
        now["t"] = float(r.get("t") or now["t"])
        out = c.apply(r["tool"], r["text"])
        probs = check(r["text"], out, list(c.elided), "\n".join(shown))
        rows.append({"tool": r["tool"], "orig": r["text"], "out": out, "elided": list(c.elided), "problems": probs})
        shown.append(out)
    return rows

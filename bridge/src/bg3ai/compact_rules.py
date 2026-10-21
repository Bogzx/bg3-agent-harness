"""Compaction rules that keep no state and read no rule switch: line rewrites (refs, empties, numbers, groups,
combat rows), block rewrites (merges, repeats, same/kind groups) and the combat text helpers. compact.py decides
which run (the switches, BG3AI_COMPACT_OFF) and re-exports these names."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .compact import _Names

_HEADERS = ("CHANGES", "EVENTS")

_H = r"[eo]\d+"
_NAME = r"[^{}=;,:\[\]\n]+?"


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

"""Pure parsers for the files a playthrough run leaves behind (no I/O; the store reads, these shape).

- Sxxx.log lines ("HH:MM:SS [kind] text", "===== ..." headers) from runner/play.py's stream printer.
- The `[end]` line's outcome, turns, minutes and cost.
- actions.jsonl rows and their tool kind.
- get_status / look result text: WHERE line and PARTY rows (HP, flags).
- The current goal from objectives.md ("NOW:") or HANDOFF.md ("## Doing right now").
- decisions.md cards.
- fights.jsonl rows (tools/logs fight records) and `git log` output.

The single source of these parsers: runner/dashboard imports them.
"""

from __future__ import annotations

import json
import re
from typing import Any

from .jsontypes import is_obj, objs

LOG_LINE_RE = re.compile(r"^(\d\d:\d\d:\d\d) \[([a-z_-]+)\] ?(.*)$")
LOG_HEADER_RE = re.compile(r"^===== (S\d{3,}\S*) (.*)$")
END_RE = re.compile(r"^(\w+)(?: turns=(\d+))?(?: duration=([\d.]+)min)?(?: cost=\$([\d.]+))?")
NEWLINE_MARK = " ⏎ "  # the stream printer's stand-in for a newline inside one log line
UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE)


# ---------------------------------------------------------------- session log


def log_line(line: str) -> dict[str, Any] | None:
    """One Sxxx.log line -> {"clock", "kind", "text"}; a "===== " header -> kind "header"; anything else None."""
    m = LOG_LINE_RE.match(line)
    if m:
        return {"clock": m.group(1), "kind": m.group(2), "text": m.group(3).replace(NEWLINE_MARK, "\n")}
    h = LOG_HEADER_RE.match(line)
    if h:
        return {"clock": None, "kind": "header", "session": h.group(1), "text": h.group(2)}
    return None


def end_line(text: str) -> dict[str, Any]:
    """The `[end]` text ("success turns=324 duration=25.3min cost=$9.89") -> outcome, turns, minutes, cost_usd."""
    m = END_RE.match(text.strip())
    if not m:
        return {"outcome": None, "turns": None, "minutes": None, "cost_usd": None}
    return {
        "outcome": m.group(1),
        "turns": int(m.group(2)) if m.group(2) else None,
        "minutes": float(m.group(3)) if m.group(3) else None,
        "cost_usd": float(m.group(4)) if m.group(4) else None,
    }


# ---------------------------------------------------------------- actions.jsonl


def tool_kind(tool: str) -> str:
    t = tool.removeprefix("mcp__bg3__")
    if t in ("screenshot", "zoom"):
        return "vision"
    if t.startswith(("get_", "list_", "look_", "session_clock", "wiki", "ability_preview")):
        return "read"
    if t in ("save_game", "load_game"):
        return "save"
    return "act"


def action(line: str, result_limit: int = 2500) -> dict[str, Any] | None:
    """One actions.jsonl line -> a feed item, or None for a bad line."""
    try:
        e = json.loads(line)
    except ValueError:
        return None
    if not is_obj(e):
        return None
    result = str(e.get("result", ""))
    tool = str(e.get("tool", "?"))
    return {
        "t": e.get("t"),
        "tool": tool,
        "args": e.get("args") or {},
        "ms": e.get("ms"),
        "result": result[:result_limit] + ("…" if len(result) > result_limit else ""),
        "ok": not re.match(r"^(FAILED|ERROR|error|Blocked|NOT READY)", result),
        "kind": tool_kind(tool),
    }


# ---------------------------------------------------------------- status text (get_status / look results)

WHERE_RE = re.compile(r"^WHERE (.*?)(?: \| (.*?))?(?: \| gold (\d+))?\s*$")
PARTY_RE = re.compile(r"^PARTY (\* ?)?(?:e\d+ )?(.*?) HP (\d+)/(\d+)(?:\+(\d+))?\s*(?:\[(.*?)\])?\s*(.*)$")
CLASS_TOKEN_RE = re.compile(r"^(?:[A-Z][A-Za-z]*\d+\S*|L\d+)$")


def _split_name(head: str) -> tuple[str, str]:
    """ "Shadowheart Cleric1(Trickery Domain)" / "Wyll Warlock 12 (The Fiend)" / "The Emperor L12" -> name, classes."""
    toks = head.split()
    for i, tok in enumerate(toks[1:], 1):
        if CLASS_TOKEN_RE.match(tok) or (i + 1 < len(toks) and toks[i + 1].isdigit()):
            return " ".join(toks[:i]), " ".join(toks[i:])
    return head, ""


def status_text(text: str) -> dict[str, Any] | None:
    """A get_status/look result -> {"where", "mode", "gold", "party": [...]}; None when it has no WHERE or PARTY
    rows (a short diff-only status)."""
    where = mode = gold = None
    party: list[dict[str, Any]] = []
    for line in text.splitlines():
        w = WHERE_RE.match(line)
        if w:
            where, mode = w.group(1), w.group(2)
            gold = int(w.group(3)) if w.group(3) else None
            continue
        p = PARTY_RE.match(line)
        if p:
            name, classes = _split_name(p.group(2))
            flags = p.group(7).split()
            party.append(
                {
                    "name": name,
                    "classes": classes,
                    "hp": int(p.group(3)),
                    "max_hp": int(p.group(4)),
                    "temp_hp": int(p.group(5)) if p.group(5) else 0,
                    "active": bool(p.group(1)),
                    "statuses": [s for s in (p.group(6) or "").split(",") if s],
                    "downed": "downed" in flags,
                    "dead": "dead" in flags,
                    "in_combat": "in_combat" in flags,
                }
            )
    if where is None and not party:
        return None
    return {"where": where, "mode": mode, "gold": gold, "party": party}


# ---------------------------------------------------------------- goal

NOW_RE = re.compile(r"^\s*(?:\d+\.\s*|-\s*)?NOW:\s*(.+)$")
TEMPLATE_HANDOFF_RE = re.compile(r"^#\s*Handoff\s+S0+\b", re.MULTILINE)  # memory-template's "# Handoff S000 → S001"


def current_goal(objectives: str, handoff: str) -> str | None:
    """objectives.md's "NOW:" entry, else the first line under HANDOFF.md's "## Doing right now" once a session has
    written it (the template's own text is no goal)."""
    for line in _strip_comments(objectives).splitlines():
        m = NOW_RE.match(line)
        if m:
            return m.group(1).strip()
    lines = _strip_comments(handoff).splitlines()
    if TEMPLATE_HANDOFF_RE.match(_strip_comments(handoff).lstrip()):
        return None
    for i, line in enumerate(lines):
        if line.strip().lower().startswith("## doing right now"):
            for nxt in lines[i + 1 :]:
                if nxt.startswith("#"):
                    break
                if nxt.strip():
                    return nxt.strip().lstrip("- ").strip()
    return None


# ---------------------------------------------------------------- decisions.md

DECISION_RE = re.compile(r"^##\s+(D-\d+[a-z]?)\s+(.*?)\s*(?:\((S[\d\-\u2013, S]+[^)]*)\))?\s*$")
FIELD_RE = re.compile(
    r"^-\s+(Choice|Why|Consequence|Expected later|Expected|Reversible|Note)s?:\s*(.*)$", re.IGNORECASE
)
TAG_RE = re.compile(r"\[(seen[^\]]*|wiki|guess|expected[^\]]*)\]\s*$", re.IGNORECASE)


def _strip_comments(text: str) -> str:
    return re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)


def decisions(text: str) -> list[dict[str, Any]]:
    """D-xx cards in file order. Field lines may repeat (several choices or consequences)."""
    cards: list[dict[str, Any]] = []
    cur: dict[str, Any] | None = None
    field = None
    for raw in _strip_comments(text).splitlines():
        line = raw.rstrip()
        m = DECISION_RE.match(line)
        if m:
            cur = {
                "id": m.group(1),
                "title": m.group(2).strip(),
                "session": (m.group(3) or "").strip() or None,
                "choice": [],
                "why": [],
                "consequences": [],
                "expected": [],
                "reversible": None,
                "notes": [],
            }
            cards.append(cur)
            field = None
            continue
        if line.startswith("#"):
            cur = None if not line.startswith("###") else cur
            continue
        if cur is None or not line.strip():
            continue
        f = FIELD_RE.match(line.strip())
        if f:
            key, val = f.group(1).lower(), f.group(2).strip()
            tag = TAG_RE.search(val)
            item = {"text": TAG_RE.sub("", val).strip(), "tag": tag.group(1) if tag else None}
            if key == "choice":
                cur["choice"].append(val)
                field = "choice"
            elif key == "why":
                cur["why"].append(val)
                field = "why"
            elif key == "consequence":
                cur["consequences"].append(item)
                field = "consequences"
            elif key.startswith("expected"):
                cur["expected"].append(item)
                field = "expected"
            elif key == "reversible":
                cur["reversible"] = val
                field = None
            else:
                cur["notes"].append(val)
                field = "notes"
        elif line.startswith(("  ", "\t")) and field and cur[field]:
            last = cur[field][-1]  # a wrapped continuation line
            if is_obj(last):
                last["text"] += " " + line.strip()
            else:
                cur[field][-1] = last + " " + line.strip()
        else:
            cur["notes"].append(line.strip().lstrip("- "))
    return cards


def session_number(s: str | None) -> int | None:
    m = re.match(r"S(\d+)", s or "")
    return int(m.group(1)) if m else None


# ---------------------------------------------------------------- fights.jsonl


def fight(row: Any) -> dict[str, Any] | None:
    """A fight record (v1, tools/logs) -> the timeline's summary of it."""
    if not is_obj(row) or not row.get("session"):
        return None
    party = objs(row.get("party"))
    enemies = objs(row.get("enemies"))

    return {
        "session": row["session"],
        "t": row.get("t"),
        "region": row.get("region"),
        "grade": row.get("grade"),
        "rounds": row.get("rounds"),
        "duration_s": row.get("duration_s"),
        "reloaded": bool(row.get("reloaded")),
        "enemies": [
            f"{e.get('name', '?')}" + (f" x{e['count']}" if (e.get("count") or 1) > 1 else "") for e in enemies
        ],
        "downed": sum(int(p.get("downed") or 0) for p in party),
        "died": [p.get("name") for p in party if p.get("died")],
    }


# ---------------------------------------------------------------- git log

GIT_LOG_FORMAT = "%H%x09%ct%x09%s"


def git_log(text: str) -> list[dict[str, Any]]:
    """`git log --format=%H%x09%ct%x09%s` output -> [{"sha", "t", "subject", "session"}], newest first."""
    out: list[dict[str, Any]] = []
    for line in text.splitlines():
        parts = line.split("\t", 2)
        if len(parts) != 3 or not parts[1].isdigit():
            continue
        m = re.match(r"(S\d{3,})", parts[2])
        out.append({"sha": parts[0], "t": int(parts[1]), "subject": parts[2], "session": m.group(1) if m else None})
    return out


DIFF_KINDS = (
    ("+++", "meta"),
    ("---", "meta"),
    ("@@", "hunk"),
    ("+", "add"),
    ("-", "del"),
    ("diff ", "meta"),
    ("index ", "meta"),
    ("new file", "meta"),
    ("deleted file", "meta"),
    ("\\", "meta"),
)


def diff_lines(patch: str) -> list[dict[str, Any]]:
    """A unified diff -> [{"kind": add | del | hunk | ctx | meta, "text"}] for the memory browser."""
    out: list[dict[str, Any]] = []
    for line in patch.splitlines():
        kind = next((k for prefix, k in DIFF_KINDS if line.startswith(prefix)), "ctx")
        out.append({"kind": kind, "text": line})
    return out


def scrub(text: str) -> str:
    """GUIDs out of text shown to viewers (the dashboard never needs them)."""
    return UUID_RE.sub("…", text)

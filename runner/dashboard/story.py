"""The story so far, for the dashboard's Story panel (/api/story), read from the playthrough memory (read-only).

- decisions.md: one card per "## D-xx Title (Sxxx)" entry: choices, why, consequences (seen / expected), reversible.
- chapters/*.md: chapter summaries, split into their "## " sections.
- PLAYTHROUGH.md: the "Now:" line, the main thread, the party and the standing commitments.
- snapshots/status.md + inventory.md: party HP, gold, where; per member the equipped gear and the notable bag
  items (story items and anything uncommon or rarer).

Everything is parsed from Markdown the playing agent and the memory keeper write, so every parser is lenient:
an unknown line is kept as text, a missing file is an empty part.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

_CACHE: dict[str, tuple[tuple, Any]] = {}

DECISION_RE = re.compile(r"^##\s+(D-\d+[a-z]?)\s+(.*?)\s*(?:\((S[\d\-–, S]+[^)]*)\))?\s*$")
FIELD_RE = re.compile(r"^-\s+(Choice|Why|Consequence|Expected later|Expected|Reversible|Note)s?:\s*(.*)$", re.I)
TAG_RE = re.compile(r"\[(seen[^\]]*|wiki|guess|expected[^\]]*)\]\s*$", re.I)
PARTY_LINE_RE = re.compile(r"^PARTY\s+(\*\s+)?(\S+)\s+(.*?)\s+HP\s+(\d+)/(\d+)\s*(?:\[(.*)\])?\s*$")
RARE_RE = re.compile(r"\((uncommon|rare|very rare|legendary)\)", re.I)
EQUIP_RE = re.compile(r"\[equipped ([^\]]+)\]")
RARITY_RANK = {"legendary": 4, "very rare": 3, "rare": 2, "uncommon": 1}


def _sig(paths: list[Path]) -> tuple:
    out = []
    for p in paths:
        try:
            st = p.stat()
            out.append((str(p), st.st_size, st.st_mtime))
        except OSError:
            out.append((str(p), None))
    return tuple(out)


def _read(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _strip_comments(text: str) -> str:
    return re.sub(r"<!--.*?-->", "", text, flags=re.S)


# ---------------------------------------------------------------- decisions

def parse_decisions(text: str) -> list[dict]:
    """D-xx cards in file order. Field lines may repeat (several choices or consequences)."""
    cards: list[dict] = []
    cur: dict | None = None
    field = None
    for raw in _strip_comments(text).splitlines():
        line = raw.rstrip()
        m = DECISION_RE.match(line)
        if m:
            cur = {"id": m.group(1), "title": m.group(2).strip(), "session": (m.group(3) or "").strip() or None,
                   "choice": [], "why": [], "consequences": [], "expected": [], "reversible": None, "notes": []}
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
                cur["choice"].append(val); field = "choice"
            elif key == "why":
                cur["why"].append(val); field = "why"
            elif key == "consequence":
                cur["consequences"].append(item); field = "consequences"
            elif key.startswith("expected"):
                cur["expected"].append(item); field = "expected"
            elif key == "reversible":
                cur["reversible"] = val; field = None
            else:
                cur["notes"].append(val); field = "notes"
        elif line.startswith(("  ", "\t")) and field and cur[field]:
            last = cur[field][-1]  # a wrapped continuation line
            if isinstance(last, dict):
                last["text"] += " " + line.strip()
            else:
                cur[field][-1] = last + " " + line.strip()
        else:
            cur["notes"].append(line.strip().lstrip("- "))
    return cards


# ---------------------------------------------------------------- chapters

def parse_chapter(name: str, text: str) -> dict:
    title, sections, cur = name, [], None
    for line in _strip_comments(text).splitlines():
        if line.startswith("# ") and title == name:
            title = line[2:].strip()
        elif line.startswith("## "):
            cur = {"heading": line[3:].strip(), "items": []}
            sections.append(cur)
        elif line.strip():
            if cur is None:
                cur = {"heading": "", "items": []}
                sections.append(cur)
            cur["items"].append(line.strip().lstrip("- ").strip())
    return {"file": name, "title": title, "sections": sections}


def chapters(mem: Path) -> list[dict]:
    d = mem / "chapters"
    try:
        files = sorted(f for f in d.iterdir() if f.suffix == ".md")
    except OSError:
        return []
    return [parse_chapter(f.name, _read(f)) for f in files]


# ---------------------------------------------------------------- PLAYTHROUGH.md

def parse_playthrough(text: str) -> dict:
    out: dict[str, Any] = {"title": None, "header": [], "now": None, "main_thread": None, "party": [],
                           "commitments": [], "lessons": []}
    sec, in_section = None, False
    want = {"party": "party", "standing commitments": "commitments", "commitments": "commitments",
            "top lessons": "lessons"}
    for line in _strip_comments(text).splitlines():
        s = line.strip()
        if not s:
            continue
        if s.startswith("# "):
            out["title"] = s[2:].strip()
        elif s.startswith("## "):
            sec, in_section = want.get(s[3:].strip().lower()), True
        elif in_section and sec is None:
            continue  # a section the panel doesn't show ("Read when")
        elif sec is None and s.lower().startswith("now:"):
            out["now"] = s[4:].strip()
        elif sec is None and s.lower().startswith("main thread:"):
            out["main_thread"] = s[len("main thread:"):].strip()
        elif sec is None:
            out["header"].append(s)
        elif sec and s.startswith("-"):
            out[sec].append(s.lstrip("- ").strip())
    return out


# ---------------------------------------------------------------- snapshots

def _snap_time(text: str) -> str | None:
    m = re.search(r"#\s*Snapshot:.*?\((.*?)\)", text)
    return m.group(1) if m else None


def parse_status(text: str) -> dict:
    out: dict[str, Any] = {"when": _snap_time(text), "where": None, "mode": None, "gold": None, "party": []}
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("WHERE "):
            parts = [p.strip() for p in s[6:].split("|")]
            out["where"] = parts[0] if parts else None
            for p in parts[1:]:
                g = re.match(r"gold\s+(\d+)", p)
                if g:
                    out["gold"] = int(g.group(1))
                elif out["mode"] is None:
                    out["mode"] = p
        m = PARTY_LINE_RE.match(s)
        if m:
            out["party"].append({"name": m.group(2), "active": bool(m.group(1)), "class": m.group(3),
                                 "hp": int(m.group(4)), "max_hp": int(m.group(5)),
                                 "statuses": [x.strip() for x in (m.group(6) or "").split(",") if x.strip()]})
    return out


def parse_inventory(text: str) -> dict:
    """Per member: equipped (slot, item) and notable bag items (story or uncommon+), rarest first."""
    out: dict[str, dict] = {}
    cur = None
    for line in text.splitlines():
        if line.startswith("## "):
            cur = {"equipped": [], "notable": [], "items": 0}
            out[line[3:].strip()] = cur
            continue
        if cur is None or not line.startswith("- "):
            continue
        s = line[2:].strip()
        cur["items"] += 1
        eq = EQUIP_RE.search(s)
        rare = RARE_RE.search(s)
        name = RARE_RE.sub("", EQUIP_RE.sub("", s))
        name = re.sub(r"\s+(usable|unique|story)\b", "", name).strip()
        item = {"name": name, "rarity": rare.group(1).lower() if rare else None, "story": " story" in f" {s}"}
        if eq:
            cur["equipped"].append(dict(item, slot=eq.group(1)))
        elif item["rarity"] or item["story"]:
            cur["notable"].append(item)
    for v in out.values():
        v["notable"].sort(key=lambda i: (-RARITY_RANK.get(i["rarity"] or "", 0), not i["story"], i["name"]))
    return out


# ---------------------------------------------------------------- payload

def story_payload(mem: Path) -> dict:
    files = [mem / "decisions.md", mem / "PLAYTHROUGH.md", mem / "snapshots" / "status.md",
             mem / "snapshots" / "inventory.md"]
    try:
        files += sorted((mem / "chapters").glob("*.md"))
    except OSError:
        pass
    sig = _sig(files)
    hit = _CACHE.get(str(mem))
    if hit and hit[0] == sig:
        return hit[1]
    status = parse_status(_read(mem / "snapshots" / "status.md"))
    inv = parse_inventory(_read(mem / "snapshots" / "inventory.md"))
    for p in status["party"]:
        p.update(inv.get(p["name"]) or {"equipped": [], "notable": [], "items": 0})
    shown = {p["name"] for p in status["party"]}
    camp = [{"name": n, **v} for n, v in inv.items() if n not in shown]  # in the inventory snapshot, not the party
    payload = {
        "dir": str(mem),
        "exists": mem.is_dir(),
        "decisions": parse_decisions(_read(mem / "decisions.md")),
        "chapters": chapters(mem),
        "playthrough": parse_playthrough(_read(mem / "PLAYTHROUGH.md")),
        "status": status,
        "others": camp,
    }
    _CACHE[str(mem)] = (sig, payload)
    return payload

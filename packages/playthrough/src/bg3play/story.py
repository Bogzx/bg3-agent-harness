"""The story so far (the Story screen; /api/v1/playthroughs/<pt>/story), read from the playthrough memory.

- decisions.md: one card per "## D-xx Title (Sxxx)" entry: choices, why, consequences (seen / expected), reversible.
- chapters/*.md: chapter summaries, split into their "## " sections.
- PLAYTHROUGH.md: the "Now:" line, the main thread, the party and the standing commitments.
- snapshots/status.md + inventory.md: party HP, gold, where; per member the equipped gear and the notable bag
  items (story items and anything uncommon or rarer).

Everything is parsed from Markdown the playing agent and the memory keeper write, so every parser is lenient:
an unknown line is kept as text, a missing file is an empty part.
"""

from __future__ import annotations

import contextlib
import re
from pathlib import Path
from typing import Any, cast

from . import parse
from .jsontypes import Obj

_CACHE: dict[str, tuple[tuple[Any, ...], Any]] = {}

RARE_RE = re.compile(r"\((uncommon|rare|very rare|legendary)\)", re.IGNORECASE)
EQUIP_RE = re.compile(r"\[equipped ([^\]]+)\]")
RARITY_RANK = {"legendary": 4, "very rare": 3, "rare": 2, "uncommon": 1}


def _sig(paths: list[Path]) -> tuple[Any, ...]:
    out: list[tuple[Any, ...]] = []
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
    return re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)


# ---------------------------------------------------------------- decisions

parse_decisions = parse.decisions  # single source: the playthrough app


# ---------------------------------------------------------------- chapters


def parse_chapter(name: str, text: str) -> dict[str, Any]:
    title = name
    sections: list[dict[str, Any]] = []
    cur: dict[str, Any] | None = None
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


def chapters(mem: Path) -> list[dict[str, Any]]:
    d = mem / "chapters"
    try:
        files = sorted(f for f in d.iterdir() if f.suffix == ".md")
    except OSError:
        return []
    return [parse_chapter(f.name, _read(f)) for f in files]


# ---------------------------------------------------------------- PLAYTHROUGH.md


def parse_playthrough(text: str) -> dict[str, Any]:
    out: dict[str, Any] = {
        "title": None,
        "header": [],
        "now": None,
        "main_thread": None,
        "party": [],
        "commitments": [],
        "lessons": [],
    }
    sec, in_section = None, False
    want = {
        "party": "party",
        "standing commitments": "commitments",
        "commitments": "commitments",
        "top lessons": "lessons",
    }
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
            out["main_thread"] = s[len("main thread:") :].strip()
        elif sec is None:
            out["header"].append(s)
        elif sec and s.startswith("-"):
            out[sec].append(s.lstrip("- ").strip())
    return out


# ---------------------------------------------------------------- snapshots


def _snap_time(text: str) -> str | None:
    m = re.search(r"#\s*Snapshot:.*?\((.*?)\)", text)
    return m.group(1) if m else None


def parse_status(text: str) -> dict[str, Any]:
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
    st = parse.status_text(text)  # PARTY rows: the app's one parser (names with spaces, +temp HP, flags)
    out["party"] = [
        {
            "name": m["name"],
            "active": m["active"],
            "class": m["classes"],
            "hp": m["hp"],
            "max_hp": m["max_hp"],
            "statuses": m["statuses"],
        }
        for m in cast(list[Obj], st["party"] if st else [])
    ]
    return out


def parse_inventory(text: str) -> dict[str, Any]:
    """Per member: equipped (slot, item) and notable bag items (story or uncommon+), rarest first."""
    out: dict[str, dict[str, Any]] = {}
    cur: dict[str, Any] | None = None
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
        item: dict[str, Any] = {
            "name": name,
            "rarity": rare.group(1).lower() if rare else None,
            "story": " story" in f" {s}",
        }
        if eq:
            cur["equipped"].append(dict(item, slot=eq.group(1)))
        elif item["rarity"] or item["story"]:
            cur["notable"].append(item)

    def rank(i: Obj) -> tuple[int, bool, str]:
        return -RARITY_RANK.get(i["rarity"] or "", 0), not i["story"], i["name"]

    for v in out.values():
        v["notable"].sort(key=rank)

    return out


# ---------------------------------------------------------------- payload


def story_payload(mem: Path) -> dict[str, Any]:
    files = [
        mem / "decisions.md",
        mem / "PLAYTHROUGH.md",
        mem / "snapshots" / "status.md",
        mem / "snapshots" / "inventory.md",
    ]
    with contextlib.suppress(OSError):
        files += sorted((mem / "chapters").glob("*.md"))
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

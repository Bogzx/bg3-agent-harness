"""Pending level ups: the LEVEL UP line, the out-of-combat nudge, and stored level-up plans (stdlib only).

The mod's state.status party rows carry `level_up` (the level a pending level up reaches) when the portrait
shows the level-up button (Server/State.lua levelUpTo: AvailableLevel > EocLevel or a CanLevelUp component;
verified live 2026-10-10 on Gale, Wizard 1 with 523 XP). get_status/look print

    LEVEL UP: e3 Gale (2) → level_up("Gale") uses your plan "Gale L2" (memory/party-build.md)

outside combat, and other tool results carry a short reminder every NUDGE_S seconds while one is pending
and the party is out of combat and dialogue.

Stored plans live in memory/party-build.md, one line per character level:

    - Gale L2: {"subclass": "Evocation", "spells": ["Magic Missile", "Shield"]}

level_up(who) without a plan uses the stored line for the level being reached.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Callable

NUDGE_S = 300.0
PLAN_FILE = "party-build.md"
PLAN_RE = re.compile(r"^\s*[-*]\s*(?P<name>[^:{}]+?)\s+L(?:evel\s*)?(?P<lvl>\d{1,2})\s*:\s*(?P<json>\{.*\})\s*$",
                     re.I)


def pending(status: dict | None) -> list[dict]:
    """[{uuid, name, to}] for party members with a level up waiting (dead and off-stage ones left out)."""
    out = []
    for r in (status or {}).get("party") or []:
        if not isinstance(r, dict) or r.get("dead") or r.get("off_stage"):
            continue
        to = r.get("level_up")
        if isinstance(to, (int, float)) and to > 0:
            out.append({"uuid": r.get("uuid"), "name": str(r.get("name") or "?"), "to": int(to)})
    return out


def load_plans(text: str) -> dict[tuple[str, int], dict]:
    """party-build.md text → {(name lowercased, level): plan}. Lines that aren't valid JSON are skipped."""
    out = {}
    text = re.sub(r"<!--.*?-->", "", text or "", flags=re.S)
    for ln in text.splitlines():
        m = PLAN_RE.match(ln)
        if not m:
            continue
        try:
            plan = json.loads(m.group("json"))
        except ValueError:
            continue
        if isinstance(plan, dict):
            out[(m.group("name").strip().lower(), int(m.group("lvl")))] = plan
    return out


def plan_for(memory: Path, name: str, level: int) -> dict | None:
    try:
        text = (Path(memory) / PLAN_FILE).read_text()
    except OSError:
        return None
    plans = load_plans(text)
    key = name.strip().lower()
    if (key, level) in plans:
        return plans[(key, level)]
    # "Claude" vs "Claude (Paladin)" style names: a unique prefix match
    hits = [p for (n, lv), p in plans.items() if lv == level and (n.startswith(key) or key.startswith(n))]
    return hits[0] if len(hits) == 1 else None


def line(status: dict | None, label: Callable[[str], str], memory: Path | None = None) -> str:
    """'LEVEL UP: e3 Gale (2) …' outside combat, '' otherwise or when nobody can level."""
    st = status or {}
    rows = pending(st)
    if not rows or st.get("in_combat"):
        return ""
    bits, missing = [], []
    for r in rows:
        bits.append(f"{label(r['uuid']) if r.get('uuid') else r['name']} ({r['to']})")
        if memory is not None and plan_for(memory, r["name"], r["to"]) is None:
            missing.append(f"{r['name']} L{r['to']}")
    tail = " → level_up(who) now, one call each, from your plan in memory/party-build.md"
    if missing:
        tail += f"; no plan yet for {', '.join(missing)}: write it there first (knowledge/playbooks/party-and-leveling.md)"
    return "LEVEL UP: " + ", ".join(bits) + tail


class Nudge:
    """Remembers the last status seen and says 'LEVEL UP pending' on other tool results, at most every NUDGE_S."""

    def __init__(self, every_s: float = NUDGE_S):
        self.every_s = every_s
        self.rows: list[dict] = []
        self.busy = False
        self.last = 0.0

    def observe(self, status: dict | None) -> None:
        if not isinstance(status, dict):
            return
        self.rows = pending(status)
        self.busy = bool(status.get("in_combat") or status.get("in_dialogue"))

    def text(self, now: float | None = None, shown: bool = False) -> str:
        """The reminder ('' when nothing is pending, in combat/dialogue, or shown recently). shown=True: the
        caller printed the full LEVEL UP line itself (get_status/look), so the timer restarts."""
        now = time.time() if now is None else now
        if not self.rows or self.busy:
            return ""
        if shown:
            self.last = now
            return ""
        if now - self.last < self.every_s:
            return ""
        self.last = now
        who = ", ".join(f"{r['name']} ({r['to']})" for r in self.rows)
        return f"LEVEL UP pending: {who}. Do it now while out of combat: level_up(who), one call each."

    def done(self, uuid: str) -> None:
        self.rows = [r for r in self.rows if r.get("uuid") != uuid]

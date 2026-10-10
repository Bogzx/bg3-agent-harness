"""Lessons from past fights, matched to what the player sees now (docs/ARCHITECTURE.md "Learning from fights").

memory/lessons.md holds one lesson per line, kept by the memory keeper from the player's [fight]/[lesson] notes:

    - WHEN Justiciar Soulhunter, Gauntlet of Shar, ranged: 8 Soulhunters shot us down from 20 m → open with
      Fireball on the archers, fight from cover (S033) [seen]

When a fight starts (or the party enters a region) the bridge shows the lessons whose triggers name an enemy, an
enemy race or the region: a cheap whole-word match, no model call, at most MAX_SHOWN lines, each lesson once per
fight / region visit. Fair: triggers are matched against what the combat read and the status show the player.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

MAX_SHOWN = 3
LINE_CHARS = 320

_LESSON_RE = re.compile(r"^\s*[-*]\s*WHEN\s+(?P<trig>[^:]+?)\s*:\s*(?P<body>.+?)\s*$", re.I)


@dataclass
class Lesson:
    triggers: list[str]
    body: str
    line: str

    def text(self) -> str:
        t = f"WHEN {', '.join(self.triggers)}: {self.body}"
        return t if len(t) <= LINE_CHARS else t[:LINE_CHARS - 1] + "…"


def parse(text: str) -> list[Lesson]:
    """The `- WHEN a, b: … → …` lines of lessons.md (anything else, e.g. headings and comments, is skipped)."""
    out = []
    in_comment = False
    for raw in text.splitlines():
        s = raw.strip()
        if in_comment:
            in_comment = "-->" not in s
            continue
        if s.startswith("<!--"):
            in_comment = "-->" not in s
            continue
        m = _LESSON_RE.match(raw)
        if not m:
            continue
        trig = [t.strip() for t in m.group("trig").split(",") if t.strip()]
        if trig:
            out.append(Lesson(trig, m.group("body"), s))
    return out


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(s).lower()).strip()


def _hits(trigger: str, terms: list[str]) -> bool:
    """Whole-word (or whole-phrase) match of a trigger in any term: 'Justiciar' matches 'Justiciar Soulhunter';
    'Undead' matches race 'Undead'; 'Gauntlet of Shar' matches region 'Gauntlet of Shar'. Never substring of a word
    ('Imp' does not match 'Imperial'); a plural/past ending is
    allowed ('mirror' matches 'Mirrored')."""
    t = _norm(trigger)
    if not t:
        return False
    # the last word may carry a plural/past ending: 'mirror' matches the status 'Mirrored', 'Imp' matches 'Imps'
    pat = re.compile(r"(?:^| )" + re.escape(t) + r"(?:s|es|d|ed)?(?: |$)")
    return any(pat.search(_norm(x)) for x in terms if x)


def _place_in(trigger: str, places: list[str]) -> bool:
    """A place the player sees named inside a longer trigger: S038's own note "WHEN Nightsong's Prison arrival
    ledge: …" vs the WHERE line's "Nightsong's Prison". Places only: an enemy name inside a trigger is too loose."""
    return any(_hits(p, [trigger]) for p in places if p)


def match(lessons: list[Lesson], terms: list[str], limit: int = MAX_SHOWN,
          places: list[str] = ()) -> list[Lesson]:
    """Lessons with a trigger matching any of `terms` (enemy names, races, region) or containing one of `places`,
    most matching triggers first, then the newest (later in the file) first."""
    scored = []
    for i, ls in enumerate(lessons):
        n = sum(1 for t in ls.triggers if _hits(t, terms) or _place_in(t, list(places)))
        if n:
            scored.append((-n, -i, ls))
    scored.sort(key=lambda x: (x[0], x[1]))
    return [ls for _, _, ls in scored[:limit]]


@dataclass
class Store:
    """lessons.md, re-read when its mtime changes; remembers what was shown in this fight / region visit."""
    path: Path
    _mtime: float | None = None
    _lessons: list[Lesson] = field(default_factory=list)
    shown_fight: set[str] = field(default_factory=set)
    shown_region: set[str] = field(default_factory=set)
    region: str | None = None
    in_fight: bool = False

    def lessons(self) -> list[Lesson]:
        try:
            m = self.path.stat().st_mtime
        except OSError:
            self._mtime, self._lessons = None, []
            return []
        if m != self._mtime:
            try:
                self._lessons = parse(self.path.read_text(errors="replace"))
            except OSError:
                self._lessons = []
            self._mtime = m
        return self._lessons

    def fight_lines(self, enemies: list[dict], places: list[str]) -> str:
        """'LESSONS (from past fights):\n- WHEN …' for a fight that just started; '' when nothing new matches.
        enemies: combat-read rows ({name, race, statuses: [names]}; S035's Self-Same mirrors are named like the
        party and only their [Mirrored] status tells them apart). Called once per fight start; fight_over() resets."""
        # S035 19:32: the fight start dropped the trial subregion (WHERE Self-Same Trial · Shadow-Cursed Lands ->
        # Shadow-Cursed Lands), so the place the party stood in before the fight counts too.
        before = [p.strip() for p in (self.region or "").split("·")]
        where = [p for p in places + before if p]
        terms = [str(e.get(k) or "") for e in enemies for k in ("name", "race")] + where
        terms += [str(x) for e in enemies for x in e.get("statuses") or []]
        new = [ls for ls in match(self.lessons(), terms, limit=len(self._lessons) or 1, places=where)
               if ls.line not in self.shown_fight][:MAX_SHOWN]
        self.in_fight = True
        if not new:
            return ""
        self.shown_fight.update(ls.line for ls in new)
        return "LESSONS (from past fights, memory/lessons.md):\n" + "\n".join("- " + ls.text() for ls in new)

    def fight_over(self) -> None:
        self.in_fight = False
        self.shown_fight.clear()

    def region_lines(self, places: list[str]) -> str:
        """Lessons whose trigger names the place (subregion and/or region), once per place entry, outside fights."""
        places = [p for p in places if p]
        key = " · ".join(places)
        if not key or key == self.region:
            return ""
        self.region = key
        if self.in_fight:
            return ""
        new = [ls for ls in match(self.lessons(), places, places=places) if ls.line not in self.shown_region]
        if not new:
            return ""
        self.shown_region.update(ls.line for ls in new)
        return f"LESSONS for {key} (memory/lessons.md):\n" + "\n".join("- " + ls.text() for ls in new)

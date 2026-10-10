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
import os
import re
import time
from pathlib import Path
from typing import Callable

NUDGE_S = 300.0
REFRESH_S = 60.0   # re-read state.status for a new level up at most this often (one ~20 ms read)
HINT_S = 600.0     # PARTY PLAN hint (missing plan lines, composition gap) at most this often
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
    """Remembers the last status seen and says 'LEVEL UP pending' on other tool results, at most every NUDGE_S
    (a level up that was not pending before is said at once). stale() asks the server to re-read state.status
    every REFRESH_S: XP arrives mid-play (a quest reward, a fight) while the agent reads no status, and in S010
    Claude's L3 went unnoticed until a screenshot, Gale's and Shadowheart's until the wrap-up look."""

    def __init__(self, every_s: float = NUDGE_S, refresh_s: float = REFRESH_S):
        self.every_s = every_s
        self.refresh_s = refresh_s
        self.rows: list[dict] = []
        self.levels: list[dict] = []   # [{name, level, classes}] of the active party (plan_hint)
        self.busy = False
        self.last = 0.0
        self.seen_at = 0.0
        self.said: set[tuple] = set()  # (uuid, to) already reminded or shown
        self.hint_at = 0.0

    def stale(self, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        return now - self.seen_at >= self.refresh_s

    def observe(self, status: dict | None, now: float | None = None) -> None:
        if not isinstance(status, dict):
            return
        self.seen_at = time.time() if now is None else now
        self.rows = pending(status)
        self.busy = bool(status.get("in_combat") or status.get("in_dialogue"))
        lv = [{"name": str(r.get("name") or "?"), "level": r.get("level"), "classes": r.get("classes")}
              for r in status.get("party") or [] if isinstance(r, dict) and not r.get("dead")
              and not r.get("off_stage")]
        if lv:
            self.levels = lv

    def text(self, now: float | None = None, shown: bool = False) -> str:
        """The reminder ('' when nothing is pending, in combat/dialogue, or shown recently). shown=True: the
        caller printed the full LEVEL UP line itself (get_status/look), so the timer restarts."""
        now = time.time() if now is None else now
        if not self.rows or self.busy:
            return ""
        keys = {(r.get("uuid"), r["to"]) for r in self.rows}
        fresh = bool(keys - self.said)
        self.said |= keys
        if shown:
            self.last = now
            return ""
        if not fresh and now - self.last < self.every_s:
            return ""
        self.last = now
        who = ", ".join(f"{r['name']} ({r['to']})" for r in self.rows)
        return f"LEVEL UP pending: {who}. Do it now while out of combat: level_up(who), one call each."

    def done(self, uuid: str) -> None:
        self.rows = [r for r in self.rows if r.get("uuid") != uuid]

    def plan_hint(self, memory: Path | None, now: float | None = None) -> str:
        """For a NOTES WINDOW (walking time): the missing party-build.md plan lines for the next two levels and
        the composition gap, at most every HINT_S ('' when all is written or nothing is known yet)."""
        now = time.time() if now is None else now
        if memory is None or not self.levels or self.busy or now - self.hint_at < HINT_S:
            return ""
        text = hint_text(memory, self.levels)
        if text:
            self.hint_at = now
        return text


# Roles of knowledge/playbooks/party-and-leveling.md by class (a Paladin tanks, a Cleric heals, ...).
ROLES = {
    "frontline": {"paladin", "fighter", "barbarian", "monk"},
    "healer": {"cleric", "druid"},
    "arcane": {"wizard", "warlock", "sorcerer"},
    "skills": {"rogue", "bard", "ranger"},
}
ROLE_FIX = {
    "frontline": "frontline tank (Lae'zel, Karlach)",
    "healer": "healer (Shadowheart, Jaheira, Halsin)",
    "arcane": "arcane control (Gale, Wyll)",
    "skills": "skills/locks/traps (Astarion)",
}


def _class_names(classes) -> list[str]:
    out = []
    for c in classes or []:
        name = c.get("name") or c.get("class") if isinstance(c, dict) else c
        if name:
            out.append(re.split(r"[\s(\d]", str(name).strip(), maxsplit=1)[0].lower())
    return out


def composition_gaps(levels: list[dict]) -> list[str]:
    """Roles no active member covers (by main class), in playbook order; [] when classes are unknown."""
    have = set()
    known = False
    for m in levels:
        names = _class_names(m.get("classes"))
        known = known or bool(names)
        for role, cls in ROLES.items():
            if names and names[0] in cls:
                have.add(role)
    return [r for r in ROLES if r not in have] if known else []


def hint_text(memory: Path, levels: list[dict]) -> str:
    try:
        text = (Path(memory) / PLAN_FILE).read_text()
    except OSError:
        text = ""
    plans = load_plans(text)
    missing = []
    for m in levels:
        lvl = m.get("level")
        if not isinstance(lvl, (int, float)):
            continue
        key = m["name"].strip().lower()
        for nxt in (int(lvl) + 1, int(lvl) + 2):
            if nxt > 12:
                continue
            if (key, nxt) not in plans and not any(lv == nxt and (n.startswith(key) or key.startswith(n))
                                                   for n, lv in plans):
                missing.append(f"{m['name']} L{nxt}")
    gaps = composition_gaps(levels)
    has_comp = re.search(r"^##\s*Party composition", text, re.M | re.I) is not None
    bits = []
    if missing:
        bits.append("no plan line yet for " + ", ".join(missing) + ' (write "- Gale L4: {json}" lines under '
                    "## Level-up plans, from knowledge/builds.md)")
    if gaps and not has_comp:
        bits.append("party lacks " + ", ".join(ROLE_FIX[g] for g in gaps)
                    + ": note who fills it under ## Party composition")
    if not bits:
        return ""
    return "PARTY PLAN (party-build.md, now while walking): " + "; ".join(bits) + \
        " (knowledge/playbooks/party-and-leveling.md)"


# ---- spell picks on the level-up page (mouse clicks) ----
#
# The spell buttons pass a MultiBinding (selector + spell) that the mod cannot build, so ui.level_up leaves
# result.needs_clicks = [{name, cantrips}] and the level_up tool clicks them here, then reruns ui.level_up.
# ui.level_up_spell_probe {name} shows the spell's selector (on a feat level it first goes back from the feat tab
# to the overview; a feat's own selector opens on the feat tab) and says which of its buttons the cursor is over
# (index in the selector's buttons, row-major in `cols` columns of `cell` page units), and `near` when the cursor
# is over the list or one of its parents up to its scroll viewer. The page lays out at 3840x2160 and is scaled to
# the window (88-unit buttons were 44 px at 1920x1080, verified live). The panel is centred by a transform that
# depends on its width, so the list is not always where Gale's Wizard 2 page had it (x 0.16-0.34, y 0.42-0.54):
# S018 (feat levels) never found it in 400 probes (~110 s, the MCP timeout). Now: big steps until the cursor is
# near the list or on a button, then button-sized ones; the last place a button was found is remembered (also
# across bridge restarts, in runtime/levelup_clicks.json); a target scrolled out of view is brought in with the
# mouse wheel; and the whole thing stops at CLICK_BUDGET_S so level_up answers well inside the tool timeout.

DESIGN_H = 2160.0
SEARCH_FROM = (0.25, 0.47)
MAX_SEARCH_PROBES = 400
COARSE_STEP = 4.0        # coarse search step, in buttons (the whole 1080p window in ~66 probes)
FINE_PROBES = 160        # button-sized steps around the coarse hit
CLICK_BUDGET_S = 45.0    # all picks of one level_up call
SCROLL_TRIES = 6
WHEEL_NOTCHES = 3
INTRO_CLICK = (0.5, 0.85)  # window fraction clicked to skip the level-up intro (empty floor under the character)
INTRO_SKIP_TRIES = 2
FROZEN_SWEEP = (0.5, 0.30, 0.75)   # a frozen page's list: centre column x, swept from y0 to y1 (window fractions)

Rpc = Callable[[str, dict], dict]
MEMO = Path(os.environ.get("BG3AI_RUNTIME", Path(__file__).resolve().parents[3] / "runtime")) / "levelup_clicks.json"
_last_hit: dict[str, tuple[float, float]] = {}   # "spells"/"cantrips"/"feat" -> window fraction of the last button hit


class SpellClickError(RuntimeError):
    """A spell pick could not be clicked or did not take."""


def _cell_of(index: int, cols: int) -> tuple[int, int]:
    return (index - 1) % max(1, cols), (index - 1) // max(1, cols)


def spiral(cx: float, cy: float, step: float, w: int, h: int, limit: int = MAX_SEARCH_PROBES) -> list[tuple[int, int]]:
    """Points on square rings around (cx, cy), `step` apart, inside the window, nearest first."""
    out: list[tuple[int, int]] = []
    rings = int(max(w, h) / max(step, 1.0)) + 1
    for r in range(rings):
        ring = [(0, 0)] if r == 0 else (
            [(dx, -r) for dx in range(-r, r + 1)] + [(r, dy) for dy in range(-r + 1, r + 1)]
            + [(dx, r) for dx in range(r - 1, -r - 1, -1)] + [(-r, dy) for dy in range(r - 1, -r, -1)])
        for dx, dy in ring:
            x, y = round(cx + dx * step), round(cy + dy * step)
            if 0 <= x < w and 0 <= y < h:
                out.append((x, y))
                if len(out) >= limit:
                    return out
    return out


def _load_memo(path: Path | None) -> None:
    if path is None or _last_hit:
        return
    try:
        data = json.loads(path.read_text())
        for k, v in data.items():
            if isinstance(v, list) and len(v) == 2:
                _last_hit[k] = (float(v[0]), float(v[1]))
    except (OSError, ValueError):
        pass


def _save_memo(path: Path | None) -> None:
    if path is None:
        return
    try:
        path.write_text(json.dumps({k: list(v) for k, v in _last_hit.items()}))
    except OSError:
        pass


class SpellClicker:
    def __init__(self, rpc: Rpc, inp, log: Callable[[str], None] = lambda m: None,
                 budget_s: float = CLICK_BUDGET_S, clock: Callable[[], float] = time.monotonic):
        self.rpc, self.inp, self.log = rpc, inp, log
        self.origin: tuple[float, float] | None = None   # centre of button 1 (window px)
        self.cell: float | None = None                   # button pitch (window px)
        self.probes = 0
        self.frozen = False                              # page stuck in its intro, made hit-testable (centred)
        self.clock = clock
        self.deadline = clock() + budget_s

    def _check_time(self, what: str) -> None:
        if self.clock() > self.deadline:
            raise SpellClickError(f"{what}: out of time ({self.probes} probes)")

    def probe(self, args: dict, at: tuple[int, int] | None = None) -> dict:
        if at is not None:
            self.inp.move(*at)
            self.inp.wait_frames(2)
            self.probes += 1
        return self.rpc("ui.level_up_spell_probe", args)

    def skip_intro(self, args: dict, p: dict) -> dict:
        """The page opens with a "Level Up!" intro whose panels stay off-screen until LevelUpStep is IntroComplete;
        it never finished by itself in S021 (still up 10 s after level_up opened the page). A click skips it
        (UISkipDialog, what the player did by hand in S021). Only clicked while the intro is up."""
        def done(q: dict) -> bool:
            return not q.get("step") or q.get("step") == "IntroComplete" or bool(q.get("placed"))

        for _ in range(0 if p.get("fade") else INTRO_SKIP_TRIES):
            if done(p):
                return p
            w, h = self.inp.window_size()
            self.log(f"level-up intro still up (step {p.get('step')}): clicking to skip it")
            self.inp.click(round(w * INTRO_CLICK[0]), round(h * INTRO_CLICK[1]))
            for _ in range(10):
                self.inp.wait_frames(6)
                p = self.probe(args)
                if done(p):
                    self.inp.wait_frames(30)   # the panels slide in (LevelUpAfterItems storyboards)
                    return self.probe(args)
        if done(p):
            return p
        # Clicks did not end it: the page was opened during a screen fade and froze in its intro (live 2026-10-10,
        # Wyll L4 right after swap_party's camp dialogue: fade at open, step stuck at Started, nothing hit-testable,
        # clicks ignored; the mod now waits for the fade before opening). Make the frozen page hit-testable; its
        # lists then sit centred under the banner (x 0.41-0.58, y 0.44), so the search sweeps the centre first.
        self.log(f"level-up page frozen in its intro (step {p.get('step')}, fade {p.get('fade')}): making it clickable")
        q = self.rpc("ui.level_up_spell_probe", dict(args, intro="hit"))
        self.frozen = bool(q.get("hit_set"))
        return self.probe(args)

    def ready(self, args: dict) -> dict:
        """Probe until the selector is shown and the spell's button exists (tab and sub-tab switches take frames)."""
        p: dict = {}
        for _ in range(16):
            p = self.skip_intro(args, self.probe(args))
            if p.get("placed") or p.get("index"):
                return p
            self.inp.wait_frames(6)
        raise SpellClickError(f"spell {args['name']}: its button never appeared ({p})")

    def _edge(self, args: dict, k: int, inside: tuple[int, int], outside: tuple[int, int]) -> tuple[int, int]:
        (xi, yi), (xo, yo) = inside, outside
        while abs(xo - xi) > 1 or abs(yo - yi) > 1:
            m = ((xi + xo) // 2, (yi + yo) // 2)
            if self.probe(args, m).get("hovered") == k:
                xi, yi = m
            else:
                xo, yo = m
        return xi, yi

    def _measure(self, args: dict, k: int, at: tuple[int, int], guess: float) -> tuple[float, float, float]:
        """Centre and width of hovered button k from its edges."""
        x, y = at
        span = round(guess * 1.5) + 2
        left = self._edge(args, k, (x, y), (x - span, y))[0]
        right = self._edge(args, k, (x, y), (x + span, y))[0]
        cx = (left + right) / 2
        top = self._edge(args, k, (round(cx), y), (round(cx), y - span))[1]
        bottom = self._edge(args, k, (round(cx), y), (round(cx), y + span))[1]
        return cx, (top + bottom) / 2, float(max(right - left, bottom - top) + 1)

    def _target(self, p: dict) -> tuple[int, int]:
        col, row = _cell_of(p["index"], p.get("cols") or 1)
        assert self.origin is not None and self.cell is not None
        return round(self.origin[0] + col * self.cell), round(self.origin[1] + row * self.cell)

    def _over(self, args: dict, at: tuple[int, int]) -> bool:
        w, h = self.inp.window_size()
        if not (0 <= at[0] < w and 0 <= at[1] < h):
            return False
        return bool(self.probe(args, at).get("over"))

    def _find(self, args: dict, kind: str, guess: float) -> tuple[tuple[int, int], int]:
        """A point over one of the selector's buttons and that button's index: coarse steps from the last hit
        (or SEARCH_FROM) until on a button or near the list, then button-sized steps around there."""
        w, h = self.inp.window_size()
        fx, fy = _last_hit.get(kind) or SEARCH_FROM
        if self.frozen:
            x, y = round(w * FROZEN_SWEEP[0]), h * FROZEN_SWEEP[1]
            while y < h * FROZEN_SWEEP[2]:
                self._check_time(f"spell {args['name']}: no spell button found under the cursor")
                q = self.probe(args, (x, round(y)))
                if q.get("hovered"):
                    return (x, round(y)), q["hovered"]
                y += guess * 0.8
        near_at = None
        for pt in spiral(fx * w, fy * h, guess * COARSE_STEP, w, h):
            self._check_time(f"spell {args['name']}: no spell button found under the cursor")
            q = self.probe(args, pt)
            if q.get("hovered"):
                return pt, q["hovered"]
            if q.get("near"):
                near_at = pt
                break
        seeds = [near_at] if near_at else []
        seeds.append((round(fx * w), round(fy * h)))
        for seed in seeds:
            for pt in spiral(seed[0], seed[1], guess * 0.9, w, h, limit=FINE_PROBES):
                self._check_time(f"spell {args['name']}: no spell button found under the cursor")
                q = self.probe(args, pt)
                if q.get("hovered"):
                    return pt, q["hovered"]
        raise SpellClickError(f"spell {args['name']}: no spell button found under the cursor ({self.probes} probes)")

    def _grid(self, args: dict, p: dict, pt: tuple[int, int], k: int, guess: float) -> tuple[int, int] | None:
        """Grid origin and pitch from hovered button k at pt; the target's spot if the cursor there is over it."""
        kc, kr = _cell_of(k, p.get("cols") or 1)
        span = round(guess * 1.5) + 2
        left = self._edge(args, k, pt, (pt[0] - span, pt[1]))[0]
        top = self._edge(args, k, pt, (pt[0], pt[1] - span))[1]
        at = None
        for attempt in ("edges", "measured"):
            if attempt == "edges":
                self.cell, cx, cy = guess, left + guess / 2, top + guess / 2
            else:
                cx, cy, width = self._measure(args, k, pt, guess)
                self.cell = width if abs(width - guess) < guess * 0.5 else guess
            self.origin = (cx - kc * self.cell, cy - kr * self.cell)
            at = self._target(p)
            if self._over(args, at):
                self.log(f"spell buttons: #{k} at ({cx:.0f},{cy:.0f}), pitch {self.cell:.1f}px, {p.get('cols')} "
                         f"columns ({attempt})")
                return at
        return None

    def locate(self, args: dict, p: dict, kind: str = "spells") -> tuple[int, int]:
        """Window coordinates over the spell's button, hover-checked."""
        if self.origin is not None:
            at = self._target(p)
            if self._over(args, at):
                return at
            self.origin = None   # the list moved (another selector, a scroll): search again
        w, h = self.inp.window_size()
        cell_units = float((p.get("cell") or {}).get("w") or 88.0)
        guess = cell_units * min(w / (DESIGN_H * 16 / 9), h / DESIGN_H)
        pt, k = self._find(args, kind, guess)
        if not self.frozen:   # a frozen page's centred layout says nothing about where a normal page has its list
            _last_hit[kind] = (pt[0] / w, pt[1] / h)
        cols = p.get("cols") or 1
        for _ in range(SCROLL_TRIES + 1):
            self._check_time(f"spell {args['name']}")
            at = self._grid(args, p, pt, k, guess)
            if at is not None:
                return at
            # The target is not where the grid says: a long list scrolled out of view. Wheel the list toward it
            # (the cursor on button k, inside the list) and measure again from whatever button is under it now.
            down = _cell_of(p["index"], cols)[1] > _cell_of(k, cols)[1]
            self.inp.wheel(-WHEEL_NOTCHES if down else WHEEL_NOTCHES, pt[0], pt[1])
            self.inp.wait_frames(4)
            q = self.probe(args, pt)
            k2 = q.get("hovered")
            if not k2:
                pt, k2 = self._find(args, kind, guess)
            if k2 == k:
                break   # the wheel moved nothing: the list does not scroll that way
            self.log(f"spell buttons: scrolled {'down' if down else 'up'} (#{k} -> #{k2} under the cursor)")
            k = k2
        self.origin = None
        raise SpellClickError(f"spell {args['name']}: its button is not under the computed spot (list scrolled?)")

    def pick(self, need: dict) -> None:
        args = {"name": need["name"]}
        if need.get("cantrips") is not None:
            args["cantrips"] = need["cantrips"]
        self._check_time(f"spell {need['name']}")
        p = self.ready(args)
        if p.get("placed"):
            return
        if p.get("enabled") is False:
            raise SpellClickError(f"spell {need['name']}: its button is disabled (no open slot?)")
        kind = "feat" if p.get("feat") else ("cantrips" if need.get("cantrips") else "spells")
        at = self.locate(args, p, kind)
        self.inp.click(*at)
        self.inp.wait_frames(8)
        for _ in range(10):
            if self.probe(args).get("placed"):
                self.log(f"picked {need['name']} (click at {at})")
                return
            self.inp.wait_frames(6)
        raise SpellClickError(f"spell {need['name']}: clicked at {at} but it is not picked")


def click_spells(rpc: Rpc, inp, needs: list[dict], log: Callable[[str], None] = lambda m: None,
                 budget_s: float = CLICK_BUDGET_S) -> dict:
    """Click each needed spell on the open level-up page. {picked: [...], failed: {name: why}, probes, seconds}."""
    memo = MEMO
    _load_memo(memo)
    t0 = time.monotonic()
    c = SpellClicker(rpc, inp, log, budget_s=budget_s)
    picked, failed = [], {}
    for need in needs:
        try:
            c.pick(need)
            picked.append(need["name"])
        except Exception as e:  # noqa: BLE001 - one bad pick must not hide the others
            failed[need["name"]] = str(e)
    try:
        inp.move(*(round(v / 2) for v in inp.window_size()))   # off the buttons (tooltips)
    except Exception:  # noqa: BLE001
        pass
    if picked:
        _save_memo(memo)
    return {"picked": picked, "failed": failed, "probes": c.probes, "seconds": round(time.monotonic() - t0, 1)}

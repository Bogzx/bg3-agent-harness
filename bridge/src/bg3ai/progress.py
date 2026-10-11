"""Story progress per hour, from what the player can see (stdlib only; runner + dashboard use it).

The runner polls the game every POLL_S seconds during a session (one server eval: state.status,
state.journal, state.map, nav.markers, nav.explored; one client eval: ui.state for the minimap subregion). Every op is
one of the mod's player-visible projections: the journal only has unlocked steps, the map only unlocked
waypoints, markers only active objectives. Each poll is diffed against the playthrough's `seen` state
(persisted in runtime/sessions/<pt>/progress_state.json) and every NEW thing becomes a progress event.

Seen-sets only grow, so reloading an earlier save and redoing a quest step never scores twice, and the
first poll of a playthrough is a silent baseline.

Weights (points per event; deliberately simple, story-weighted):

    region     15  a new region/level (Nautiloid -> Wilderness -> ...); the big story beats
    quest_done 10  a journal quest marked finished
    level_up    8  a party member's level went up (per member per level)
    waypoint    5  a newly unlocked waypoint
    companion   5  a new party member
    quest_new   3  a new journal quest
    area        3  a new minimap subregion ("Ship Hold", "Blighted Village")
    quest_step  2  a new journal entry in a known quest (by its text: steps come in the quest's order, not
                   the order they unlocked; a reload that loses a step and redoes it never scores twice)
    objective   2  an active quest objective marker disappeared (done or superseded)
    obj_moved   1  an objective marker moved >= OBJ_MOVED_M to a spot it never had (the next leg of it:
                   S001 opened the first door and the "Escape Route" marker jumped 67 m on, scoring 0)
    explore     1  per EXPLORE_CELLS new explored 4x4 m cells on a level (the party's own breadcrumbs,
                   nav.explored; ~130 m2, a corridor of ~30 m)
    talk        2  a conversation with an NPC the party never talked to (dialogue_started speakers, not
                   automated barks; S015/S018: freeing Barcus, Dror, Zevlor scored nothing)
    kill        1  a visible non-party character died that never died before (events.since `died`; S018
                   spent 23 min killing the three goblin leaders and their camp for 0 points)
    story_kill  5  … instead of kill: someone the party has talked to (S040/S041: Balthazar scored 1 point, like
                   each of his 20 skeletons)
    key_item    5  a quest item (story mark) came into the party's hands for the first time (item_added story;
                   S036: the Gauntlet's trials won all 4 Umbral Gems and scored 10 points in 25 min, 15.6 min
                   "stuck" while crossing the Faith-Leap Trial)
    puzzle      2  a door/wall in view opened by the world, not by the party (door_opened: a lever, a trial won,
                   the Grand Mausoleum's paintings), once per door
    fight_won   6  a fight ended (combat_ended) with >= FIGHT_WON_KILLS first-time kills in it
    boss_fight 15  … instead of fight_won: someone the party had talked to died in it (S041: the Balthazar fight,
                   won on try 4, scored 28/h; a refight after a reload kills nobody new, so it never scores twice)

Not progress: summons joining (Spiritual Weapon is in DB_PartyMembers, S017 scored it as a companion), the
levels a recruit is caught up to on joining (S021: Karlach -> 2, 3, 4 = 24 points within 90 s), and an
objective marker riding on a walking NPC (S015: "Volo moved on" 6x in 75 s; one obj_moved per marker per
OBJ_MOVED_COOLDOWN_S).

points/hour = points / session hours. A rough scale: the Nautiloid (~1 h for a human) is ~80-100 points.

Stuck signal: no progress event for STUCK_MIN minutes while the agent is active (it made at least
STUCK_MIN_ACTIONS tool calls in that time, per the bridge metrics file). The runner writes a `stuck`
record when an episode starts and an `unstuck` record when progress resumes.

Timeline records (Sxxx.progress.jsonl, one JSON per line):
    {"type":"progress","t","kind","what","points"}
    {"type":"poll","t","ok"}                         (only failures and every 10th success, to stay small)
    {"type":"stuck","t","since","idle_min","actions"} / {"type":"unstuck","t","stuck_min"}
"""

from __future__ import annotations

import json
import math
import os
import time
from pathlib import Path
from typing import Any, Callable

WEIGHTS = {"region": 15, "quest_done": 10, "level_up": 8, "waypoint": 5, "companion": 5, "quest_new": 3,
           "area": 3, "quest_step": 2, "objective": 2, "obj_moved": 1, "explore": 1, "talk": 2, "kill": 1,
           "key_item": 5, "puzzle": 2, "story_kill": 5, "fight_won": 6, "boss_fight": 15}
EVENT_KINDS = ("died", "dialogue_started", "item_added", "door_opened", "combat_started", "combat_ended")
FIGHT_WON_KILLS = 3
EXPLORE_CELLS = 8
OBJ_MOVED_M = 10.0
OBJ_POS_CAP = 20  # remembered spots per objective
OBJ_MOVED_COOLDOWN_S = 300.0
JOIN_GRACE_S = 180.0  # level-ups of a party member within this long of joining are the game catching it up
EVENTS_LIMIT = 500
STEP_KEY_LEN = 100  # journal steps are remembered by their first 100 characters
POLL_S = float(os.environ.get("BG3AI_PROGRESS_POLL_S", "15") or 15)
STUCK_MIN = float(os.environ.get("BG3AI_STUCK_MINUTES", "15") or 15)
STUCK_MIN_ACTIONS = 10

SERVER_OPS = (("status", "state.status"), ("journal", "state.journal"), ("map", "state.map"),
              ("markers", "nav.markers"), ("explored", "nav.explored"))


# ---------------------------------------------------------------------------
# reading the game
# ---------------------------------------------------------------------------

def _bracket(payload: str) -> str:
    eq = "="
    while f"]{eq}]" in payload:
        eq += "="
    return f"[{eq}[{payload}]{eq}]"


def combined_code(ops: list) -> str:
    """One Lua expression running several BG3AI ops (a name, or (name, args)): a JSON array of their replies."""
    ops = [o if isinstance(o, tuple) else (o, {}) for o in ops]
    parts = [f"R({_bracket(json.dumps({'op': op, 'args': a}, separators=(',', ':')))})" for op, a in ops]
    return "local R=Mods.BG3AI.BG3AI.rpc return '['.." + "..','..".join(parts) + "..']'"


_events = {"seq": None}  # this process's cursor into the mod's event ring (events.since is non-destructive)


def read_game(lua_eval: Callable[..., str]) -> dict | None:
    """{status, journal, map, markers, explored, events, ui} from two evals, or None when the game has no
    session. events = the `died` / `dialogue_started` events since the last call (None on the first call,
    which only sets the cursor). lua_eval(code, context) -> str (the runner's or bridge's LuaDebug eval)."""
    since = _events["seq"]
    ops: list = [op for _, op in SERVER_OPS]
    ops.append(("events.since", {"seq": since if since is not None else 2 ** 31, "limit": EVENTS_LIMIT}))
    raw = lua_eval(combined_code(ops), "server")
    replies = json.loads(raw) if raw and raw not in ("nil", "") else []
    got: dict[str, Any] = {}
    for (key, _), r in zip(SERVER_OPS, replies):
        got[key] = r.get("result") if isinstance(r, dict) and r.get("ok") else None
    if got.get("status") is None:
        return None
    er = replies[len(SERVER_OPS)] if len(replies) > len(SERVER_OPS) else None
    er = er.get("result") if isinstance(er, dict) and er.get("ok") else None
    if isinstance(er, dict) and isinstance(er.get("latest"), int):
        evs = [e for e in er.get("events") or [] if isinstance(e, dict)]
        if since is None or er["latest"] < since:
            # first call, or the mod's Lua restarted (seq began again): start from here
            got["events"] = None if since is None else [e for e in evs if e.get("kind") in EVENT_KINDS]
            _events["seq"] = er["latest"] if since is None else max([e.get("seq", 0) for e in evs] + [0])
        else:
            got["events"] = [e for e in evs if e.get("kind") in EVENT_KINDS]
            _events["seq"] = max([e.get("seq", since) for e in evs] + [since])
    try:
        r = json.loads(lua_eval(combined_code(["ui.state"]), "client"))[0]
        got["ui"] = r.get("result") if r.get("ok") else None
    except Exception:  # noqa: BLE001 - the subregion is optional
        got["ui"] = None
    return got


# ---------------------------------------------------------------------------
# diffing
# ---------------------------------------------------------------------------

def empty_seen() -> dict:
    return {"regions": [], "areas": [], "waypoints": [], "quests": {}, "objectives_active": [],
            "objectives_done": [], "levels": {}, "explored": {}, "objective_pos": {}, "objective_moved_t": {},
            "joined": {}, "talked": [], "killed": [], "key_items": [], "puzzles": [], "baseline": False,
            "quests_unaccepted": True,
            "fight": {"kills": 0, "story": ""}}


def _step_key(text: str) -> str:
    return " ".join(text.split())[:STEP_KEY_LEN]


def _quests(journal: Any) -> dict:
    qs = journal.get("quests", journal) if isinstance(journal, dict) else journal
    out: dict = {}
    for q in qs or []:
        if isinstance(q, dict) and q.get("title"):
            texts = [str(x) for x in q.get("steps") or []]
            keys = [_step_key(x) for x in texts]
            o = out.get(str(q["title"]))
            if o is None:
                out[str(q["title"])] = {"steps": len(texts), "done": bool(q.get("done")),
                                        "last": (texts or [""])[-1][:120], "texts": keys}
                continue
            # Two journal quests with one title (Daughter of Darkness: the party's and Shadowheart's own "I …"
            # entries; acceptance 01:07): one row, the union of their steps, done only when both are closed.
            o["texts"] = o["texts"] + [k for k in keys if k not in o["texts"]]
            o["steps"], o["done"] = len(o["texts"]), o["done"] and bool(q.get("done"))
            o["last"] = (texts or [o["last"]])[-1][:120]
    return out


def _markers(m: Any) -> list[str] | None:
    ms = m.get("markers") if isinstance(m, dict) else m
    if not isinstance(ms, list):
        return None
    return sorted({f"{x.get('label') or x.get('objective') or '?'}|{x.get('quest') or ''}" for x in ms if isinstance(x, dict)})


def _objective_moves(seen: dict, m: Any, add: Callable, silent: bool, now: float) -> None:
    """obj_moved: an objective marker of this level standing >= OBJ_MOVED_M from every spot it had before."""
    ms = m.get("markers") if isinstance(m, dict) else m
    if not isinstance(ms, list):
        return
    for x in ms:
        p = x.get("pos") if isinstance(x, dict) else None
        if not (isinstance(p, list) and len(p) >= 3):
            continue
        key = f"{x.get('label') or x.get('objective') or '?'}|{x.get('quest') or ''}"
        spots = seen["objective_pos"].setdefault(key, [])
        here = [round(float(p[0]), 1), round(float(p[1]), 1), round(float(p[2]), 1)]
        if all(math.dist(here, s) >= OBJ_MOVED_M for s in spots):
            if spots and now - seen["objective_moved_t"].get(key, -1e18) >= OBJ_MOVED_COOLDOWN_S:
                add("obj_moved", f"{key.split('|')[0]} moved on", silent)
                seen["objective_moved_t"][key] = now
            spots.append(here)
            del spots[:-OBJ_POS_CAP]


def _explored(seen: dict, ex: Any, add: Callable, silent: bool) -> None:
    """explore: one event per EXPLORE_CELLS new breadcrumb cells on a level (credited = the best count
    scored so far, so reloading an earlier save never scores the same ground twice)."""
    if not isinstance(ex, dict) or not ex.get("level") or not isinstance(ex.get("cells"), (int, float)):
        return
    lvl, cells = str(ex["level"]), int(ex["cells"])
    credited = seen["explored"].get(lvl)
    if credited is None:
        # a level first seen now: its cells so far are new ground, unless this is the silent baseline
        credited = cells if (silent or not seen["baseline"]) else 0
    while cells >= credited + EXPLORE_CELLS:
        credited += EXPLORE_CELLS
        add("explore", f"{lvl}: {credited} cells explored", silent)
    seen["explored"][lvl] = credited


def _game_events(seen: dict, evs: Any, add: Callable, party: set[str]) -> None:
    """talk / kill / key_item / puzzle from the mod's player-visible events (dialogue_started speakers, died,
    item_added story, door_opened)."""
    fight = seen.setdefault("fight", {"kills": 0, "story": ""})
    for e in evs if isinstance(evs, list) else []:
        d = e.get("data") if isinstance(e, dict) else None
        if not isinstance(d, dict):
            continue
        if e.get("kind") == "combat_started":
            fight.update(kills=0, story="")
        elif e.get("kind") == "combat_ended":
            if fight.get("story"):
                add("boss_fight", f"fight won ({fight['story']})")
            elif fight.get("kills", 0) >= FIGHT_WON_KILLS:
                add("fight_won", f"fight won ({fight['kills']} down)")
            fight.update(kills=0, story="")
        elif e.get("kind") == "died":
            u, name = d.get("uuid"), str(d.get("name") or "")
            if u and u not in party and name not in party and u not in seen["killed"]:
                seen["killed"].append(u)
                # someone the party has talked to (Balthazar, a camp leader): a story beat, not a mook
                add("story_kill" if u in seen["talked"] else "kill", name or "someone")
                fight["kills"] = fight.get("kills", 0) + 1
                if u in seen["talked"]:
                    fight["story"] = name or "someone"
        elif e.get("kind") == "item_added" and d.get("story"):
            it = d.get("item") if isinstance(d.get("item"), dict) else {}
            u = it.get("uuid")
            if u and u not in seen["key_items"]:
                seen["key_items"].append(u)  # passed between members or reloaded: still once
                add("key_item", str(it.get("name") or "a quest item"))
        elif e.get("kind") == "door_opened":
            it = d.get("item") if isinstance(d.get("item"), dict) else {}
            u = it.get("uuid")
            if u and u not in seen["puzzles"]:
                seen["puzzles"].append(u)
                add("puzzle", f"{it.get('name') or 'a door'} opened")
        elif e.get("kind") == "dialogue_started" and not d.get("automated"):
            for sp in d.get("speakers") or []:
                u, name = (sp.get("uuid"), str(sp.get("name") or "")) if isinstance(sp, dict) else (None, "")
                if u and u not in party and name not in party and u not in seen["talked"]:
                    seen["talked"].append(u)
                    add("talk", name or "someone")


def diff(seen: dict, got: dict, now: float | None = None) -> tuple[dict, list[dict]]:
    """(new seen, events) for one poll. Events: {kind, what, points}. The first call is a silent baseline."""
    now = time.time() if now is None else now
    seen = json.loads(json.dumps(seen or empty_seen()))
    # keys added after a playthrough started baseline silently on their first poll (no back-scoring)
    fresh = {k for k in empty_seen() if k not in seen}
    for k, v in empty_seen().items():
        seen.setdefault(k, v)
    ev: list[dict] = []
    quiet = not seen["baseline"]

    def add(kind: str, what: str, silent: bool = False) -> None:
        if not quiet and not silent:
            ev.append({"kind": kind, "what": what, "points": WEIGHTS[kind]})

    st = got.get("status") or {}
    region = st.get("region") or st.get("level")
    if region and region not in seen["regions"]:
        seen["regions"].append(region)
        add("region", str(region))
    ui = got.get("ui") or {}
    ex = got.get("explored") if isinstance(got.get("explored"), dict) else {}
    sub = (ui.get("region") if ui.get("screen") in (None, "game") else None) or st.get("subregion") \
        or ex.get("subregion")
    if sub:
        key = f"{region}|{sub}"
        if key not in seen["areas"]:
            seen["areas"].append(key)
            add("area", str(sub))
    party: set[str] = set()
    for c in st.get("party") or []:
        if not isinstance(c, dict) or not c.get("name"):
            continue
        party.update(str(x) for x in (c.get("uuid"), c.get("name")) if x)
        if c.get("summon"):
            continue  # Spiritual Weapon, familiars: in DB_PartyMembers, never a companion
        name, lvl = str(c["name"]), c.get("level")
        old = seen["levels"].get(name)
        if old is None:
            add("companion", name)
            seen["levels"][name] = lvl
            if not quiet:
                seen["joined"][name] = now  # (the baseline party has been with us all along)
        elif isinstance(lvl, (int, float)) and isinstance(old, (int, float)) and lvl > old:
            if now - seen["joined"].get(name, -1e18) >= JOIN_GRACE_S:
                for n in range(int(old) + 1, int(lvl) + 1):
                    add("level_up", f"{name} -> level {n}")
            seen["levels"][name] = lvl
    mp = got.get("map")
    if isinstance(mp, dict):
        for w in mp.get("waypoints") or []:
            n = str(w.get("name") or w.get("id")) if isinstance(w, dict) else str(w)
            if n not in seen["waypoints"]:
                seen["waypoints"].append(n)
                add("waypoint", n)
    if got.get("journal") is not None:
        # 2026-10-10 23:3x the journal started listing open quests the game never "accepted" (Find the Nightsong,
        # the companions' quests): an old playthrough's first poll since takes them in silently (no 30-point burst)
        jq = "quests_unaccepted" in fresh
        for t, q in _quests(got["journal"]).items():
            old = seen["quests"].get(t)
            if old is None:
                add("quest_new", t, jq)
                seen["quests"][t] = {"steps": q["steps"], "done": q["done"], "texts": q["texts"]}
                if q["done"]:
                    add("quest_done", t, jq)
                continue
            if "texts" in old:
                # By text: the journal lists steps in the quest's own order, not the order they unlocked, so
                # the last one is often an old step (S036-S042 named "Soft-Step Trial" for every Shar step).
                new = [k for k in q["texts"] if k not in old["texts"]]
                for k in new:
                    # (the migration poll: a newly listed quest can share its title with a known one, S044 t=0:
                    # 32 old steps of "Daughter of Darkness"/"The Chosen of Shar"/... scored 64 points)
                    add("quest_step", f"{t}: {k}", jq)
                old["texts"] = old["texts"] + new
            elif q["steps"] > old["steps"]:
                for _ in range(q["steps"] - old["steps"]):
                    add("quest_step", f"{t}: {q['last']}")
            if "texts" not in old:
                old["texts"] = q["texts"]  # (a playthrough from before texts were kept: from here on)
            old["steps"] = max(old["steps"], q["steps"])
            if q["done"] and not old["done"]:
                add("quest_done", t)
                old["done"] = True
    ms = _markers(got.get("markers"))
    if ms is not None:
        for k in seen["objectives_active"]:
            if k not in ms and k not in seen["objectives_done"]:
                seen["objectives_done"].append(k)
                add("objective", k.split("|")[0])
        seen["objectives_active"] = ms
    _objective_moves(seen, got.get("markers"), add, "objective_pos" in fresh, now)
    _explored(seen, got.get("explored"), add, "explored" in fresh)
    party.update(seen["levels"])
    _game_events(seen, got.get("events"), add, party)
    seen["baseline"] = True
    return seen, ev


# ---------------------------------------------------------------------------
# the session timeline
# ---------------------------------------------------------------------------

class Timeline:
    """One session's progress file plus the playthrough's persistent seen-state."""

    def __init__(self, path: Path, state_path: Path, started: float | None = None):
        self.path, self.state_path = Path(path), Path(state_path)
        self.started = time.time() if started is None else started
        self.last_progress = self.started
        self.stuck_since: float | None = None
        self.polls = 0
        try:
            self.seen = json.loads(self.state_path.read_text())
        except (OSError, ValueError):
            self.seen = empty_seen()

    def _write(self, rec: dict) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except OSError:
            pass

    def observe(self, got: dict | None, now: float | None = None, actions_since: Callable[[float], int] | None = None) -> list[dict]:
        """Diff one poll (None = no answer), write records, update the stuck state. Returns the new events."""
        now = time.time() if now is None else now
        self.polls += 1
        if got is None:
            self._write({"type": "poll", "t": now, "ok": False})
            return []
        if self.polls % 10 == 1:
            self._write({"type": "poll", "t": now, "ok": True})
        self.seen, evs = diff(self.seen, got, now)
        try:
            self.state_path.write_text(json.dumps(self.seen, ensure_ascii=False))
        except OSError:
            pass
        for e in evs:
            self._write({"type": "progress", "t": now, **e})
        if evs:
            self.last_progress = now
            if self.stuck_since is not None:
                self._write({"type": "unstuck", "t": now, "stuck_min": round((now - self.stuck_since) / 60, 1)})
                self.stuck_since = None
        elif self.stuck_since is None and now - self.last_progress >= STUCK_MIN * 60:
            n = actions_since(self.last_progress) if actions_since else None
            if n is None or n >= STUCK_MIN_ACTIONS:
                self.stuck_since = now
                self._write({"type": "stuck", "t": now, "since": self.last_progress,
                             "idle_min": round((now - self.last_progress) / 60, 1), "actions": n})
        return evs


def actions_in(metrics_path: Path) -> Callable[[float], int | None]:
    """actions_since(t) for Timeline.observe: agent tool calls after t in the bridge metrics file
    (top-level calls only; None if the file isn't there)."""
    def count(t: float) -> int | None:
        try:
            lines = Path(metrics_path).read_text().splitlines()
        except OSError:
            return None
        n = 0
        for ln in lines:
            try:
                r = json.loads(ln)
            except ValueError:
                continue
            if r.get("type") == "tool" and not r.get("nested") and (r.get("t0") or 0) >= t:
                n += 1
        return n
    return count


def load(path: Path) -> list[dict]:
    try:
        lines = Path(path).read_text().splitlines()
    except OSError:
        return []
    out = []
    for ln in lines:
        try:
            r = json.loads(ln)
        except ValueError:
            continue
        if isinstance(r, dict):
            out.append(r)
    return out


def summarize(records: list[dict], start: float, end: float) -> dict:
    """Points, points/hour, counts by kind, longest gap without progress, stuck episodes."""
    prog = [r for r in records if r.get("type") == "progress"]
    pts = sum(r.get("points", 0) for r in prog)
    hours = max((end - start) / 3600, 1e-9)
    by_kind: dict[str, int] = {}
    for r in prog:
        by_kind[r["kind"]] = by_kind.get(r["kind"], 0) + 1
    times = [start] + sorted(r["t"] for r in prog) + [end]
    longest = max((b - a for a, b in zip(times, times[1:])), default=0)
    stuck = [r for r in records if r.get("type") == "stuck"]
    stuck_min = 0.0
    for s in stuck:
        un = next((u for u in records if u.get("type") == "unstuck" and u["t"] >= s["t"]), None)
        stuck_min += ((un["t"] if un else end) - s.get("since", s["t"])) / 60  # from the last progress on
    return {"minutes": round((end - start) / 60, 1), "points": pts,
            "per_hour": round(pts / hours, 1) if end > start else None, "events": len(prog), "by_kind": by_kind,
            "longest_no_progress_min": round(longest / 60, 1), "stuck_episodes": len(stuck),
            "stuck_min": round(stuck_min, 1),
            "last": [{"t": r["t"], "kind": r["kind"], "what": r["what"], "points": r["points"]} for r in prog[-5:]]}


def write_summary(summary: dict, path: Path, history: Path, session: str) -> None:
    Path(path).write_text(json.dumps(summary, indent=1, ensure_ascii=False))
    row = {"session": session, "t": time.time(), **{k: v for k, v in summary.items() if k != "last"}}
    with Path(history).open("a") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def live_view(records: list[dict], started: float, now: float, recent: int = 8) -> dict:
    """The dashboard's live counter."""
    s = summarize(records, started, now)
    prog = [r for r in records if r.get("type") == "progress"]
    last_t = prog[-1]["t"] if prog else None
    open_stuck = None
    for r in records:
        if r.get("type") == "stuck":
            open_stuck = r
        elif r.get("type") == "unstuck":
            open_stuck = None
    since = last_t or started
    return {"points": s["points"], "per_hour": s["per_hour"], "minutes": s["minutes"], "by_kind": s["by_kind"],
            "events": s["events"], "since_progress_min": round((now - since) / 60, 1),
            "stuck": open_stuck is not None, "stuck_min": round((now - open_stuck["since"]) / 60, 1) if open_stuck else None,
            "stuck_after_min": STUCK_MIN,
            "recent": [{"t": r["t"], "kind": r["kind"], "what": r["what"], "points": r["points"]} for r in prog[-recent:]][::-1]}

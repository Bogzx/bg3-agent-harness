"""Story progress per hour, from what the player can see (stdlib only; runner + dashboard use it).

The runner polls the game every POLL_S seconds during a session (one server eval: state.status,
state.journal, state.map, nav.markers; one client eval: ui.state for the minimap subregion). Every op is
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
    quest_step  2  a new journal entry in a known quest
    objective   2  an active quest objective marker disappeared (done or superseded)

points/hour = points / session hours. A rough scale: the Nautiloid (~1 h for a human) is ~60-80 points.

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
import os
import time
from pathlib import Path
from typing import Any, Callable

WEIGHTS = {"region": 15, "quest_done": 10, "level_up": 8, "waypoint": 5, "companion": 5, "quest_new": 3,
           "area": 3, "quest_step": 2, "objective": 2}
POLL_S = float(os.environ.get("BG3AI_PROGRESS_POLL_S", "15") or 15)
STUCK_MIN = float(os.environ.get("BG3AI_STUCK_MINUTES", "15") or 15)
STUCK_MIN_ACTIONS = 10

SERVER_OPS = (("status", "state.status"), ("journal", "state.journal"), ("map", "state.map"),
              ("markers", "nav.markers"))


# ---------------------------------------------------------------------------
# reading the game
# ---------------------------------------------------------------------------

def _bracket(payload: str) -> str:
    eq = "="
    while f"]{eq}]" in payload:
        eq += "="
    return f"[{eq}[{payload}]{eq}]"


def combined_code(ops: list[str]) -> str:
    """One Lua expression running several BG3AI ops: returns a JSON array of their replies."""
    parts = [f"R({_bracket(json.dumps({'op': op, 'args': {}}, separators=(',', ':')))})" for op in ops]
    return "local R=Mods.BG3AI.BG3AI.rpc return '['.." + "..','..".join(parts) + "..']'"


def read_game(lua_eval: Callable[..., str]) -> dict | None:
    """{status, journal, map, markers, ui} from two evals, or None when the game has no session.
    lua_eval(code, context) -> str (the runner's or bridge's LuaDebug eval)."""
    raw = lua_eval(combined_code([op for _, op in SERVER_OPS]), "server")
    replies = json.loads(raw) if raw and raw not in ("nil", "") else []
    got: dict[str, Any] = {}
    for (key, _), r in zip(SERVER_OPS, replies):
        got[key] = r.get("result") if isinstance(r, dict) and r.get("ok") else None
    if got.get("status") is None:
        return None
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
            "objectives_done": [], "levels": {}, "baseline": False}


def _quests(journal: Any) -> dict:
    qs = journal.get("quests", journal) if isinstance(journal, dict) else journal
    out = {}
    for q in qs or []:
        if isinstance(q, dict) and q.get("title"):
            out[str(q["title"])] = {"steps": len(q.get("steps") or []), "done": bool(q.get("done")),
                                    "last": str((q.get("steps") or [""])[-1])[:120]}
    return out


def _markers(m: Any) -> list[str] | None:
    ms = m.get("markers") if isinstance(m, dict) else m
    if not isinstance(ms, list):
        return None
    return sorted({f"{x.get('label') or x.get('objective') or '?'}|{x.get('quest') or ''}" for x in ms if isinstance(x, dict)})


def diff(seen: dict, got: dict) -> tuple[dict, list[dict]]:
    """(new seen, events) for one poll. Events: {kind, what, points}. The first call is a silent baseline."""
    seen = json.loads(json.dumps(seen or empty_seen()))
    for k, v in empty_seen().items():
        seen.setdefault(k, v)
    ev: list[dict] = []
    quiet = not seen["baseline"]

    def add(kind: str, what: str) -> None:
        if not quiet:
            ev.append({"kind": kind, "what": what, "points": WEIGHTS[kind]})

    st = got.get("status") or {}
    region = st.get("region") or st.get("level")
    if region and region not in seen["regions"]:
        seen["regions"].append(region)
        add("region", str(region))
    ui = got.get("ui") or {}
    sub = (ui.get("region") if ui.get("screen") in (None, "game") else None) or st.get("subregion")
    if sub:
        key = f"{region}|{sub}"
        if key not in seen["areas"]:
            seen["areas"].append(key)
            add("area", str(sub))
    for c in st.get("party") or []:
        if not isinstance(c, dict) or not c.get("name"):
            continue
        name, lvl = str(c["name"]), c.get("level")
        old = seen["levels"].get(name)
        if old is None:
            add("companion", name)
            seen["levels"][name] = lvl
        elif isinstance(lvl, (int, float)) and isinstance(old, (int, float)) and lvl > old:
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
        for t, q in _quests(got["journal"]).items():
            old = seen["quests"].get(t)
            if old is None:
                add("quest_new", t)
                seen["quests"][t] = {"steps": q["steps"], "done": q["done"]}
                if q["done"]:
                    add("quest_done", t)
                continue
            if q["steps"] > old["steps"]:
                for _ in range(q["steps"] - old["steps"]):
                    add("quest_step", f"{t}: {q['last']}")
                old["steps"] = q["steps"]
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
        self.seen, evs = diff(self.seen, got)
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

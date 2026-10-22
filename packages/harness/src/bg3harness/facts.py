# pyright: basic
"""facts.jsonl v1 (packages/harness/schemas/facts.v1.schema.json): what the game state was, per run, for the
benchmark's graders; the agent never sees it. Built from the progress poller's reads (bridge progress.read_game)
plus the party inventory, so it costs one extra eval per poll.

- a snapshot (the full state) on the first good read, after a load and every SNAPSHOT_S;
- events from the difference of two reads: combat, died/downed/revived (party members; other characters' deaths
  from the mod's `died` events), level_up, joined, gold (with delta), item_added (gold: true for a gold stack),
  quest_added/quest_done, region, area;
- marks: run_start (time zero), load, run_end;
- the referee ring's events (RingFeed: trade, pickpocket, stole, crime, kill, loot and, while it answers, gold,
  item_added, died and combat exactly per change) and the roll log into rolls.jsonl.

`game_t` is on every record: seconds since run_start. D7 (revised 2026-10-21): BG3 has no pause the harness can
use, so game time is wall time since run_start (null before it).

    python3 -m unittest runner/test_facts.py
"""
from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from bg3ai import referee

VERSION = 1
SNAPSHOT_S = 300.0
INVENTORY_OP = "state.inventory"
# Event kinds the referee ring records exactly per change (bridge/src/bg3ai/referee.py); while it answers, the
# poll diff doesn't write them too (one source per kind, research/ovh/mod.md).
REFEREE_KINDS = frozenset({"gold", "item_added", "died", "combat_started", "combat_ended"})
# The reads before run_end wait this long, one every END_EVERY_S, for a region change's area (end_reads).
END_WAIT_S, END_EVERY_S = 45.0, 5.0


def _member(c: dict) -> dict:
    m = {"name": str(c["name"]), "level": int(c.get("level") or 0), "dead": bool(c.get("dead")),
         "downed": bool(c.get("downed"))}
    for k in ("hp", "max_hp"):
        if isinstance(c.get(k), (int, float)):
            m[k] = c[k]
    if c.get("summon"):
        m["summon"] = True
    return m


def _items(inv: Any) -> list[dict]:
    """state.inventory's items -> [{name, count, owner}], stacks of one name and owner merged."""
    out: dict[tuple, dict] = {}
    for it in (inv or {}).get("items") or [] if isinstance(inv, dict) else []:
        if not isinstance(it, dict) or not it.get("name"):
            continue
        key = (str(it["name"]), str(it.get("owner") or ""))
        row = out.setdefault(key, {"name": key[0], "count": 0, **({"owner": key[1]} if key[1] else {})})
        row["count"] += int(it.get("count") or 1)
        if it.get("gold"):
            row["gold"] = True
    return list(out.values())


def quests(journal: Any) -> dict[str, bool]:
    """journal title -> done (one row per title, as progress._quests merges them)."""
    qs = journal.get("quests", journal) if isinstance(journal, dict) else journal
    out: dict[str, bool] = {}
    for q in qs or []:
        if isinstance(q, dict) and q.get("title"):
            t = str(q["title"])
            out[t] = out.get(t, True) and bool(q.get("done"))
    return out


def snapshot(got: dict, inv: Any, areas: list[str]) -> dict | None:
    """The snapshot fields from one read_game result (+ the inventory read); None without a game session."""
    st = got.get("status") if isinstance(got, dict) else None
    if not isinstance(st, dict):
        return None
    ui = got.get("ui") or {}
    ex = got.get("explored") if isinstance(got.get("explored"), dict) else {}
    area = (ui.get("region") if ui.get("screen") in (None, "game") else None) or st.get("subregion") \
        or ex.get("subregion")
    return {"gold": max(0, int(st.get("gold") or 0)),
            "party": [_member(c) for c in st.get("party") or [] if isinstance(c, dict) and c.get("name")],
            "region": st.get("region") or st.get("level"), "area": area, "areas": list(areas),
            "combat": bool(st.get("in_combat")), "inv": _items(inv),
            "quests": quests(got.get("journal")) if got.get("journal") is not None else None}


def events(old: dict, new: dict, game_events: list | None = None) -> list[dict]:
    """The facts events between two snapshots (old may be None: the first read only baselines), plus the mod's
    deaths of characters outside the party."""
    if old is None:
        return []
    out: list[dict] = []
    if new["region"] and new["region"] != old["region"]:
        out.append({"ev": "region", "name": str(new["region"])})
    if new["area"] and new["area"] != old["area"]:
        out.append({"ev": "area", "name": str(new["area"])})
    if new["combat"] != old["combat"]:
        out.append({"ev": "combat_started" if new["combat"] else "combat_ended"})
    before = {m["name"]: m for m in old["party"]}
    for m in new["party"]:
        b = before.get(m["name"])
        if b is None:
            if not m.get("summon"):
                out.append({"ev": "joined", "name": m["name"]})
            continue
        if m["dead"] and not b["dead"]:
            out.append({"ev": "died", "name": m["name"], "party": True})
        elif m["downed"] and not b["downed"]:
            out.append({"ev": "downed", "name": m["name"], "party": True})
        elif (b["dead"] or b["downed"]) and not (m["dead"] or m["downed"]):
            out.append({"ev": "revived", "name": m["name"], "party": True})
        if m["level"] > b["level"] and not m.get("summon"):
            out += [{"ev": "level_up", "name": m["name"], "level": n} for n in range(b["level"] + 1, m["level"] + 1)]
    if new["gold"] != old["gold"]:
        out.append({"ev": "gold", "gold": new["gold"], "delta": new["gold"] - old["gold"]})
    had = {(i["name"], i.get("owner")): i["count"] for i in old["inv"]}
    if old["inv"] or not new["inv"]:   # an inventory read that failed before isn't a pile of new items
        for i in new["inv"]:
            n = i["count"] - had.get((i["name"], i.get("owner")), 0)
            if n > 0:
                gold = {"gold": True} if i.get("gold") else {}
                out.append({"ev": "item_added", "name": i["name"], "count": n, **gold})
    if old["quests"] is not None and new["quests"] is not None:
        for t, done in new["quests"].items():
            if t not in old["quests"]:
                out.append({"ev": "quest_added", "title": t})
            if done and not old["quests"].get(t):
                out.append({"ev": "quest_done", "title": t})
    party = set(before) | {m["name"] for m in new["party"]}
    for e in game_events or []:
        if not isinstance(e, dict) or e.get("kind") != "died":
            continue
        name = (e.get("data") or {}).get("name") if isinstance(e.get("data"), dict) else e.get("name")
        if name and str(name) not in party:
            out.append({"ev": "died", "name": str(name), "party": False})
    return out


class FactsWriter:
    """Appends facts.jsonl records (thread-safe; a write failure is dropped, facts never break a session)."""

    def __init__(self, path: Path, clock: Callable[[], float] = time.time, snapshot_s: float = SNAPSHOT_S):
        self.path, self.clock, self.snapshot_s = Path(path), clock, snapshot_s
        self.t0: float | None = None
        self.last: dict | None = None        # the last read's snapshot fields
        self.snap_t: float | None = None     # when the last snapshot record was written
        self.areas: list[str] = []
        self.last_t: float | None = None
        self.referee_live = False            # the referee ring answered the last poll: it owns REFEREE_KINDS
        self.area_due = False                # a region event came after the last area event (the minimap lags)
        self.lock = threading.Lock()
        try:   # a later process of the same run: time zero is the file's run_start
            with open(self.path) as f:
                for ln in f:
                    if '"run_start"' in ln:
                        self.t0 = json.loads(ln)["t"]
                        break
        except (OSError, ValueError, KeyError):
            pass

    def _write(self, rec: dict, now: float) -> dict:
        if self.last_t is not None and now < self.last_t:   # records stay in t order: an older ring record
            rec["at"], now = round(now, 2), self.last_t       # keeps its own time in `at`
        self.last_t = now
        game_t = None if self.t0 is None else round(max(0.0, now - self.t0), 1)
        line = {"v": VERSION, "kind": rec.pop("kind"), "t": round(now, 2), "game_t": game_t, **rec}
        try:
            with self.lock, open(self.path, "a") as f:
                f.write(json.dumps(line, ensure_ascii=False, separators=(",", ":")) + "\n")
        except OSError:
            pass
        return line

    def mark(self, mark: str, detail: Any = None, now: float | None = None) -> dict:
        now = self.clock() if now is None else now
        if mark == "run_start" and self.t0 is None:
            self.t0 = now
        if mark == "load":
            self.snap_t = None   # the next read is a full snapshot
        return self._write({"kind": "mark", "mark": mark, **({"detail": detail} if detail is not None else {})}, now)

    def observe(self, got: dict | None, inv: Any = None, now: float | None = None) -> list[dict]:
        """One poll: its events, then a snapshot when one is due. Returns the records written."""
        now = self.clock() if now is None else now
        snap = snapshot(got, inv, self.areas) if got else None
        if snap is None:
            return []
        if snap["area"] and snap["area"] not in self.areas:
            self.areas.append(snap["area"])
            snap["areas"] = list(self.areas)
        if snap["quests"] is None:   # a failed journal read keeps the last one
            snap["quests"] = (self.last or {}).get("quests")
        if not snap["inv"] and self.last and self.last["inv"] and not (isinstance(inv, dict) and "items" in inv):
            snap["inv"] = self.last["inv"]   # likewise a failed inventory read
        evs = events(self.last, snap, got.get("events"))
        for e in evs:
            if e["ev"] in ("region", "area"):
                self.area_due = e["ev"] == "region"
        if self.referee_live:
            evs = [e for e in evs if e["ev"] not in REFEREE_KINDS]
        out = [self._write({"kind": "event", **e}, now) for e in evs]
        self.last = snap
        if self.snap_t is None or now - self.snap_t >= self.snapshot_s:
            self.snap_t = now
            out.append(self._write({"kind": "snapshot", **snap, "quests": snap["quests"] or {}}, now))
        return out


    def timed_events(self, facts: list[dict]) -> list[dict]:
        """Events that carry their own time `t` (referee.to_fact records, choices.choices), in time order; `game_t`
        is filled like any other record's. A record without a time is written now."""
        out = []
        for f in sorted(facts, key=lambda f: f.get("t") or self.clock()):
            rec = {"kind": "event", **{k: v for k, v in f.items() if k not in ("v", "kind", "t", "game_t")}}
            out.append(self._write(rec, f.get("t") or self.clock()))
        return out


def end_reads(read: Callable[[], dict | None], writer: FactsWriter, sleep: Callable[[float], None],
              clock: Callable[[], float], log: Callable[[str], None],
              wait_s: float = END_WAIT_S, every_s: float = END_EVERY_S) -> int:
    """The reads before run_end (read() reads the game and feeds `writer`): one, or after a region change whose
    area hasn't come yet, one every every_s for up to wait_s until it has. The minimap's area lags a region change
    by 15-30 s, and the Nautiloid -> Wilderness wake-up dialogue reads no status for a while (the naut runs of
    live4 stopped 13-28 s after the change). A read without a status, or a failed one, is logged. Returns the
    number of reads."""
    t0, n = clock(), 0
    while True:
        n += 1
        try:
            got = read()
            if not isinstance((got or {}).get("status"), dict):
                wait = "; waiting for the area" if writer.area_due else ""
                log(f"facts: end read {n} has no status (no snapshot){wait}")
        except Exception as e:  # noqa: BLE001 - a loading screen; the run_end mark is written anyway
            log(f"facts: end read {n} failed: {e}")
        if not writer.area_due or clock() - t0 + every_s > wait_s:
            return n
        sleep(every_s)


class RingFeed:
    """The mod's hidden rings into this run's files: the referee (ref.since) as facts events, the roll log
    (roll.since) as rolls.jsonl rows. A reader starts at the ring's current end (sync): what happened before this
    run, or before a runner restart, isn't this run's. A failed poll marks the referee not live, so the poll diff
    writes REFEREE_KINDS again until it answers."""

    def __init__(self, rpc: referee.Rpc, writer: FactsWriter, rolls_path: Path,
                 wall: Callable[[], float] = time.time):
        self.rpc, self.writer, self.rolls_path, self.wall = rpc, writer, Path(rolls_path), wall
        self.ref: referee.RingReader | None = None
        self.roll: referee.RingReader | None = None

    def _reader(self, make: Callable) -> referee.RingReader:
        r = make(self.rpc, self.wall)
        r.sync()
        return r

    def poll(self) -> tuple[int, int]:
        """(facts written, rolls written) for this poll."""
        n_facts = n_rolls = 0
        try:
            self.ref = self.ref or self._reader(referee.referee)
            n_facts = len(self.writer.timed_events([referee.to_fact(r) for r in self.ref.poll()]))
            self.writer.referee_live = True
        except Exception:  # noqa: BLE001 - loading screens, an old mod: the poll diff covers the kinds
            self.writer.referee_live = False
        try:
            self.roll = self.roll or self._reader(referee.rolls)
            rows = [referee.to_roll(r) for r in self.roll.poll()]
            if rows:
                with open(self.rolls_path, "a") as f:
                    f.writelines(json.dumps(r, ensure_ascii=False, separators=(",", ":")) + "\n" for r in rows)
            n_rolls = len(rows)
        except Exception:  # noqa: BLE001 - rolls never break a session
            pass
        return n_facts, n_rolls


def read_inventory(lua_eval: Callable[..., str], combined_code: Callable[[list], str]) -> dict | None:
    """The party inventory (the mod's cached default read), or None when it fails."""
    try:
        r = json.loads(lua_eval(combined_code([INVENTORY_OP]), "server"))[0]
    except Exception:  # noqa: BLE001 - a failed read is a gap, not an error
        return None
    return r.get("result") if isinstance(r, dict) and r.get("ok") else None

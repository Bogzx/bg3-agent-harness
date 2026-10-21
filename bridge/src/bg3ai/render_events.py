"""The EVENTS section: redundant and flicker filters, the cross-call status mute (StatusFlap) and duplicate
filter (EventEcho), status grouping and render_events(). render.py re-exports these names."""

from __future__ import annotations

import json
import re
import time
from typing import Any

from .render_base import Handles, compact

# Event kinds that only duplicate what an action's own result already says. rest_flow: the End-the-Day diagnostics
# (Events.lua REST_FLOW; long_rest/fast_travel read them themselves): their ready-check ids leaked an engine name
# (S044 23:50:48 look: `rest_flow step=ready_ok:MOO_Assault_ReadyCheck`, fairness audit LEAK template).
_REDUNDANT_EVENTS = {"moved_done", "rest_flow"}
# Consecutive events of these kinds share one line ("turn_ended Claude e4; Wyll e1; ...").
_RUN_EVENTS = {"turn_ended", "turn_started", "item_effect"}


def _ref_handle(x: Any, H: Handles) -> str | None:
    return H.get(x["uuid"], x.get("name")) if isinstance(x, dict) and isinstance(x.get("uuid"), str) else None


def _already_shown(e: dict, H: Handles, body: str, own: str | None, job: str | None) -> bool:
    """True when the result's own text already reports this event (S013: 'think' grows with every repeated
    line): the turn owner's cast/damage/statuses (combat_turn prints them), the job a wait_for returned, and
    the queue report a wait_for('queue') printed."""
    kind, d = e.get("kind"), e.get("data")
    if not isinstance(d, dict):
        return False
    if own and kind in ("cast", "damage", "status_applied"):
        src = _ref_handle(d.get("caster") or d.get("source"), H)
        if src == own or (kind == "status_applied" and src is None and _ref_handle(d.get("target"), H) == own):
            return True
    if job and kind == "job_finished" and d.get("job") == job:
        return True
    if kind in ("queue_done", "queue_interrupted") and body and isinstance(d.get("who"), str):
        return f"queue {H.scrub(d['who'])} " in body
    return False


def _drop_flicker(evs: list[dict]) -> list[dict]:
    """A status gained and lost again (or lost and regained) by the same target within one EVENTS batch is no
    change: S023-S027 printed ~200 Moonshield on/off lines a session as the party walked through lantern light."""
    open_: dict[tuple, int] = {}
    drop: set[int] = set()
    for i, e in enumerate(evs):
        kind, d = e.get("kind"), e.get("data")
        if kind not in ("status_applied", "status_removed") or not isinstance(d, dict):
            continue
        t = d.get("target")
        key = ((t.get("uuid") or t.get("name")) if isinstance(t, dict) else t, d.get("name") or d.get("status"))
        j = open_.pop(key, None)
        if j is not None and evs[j].get("kind") != kind:
            drop.update((i, j))
        else:
            open_[key] = i
    return [e for i, e in enumerate(evs) if i not in drop] if drop else evs


def _status_key(e: dict) -> tuple | None:
    kind, d = e.get("kind"), e.get("data")
    if kind not in ("status_applied", "status_removed") or not isinstance(d, dict):
        return None
    t = d.get("target")
    return ((t.get("uuid") or t.get("name")) if isinstance(t, dict) else t, d.get("name") or d.get("status"))


class StatusFlap:
    """Across calls: a status that goes on/off on one target FLAP_TOGGLES times within FLAP_WINDOW_S is an aura
    or a light (S026-S027: SCL_MOONSHIELD on Scratch every tick, dozens of EVENTS lines per get_status, which
    _drop_flicker misses when the on and the off land in different calls). Its events are replaced by one
    status_flapping line and muted until it has been quiet for FLAP_MUTE_S."""
    FLAP_TOGGLES = 6
    FLAP_WINDOW_S = 60.0
    FLAP_MUTE_S = 120.0
    # Bystanders: one status going on AND off on many non-party targets (S026/S027 Moonrise: Moonshield on/off on
    # 20 pilgrims, each once per call, so no single target flaps: ~25 lines per wait_for). Muted by status name for
    # non-party targets; the party's own statuses keep the per-target rule (Moonshield on Claude matters).
    CROWD_EACH = 3      # >= this many applied and this many removed ...
    CROWD_TARGETS = 3   # ... on >= this many distinct non-party targets within FLAP_WINDOW_S

    def __init__(self, clock=time.monotonic) -> None:
        self.clock = clock
        self.hist: dict[tuple, list[float]] = {}
        self.muted: dict[tuple, float] = {}  # key -> time of its last event while muted
        self.crowd: dict[str, list[tuple[float, str, Any]]] = {}  # status name -> (t, kind, target)
        self.crowd_muted: dict[str, float] = {}

    def _bystander(self, e: dict, key: tuple, now: float, party: set | None) -> str:
        """'' = not a bystander event (or party unknown); 'mute' = drop it; 'flap' = it just tipped the crowd rule."""
        if party is None or key[0] in party:
            return ""
        name = key[1]
        last = self.crowd_muted.get(name)
        if last is not None and now - last < self.FLAP_MUTE_S:
            self.crowd_muted[name] = now
            return "mute"
        self.crowd_muted.pop(name, None)
        h = [x for x in self.crowd.get(name, []) if now - x[0] < self.FLAP_WINDOW_S] + [(now, e.get("kind"), key[0])]
        self.crowd[name] = h
        on = sum(1 for x in h if x[1] == "status_applied")
        if on >= self.CROWD_EACH and len(h) - on >= self.CROWD_EACH and len({x[2] for x in h}) >= self.CROWD_TARGETS:
            self.crowd_muted[name] = now
            del self.crowd[name]
            return "flap"
        return ""

    def filter(self, evs: list[dict], party: set | None = None) -> list[dict]:
        """party: the party's uuids (their statuses are never crowd-muted); None = per-target rule only."""
        now = self.clock()
        out = []
        for e in evs:
            key = _status_key(e)
            if key is None:
                out.append(e)
                continue
            b = self._bystander(e, key, now, party)
            if b == "mute":
                continue
            if b == "flap":
                out = [x for x in out if not ((k := _status_key(x)) and k[1] == key[1] and k[0] not in party)]
                n = len({k[0] for x in evs if (k := _status_key(x)) and k[1] == key[1] and k[0] not in party})
                out.append({"seq": e.get("seq"), "kind": "status_flapping",
                            "data": {"name": key[1], "note": f"on/off on {max(n, self.CROWD_TARGETS)}+ others nearby "
                                                             "(aura/light): muted for non-party"}})
                continue
            last = self.muted.get(key)
            if last is not None and now - last < self.FLAP_MUTE_S:
                self.muted[key] = now
                continue
            self.muted.pop(key, None)
            h = [t for t in self.hist.get(key, []) if now - t < self.FLAP_WINDOW_S] + [now]
            self.hist[key] = h
            if len(h) >= self.FLAP_TOGGLES:
                self.muted[key] = now
                del self.hist[key]
                d = e["data"]
                out = [x for x in out if _status_key(x) != key]
                out.append({"seq": e.get("seq"), "kind": "status_flapping",
                            "data": {"name": key[1], "target": d.get("target"),
                                     "note": f"on/off {len(h)}x in {int(self.FLAP_WINDOW_S)}s (aura/light): muted"}})
                continue
            out.append(e)
        if len(self.hist) > 500:  # bounded: forget keys quiet for a window
            self.hist = {k: v for k, v in self.hist.items() if now - v[-1] < self.FLAP_WINDOW_S}
        if len(self.crowd) > 200:
            self.crowd = {k: v for k, v in self.crowd.items() if v and now - v[-1][0] < self.FLAP_WINDOW_S}
        return out


FLAP = StatusFlap()


class EventEcho:
    """Across calls: the same event (kind + identical data) again within ECHO_WINDOW_S is an echo, not news.
    The engine fires some twice for one act: a door/Sphincter interact gives two UseFinished (`used`), one of
    them after the interact result returned, so the next result (a route, a look) showed it again; a pickup
    gave `item_added ... count=2` twice (naut golden results 21-22, 128-129, 254-261). Real repeats differ in
    their data (a stack count, another item) or come later than the window."""
    ECHO_KINDS = {"used", "item_added", "item_removed", "opened", "closed", "door_opened"}
    ECHO_WINDOW_S = 5.0

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.last: dict[tuple, float] = {}

    def filter(self, evs: list[dict]) -> list[dict]:
        out = []
        for e in evs:
            if e.get("kind") not in self.ECHO_KINDS:
                out.append(e)
                continue
            key = (e.get("kind"), json.dumps(e.get("data"), sort_keys=True, default=str))
            t = e.get("t")
            now = float(t) / 1000.0 if isinstance(t, (int, float)) else self.clock()
            prev = self.last.get(key)
            self.last[key] = now
            if prev is not None and 0 <= now - prev < self.ECHO_WINDOW_S:
                continue
            out.append(e)
        if len(self.last) > 300:
            newest = max(self.last.values())
            self.last = {k: v for k, v in self.last.items() if newest - v < self.ECHO_WINDOW_S}
        return out


ECHO = EventEcho()


def one_line(text: str, cap: int = 240) -> str:
    """A multi-line step result as one line for an event field (queue_interrupted last=): the step's own line
    plus its FAILED/INTERRUPTED/STOPPED lines, up to its CHANGES/EVENTS/RUNNING/NOW block. An event value
    with raw newlines broke the EVENTS block (naut-r2 golden 129: `last=1. travel_to ..` then a stray
    `STOPPED: leg 1 failed` and a fake CHANGES block holding `done=0 who=e2`)."""
    lines = [ln.strip() for ln in str(text).split("\n")]
    head: list[str] = []
    for ln in lines:
        if ln in ("CHANGES", "EVENTS") or ln.startswith(("RUNNING ", "NOW ", "\u23f1")):
            break
        if ln:
            head.append(ln)
    keep = head[:1] + [ln for ln in head[1:] if re.search(r"FAILED|INTERRUPTED|TIMEOUT|STOPPED|REFUSED", ln)]
    out = " | ".join(keep or head[:1])
    return out if len(out) <= cap else out[: cap - 1] + "…"


def _target_text(t: Any, H: Handles) -> str:
    if isinstance(t, dict) and isinstance(t.get("uuid"), str):
        return f"{t.get('name')} {H.get(t['uuid'], t.get('name'))}".strip()
    return compact(t, H)


STATUS_GROUP_MIN = 3  # the same status change on this many targets in one batch shares one line


def _group_statuses(evs: list[dict], H: Handles) -> list[dict]:
    """The same status gained (or lost) by many targets in one batch shares one line, targets listed (S026: 25
    'status_applied status=SCL_MOONSHIELD name=Moonshield target={...}' lines on arriving at Moonrise, ~2 KB):
    'status_applied name=Moonshield source=... on 9: Adept Dorikel e713, Brawler Weneg e714, ...'."""
    groups: dict[tuple, list[int]] = {}
    for i, e in enumerate(evs):
        d = e.get("data")
        if e.get("kind") in ("status_applied", "status_removed") and isinstance(d, dict) and d.get("target"):
            rest = compact({k: v for k, v in d.items() if k != "target"}, H)
            groups.setdefault((e["kind"], rest), []).append(i)
    big = {ix[0]: (k, ix) for k, ix in groups.items() if len(ix) >= STATUS_GROUP_MIN}
    if not big:
        return evs
    skip = {i for _, ix in big.values() for i in ix[1:]}
    out = []
    for i, e in enumerate(evs):
        if i in skip:
            continue
        if i in big:
            (kind, rest), ix = big[i]
            who = ", ".join(_target_text(evs[j]["data"]["target"], H) for j in ix)
            out.append({"seq": e.get("seq"), "kind": kind, "data": f"{rest} on {len(ix)}: {who}"})
        else:
            out.append(e)
    return out


def render_events(evs: list[dict], H: Handles, limit: int = 25, body: str = "", job: str | None = None,
                  flap: StatusFlap | None = None, party: set | None = None, echo: EventEcho | None = None) -> str:
    """EVENTS section. body: the tool result's own text, so events it already reports are left out
    (job: the job id a wait_for just returned). flap: the cross-call status mute (server passes FLAP).
    echo: the cross-call duplicate filter (server passes ECHO)."""
    m = re.match(r"TURN (e\d+)", body or "")
    own = m.group(1) if m else None
    evs = [e for e in evs if e.get("kind") not in _REDUNDANT_EVENTS and not _already_shown(e, H, body, own, job)]
    evs = echo.filter(evs) if echo is not None else evs
    evs = _drop_flicker(flap.filter(evs, party) if flap is not None else evs)
    evs = _group_statuses(evs, H)
    if not evs:
        return ""
    shown = evs[-limit:]
    out = []
    if len(evs) > limit:
        out.append(f"(+{len(evs) - limit} earlier events)")
    prev = None
    for e in shown:
        data = e.get("data")
        if e.get("kind") == "dialogue_started" and isinstance(data, dict):  # where/level: the bridge's (vis V26)
            data = {k: v for k, v in data.items() if k not in ("at", "level")}
        txt = compact(data, H) if isinstance(data, (dict, list)) else (H.scrub(str(data)) if data is not None else "")
        txt = re.sub(r"\s*\n\s*", " | ", txt.strip())  # one event, one line: a raw newline would end the block
        kind = e.get("kind")
        if kind in _RUN_EVENTS and prev == kind:
            out[-1] += f"; {txt}"
        else:
            out.append(f"{kind} {txt}".strip())
        prev = kind
    return "EVENTS\n" + "\n".join("  " + x for x in out)

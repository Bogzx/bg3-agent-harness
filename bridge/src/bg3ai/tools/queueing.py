"""Queue plumbing behind queue_actions: executors, reports, running jobs.
"""

from __future__ import annotations

import re
import sys
import time
from typing import Any

from .. import actq, delta, render, turn
from ..game import Game, GameError
from . import batch, core, overview, plumbing, reporting

# ---- queue plumbing ----

_watch_game: Game | None = None  # own LuaDebug connection for interrupt checks (never blocks the agent's calls)
_queue_mgr: actq.QueueManager | None = None
_queue_ignore: dict[str, set] = {}


def _watch_conn() -> Game:
    global _watch_game
    if _watch_game is None:
        _watch_game = Game(port=core.game.port, pool_max=core.game.pool_cap, job_push=core.game.job_push)
        _watch_game.metrics = core.METRICS.watch_view()  # its evals count in the tools' rt_n / watch_n
    return _watch_game


def _parse_ignore(ignore: str) -> set:
    out = {x.strip().lower() for x in (ignore or "").split(",") if x.strip()}
    bad = out - set(actq.INTERRUPT_KINDS)
    if bad:
        raise ValueError(f"unknown interrupt kind(s) {', '.join(sorted(bad))}; use {', '.join(actq.INTERRUPT_KINDS)}")
    return out


def _accepts(fn, name: str) -> bool:
    import inspect
    try:
        return name in inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False


def _step_tool(name: str) -> Any:
    """The tool a step names, looked up in its tools/ module at call time (a test may have replaced it there)."""
    for m in sys.modules[__package__].MODULES:
        if name in vars(m):
            return vars(m)[name]
    raise KeyError(name)


def _call_step(ex: actq.Executor, step: actq.Step, who: str | None) -> tuple[str, str, Any]:
    """Run one step's tool in this thread, batch-quiet, with ex receiving the job id (for cancelling)."""
    fn = _step_tool(step.tool)
    args = dict(step.args)
    if who and not args.get("who") and _accepts(fn, "who"):
        args["who"] = core.H.get(who)
    saved = dict(reporting._batch.d)
    reporting._batch["active"], reporting._batch["exec"], reporting._batch["last"], reporting._batch["result"] = True, ex, "done", None
    try:
        text = fn(**args)
        return text, reporting._batch["last"], reporting._batch["result"]
    finally:
        reporting._batch.d.update(saved)


def _predicate(name: str, arg: str, who: str | None, _w: dict) -> bool:
    w = _watch_conn().try_rpc("act.watch") or {}
    if name == "in_combat":
        return bool(w.get("combat"))
    if name == "in_dialogue":
        return bool(w.get("dialogue"))
    if name == "hp_below":
        rows = [r for r in w.get("party") or [] if who is None or r.get("uuid") == who] or (w.get("party") or [])
        lim = float(arg)
        return any(actq._pct(r) is not None and actq._pct(r) < lim for r in rows)
    if name == "my_turn":
        cs = _watch_conn().try_rpc("state.combat") or {}
        return bool(cs.get("active")) and (who or plumbing._active()) in turn.acting(cs)
    if name in ("item_present", "target_visible"):
        try:
            u = plumbing._uuid(arg)
        except ValueError:
            return False
        ts = _watch_conn().try_rpc("act.target_state", {"uuid": u}) or {}
        if name == "item_present":
            return bool(ts.get("visible") or ts.get("held"))
        return bool(ts.get("visible")) and not ts.get("held")
    return False


def _make_executor(who: str | None, ignore: str | set = "") -> actq.Executor:
    ign = ignore if isinstance(ignore, set) else _parse_ignore(ignore)
    return _new_executor(actq.Watcher(_watch_conn(), ignore=ign), _watch_conn(), who)


def _new_executor(watcher: actq.Watcher, wg: Game, who: str | None) -> actq.Executor:
    holder: dict = {}
    ex = actq.Executor(lambda step, w: _call_step(holder["ex"], step, w), watcher,
                       lambda ctx, jid: wg.rpc("job.cancel", {"id": jid}, ctx), who=who, predicate=_predicate,
                       actor=lambda: who or plumbing._active())
    holder["ex"] = ex
    return ex


def _label(text: str) -> str:
    """uuids in a reason -> "e14 Goblin" (handles plus the last known name)."""
    def sub(m: re.Match) -> str:
        h = core.H.get(m.group(0))
        n = core.H.names.get(h)
        return f"{h} {n}" if n else h
    return render.UUID_RE.sub(sub, text)


def _report_text(rep: actq.Report, n: int) -> str:
    lines = list(rep.lines)
    if rep.stopped:
        tail = f"STOPPED: {_label(rep.stopped)}"
        if rep.left:
            tail += f"; not run: {', '.join(rep.left)}"
        lines.append(tail)
    after = rep.last_result.get("after") if isinstance(rep.last_result, dict) else None
    if isinstance(after, dict):
        keep = {k: after[k] for k in ("hp", "max_hp", "action", "bonus", "movement", "my_turn") if k in after}
        if keep:
            lines.append("NOW " + render.compact(keep, core.H))
    return "\n".join(lines)


def _on_queue_report(who: str, rep: actq.Report) -> None:
    core.METRICS.bg_end(f"queue:{who}")
    # review L2: the run that reports is the background end (_enqueue holds new steps for it): forget it now, or a later
    # unrelated queue for this character (an out-of-combat travel_to) made every hold wait for that walk
    turn_tool._bg_ends.pop(who, None)
    data: dict[str, Any] = {"who": who, "done": rep.done}
    if rep.skipped:
        data["skipped"] = rep.skipped
    if rep.stopped:
        data["why"] = _label(rep.stopped)
        if rep.left:
            data["left"] = rep.left
        if rep.lines:
            data["last"] = render.one_line(core.H.scrub(rep.lines[-1]))
    kind = "queue_interrupted" if rep.stopped else "queue_done"
    try:
        _watch_conn().rpc("act.note", {"kind": kind, "data": data})
    except GameError:
        pass
    if who not in overview._stay:
        _rechain_after(who)


def _rechain_after(who: str) -> None:
    """A companion's own queue ended (walked, interacted, came back from regroup): chain it back to the
    leader so the next walk takes it along (S005: an unchained Shadowheart stayed 77 m behind)."""
    try:
        st = core.game.try_rpc("state.status") or {}
        lead = st.get("active")
        if not lead or lead == who or st.get("in_combat") or st.get("in_dialogue"):
            return
        row = next((r for r in st.get("party") or [] if r.get("uuid") == who), None)
        if row and row.get("chained") is False and not (row.get("dead") or row.get("downed") or row.get("summon")):
            overview._rechain([who], lead)
    except (GameError, ValueError, TypeError):
        pass


def _queues() -> actq.QueueManager:
    global _queue_mgr
    if _queue_mgr is None:
        _queue_mgr = actq.QueueManager(lambda who: _new_executor(
            actq.Watcher(_watch_conn(), ignore=_queue_ignore.get(who, set()), who=who), _watch_conn(), who),
            _on_queue_report)
    return _queue_mgr


def _last_queue_text(who: str | None) -> str:
    qs = [q for w, q in _queues().queues.items() if (who is None or w == who) and q.last_report is not None]
    if not qs:
        return ""
    q = max(qs, key=lambda x: x.finished_at)
    rep = q.last_report
    head = f"queue {_label(q.who)}: " + (f"STOPPED: {_label(rep.stopped)}" if rep.stopped else f"done ({rep.done} steps)")
    if rep.stopped and rep.left:
        head += f"; not run: {', '.join(rep.left)}"
    return head + ("\n" + "\n".join(rep.lines[-4:]) if rep.lines else "")


# One tool result asks twice (RUNNING line, notes window; S009: two job.poll RPCs per job and two act.watch
# reads per trip on every result). Inside _finish's tail ("on") the first answer is reused.
_running_memo: dict[str, Any] = {"on": False, "keys": None, "out": None}


def _running_jobs() -> list[str]:
    """Async jobs still running (forgets settled ones). All jobs of a context are polled in one eval."""
    keys = tuple(sorted(plumbing._async))
    if _running_memo["on"] and _running_memo["keys"] == keys and _running_memo["out"] is not None:
        return list(_running_memo["out"])
    items = list(plumbing._async.items())
    views: dict[str, Any] = {}
    for ctx in {info["ctx"] for _, info in items}:
        mine = [(h, info) for h, info in items if info["ctx"] == ctx]
        try:
            res = delta.multi_rpc(core.game, [("job.poll", {"id": info["id"]}) for _, info in mine], ctx)
        except GameError:
            res = [None] * len(mine)
        views.update({h: v for (h, _), v in zip(mine, res)})
    out = []
    for h, info in items:
        view = views.get(h)
        if not isinstance(view, dict) or view.get("status") != "running":
            plumbing._async.pop(h, None)
            core.METRICS.bg_end(h)
            continue
        out.append(h)
    if _running_memo["on"]:
        _running_memo.update(keys=tuple(sorted(plumbing._async)), out=list(out))
    return out


def _running_text() -> str:
    parts = []
    for h in _running_jobs():
        info = plumbing._async[h]
        s_ = f"{h} {info['tool']}"
        if info.get("eta") is not None:
            left = float(info["eta"]) - (time.monotonic() - info["t0"])
            s_ += f" ~{render.num(round(max(0.0, left), 1))}s left"
        parts.append(s_)
    qs = _queues().status()
    for q in qs:
        s_ = f"queue {_label(q['who'])}: " + (q["running"] or "starting")
        if (q["running"] or "").startswith("travel_to"):
            prog = batch._trip_progress(q["who"])
            if prog:
                s_ += f" ({prog})"
        if q["waiting"]:
            s_ += f" then {', '.join(q['waiting'][:4])}" + (f" +{len(q['waiting']) - 4}" if len(q["waiting"]) > 4 else "")
        parts.append(s_)
    running = {q["who"] for q in qs}
    for c in [c for c in batch._trips if c not in running]:
        batch._trips.pop(c, None)
    if not parts:
        return ""
    return "RUNNING " + " | ".join(parts) + (" (queue_actions appends after it)" if qs else "")


IDLE_EVENT_WAIT_S = 4.0  # wait_for <event kinds> with no job, queue or prompt running gives up after this


def _peek_events(kinds: set) -> dict | None:
    """First event of one of `kinds` since the agent's last tool call, without consuming it
    (the result's EVENTS section still lists everything)."""
    def since(ctx):
        return core.game.try_rpc("events.since", {"seq": core.game.event_seq[ctx], "limit": 300}, context=ctx)

    getattr(core.game, "sync_events", lambda: None)()   # before the seqs above are read

    got = reporting._par(lambda: since("server"), lambda: since("client"))  # both contexts in one tick
    for ctx, res in zip(("server", "client"), got):
        stashed = core.game.stashed_events(ctx) if hasattr(core.game, "stashed_events") else []  # read() took them
        for ev in stashed + ((res.get("events") or []) if isinstance(res, dict) else []):
            if ev.get("kind") in kinds:
                return ev
    return None


# The next module (and so the rest, in file order) loads after this one; later ones used here.
# isort: off
from . import (  # noqa: E402, F401
    turn_plan, turn_tool,
)

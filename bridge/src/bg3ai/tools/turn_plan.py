"""One step through the executor (the macros' building block) and the party's uuids.
"""

from __future__ import annotations

import re
import time
from typing import Any

from .. import actq
from . import core, queueing


def _run_one(watcher: actq.Watcher, who: str | None, step: actq.Step,
             outer: actq.Executor | None = None) -> tuple[actq.Report, str]:
    """One step through the executor (guarded tool, interrupts watched, job cancelled on an interrupt).
    outer: the queue/batch executor running this macro, if any; it also tracks the step's job, so
    cancel_queue or an interrupt it sees cancels the step. Returns (report, the step's line without
    its number)."""
    ex = queueing._new_executor(watcher, queueing._watch_conn(), who)
    if outer is not None:
        own = ex.track

        def track(ctx: str, jid: int) -> None:
            own(ctx, jid)
            outer.track(ctx, jid)
        ex.track = track
    it = iter([step])
    rep = ex.run(lambda: next(it, None), lambda: None)
    text = re.sub(r"^\d+\. ", "", rep.lines[0]) if rep.lines else step.short()
    return rep, text


def _outcome(step: actq.Step, text: str) -> str:
    """'use_ability: done — ...' -> 'Target_MainHandAttack e12: done — ...' (what was tried, then how it went)."""
    body = text.split(": ", 1)[1] if text.startswith(step.tool + ":") or text.startswith(step.short() + ":") else text
    a = step.args
    what = " ".join(str(x) for x in (a.get("ability"), a.get("target")) if x) if step.tool == "use_ability" else step.short()
    return f"{what}: {body}"


def _after(rep: actq.Report, keep: dict) -> None:
    a = rep.last_result.get("after") if isinstance(rep.last_result, dict) else None
    if isinstance(a, dict):
        keep.update(a)


_party_memo: dict = {"t": -1e9, "uuids": None}


def _party_cached(ttl_s: float = 30.0) -> set | None:
    """The party's uuids, re-read at most every ttl_s (the EVENTS crowd mute needs them; one RPC per 30 s)."""
    if time.monotonic() - _party_memo["t"] > ttl_s:
        _remember_party(core.game.try_rpc("state.status"))
    return _party_memo["uuids"]


def _remember_party(st: Any) -> None:
    """Keep the party of a state.status read for _party_cached (a tool that just read it saves its result a read)."""
    if isinstance(st, dict) and st.get("party"):
        _party_memo.update(t=time.monotonic(), uuids={c.get("uuid") for c in st["party"] if isinstance(c, dict)})


def _party_set() -> set:
    st = core.game.try_rpc("state.status") or {}
    return {c.get("uuid") for c in st.get("party") or [] if isinstance(c, dict)}


def _path_over(text: str) -> float | None:
    """'beyond movement (path 9.7 m, 9.0 m left)' → 0.7 (how much the route needs over the movement left)."""
    m = re.search(r"path ([\d.]+) m, ([\d.]+) m left", text or "")
    return float(m.group(1)) - float(m.group(2)) if m else None


# The next module (and so the rest, in file order) loads after this one; later ones used here.
# isort: off
from . import (  # noqa: E402, F401
    turn_slots, macro_tools,
)

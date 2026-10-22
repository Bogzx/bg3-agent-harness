"""run_session(spec, hooks): one play session's agent processes, from the kickoff to the session's end.

The first process gets the kickoff; then, until the session ends:
- the agent ended its turn on its own (exit 0) but the stop check says it can't end yet (keep playing until wrap-up,
  write the handoff, fix lint): the same conversation is resumed with the reason (at most max_stop_resumes);
- the game crashed or hung: the game is recovered (relaunch, load the latest save) and the same conversation is
  resumed for the time left; the agent went silent (agent_hang) with the game fine: only the agent is resumed;
- with Recovery(max_resumes=0) (a benchmark run: no resume) a fault ends the session at once, invalid, and the game
  is left as it is;
- anything else (hard stop, cap, api limit, abort, ...) ends the session.

Everything outside (the agent process, the game, the stop check, events, the clock) comes in through SessionHooks,
so a fake client and a fake game drive it in tests (packages/harness/tests/test_session.py).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

GAME_FAULTS = ("crash", "hang")
FAULTS = (*GAME_FAULTS, "agent_hang")
# A session never ends mid-fight (S078): the bg3 MCP server holds the clock while a fight that was on at WRAP UP lasts,
# at most FIGHT_HOLD_S (15 min) past the planned WRAP UP, and writes the moved times to $BG3AI_FIGHT_HOLD_PATH
# (fight_hold_path: <session dir>/Sxxx.hold.json). The caller's SessionHooks.session_end moves its deadline with it
# (held_deadline), never more than FIGHT_HOLD_S past its own. The legacy runner and the app share this one copy.
FIGHT_HOLD_S = 15 * 60


def fight_hold_path(state_dir: Path, sid: str) -> Path:
    return state_dir / f"{sid}.hold.json"


def held_deadline(path: Path, deadline: float) -> float:
    """The session deadline with the MCP server's fight hold applied (bounded), or `deadline`."""
    try:
        held = float(json.loads(path.read_text()).get("deadline") or 0)
    except (OSError, ValueError, AttributeError, TypeError):
        return deadline
    return max(deadline, min(held, deadline + FIGHT_HOLD_S))


@dataclass(frozen=True)
class Recovery:
    max_resumes: int = 3  # game crashes / agent hangs per session that resume the same conversation
    resume_min_minutes: float = 8  # less than this left after a fault: end the session, the next one picks up
    max_stop_resumes: int = 6  # the agent ended its turn but the stop check sent it back
    hard_stop_grace_s: float = 600  # the deadline + this is the hard stop (no stop resume within a minute of it)


@dataclass(frozen=True)
class SessionSpec:
    sid: str
    prompt: str  # the kickoff
    minutes: float
    client_session: str | None  # the conversation id (None: the client names it)
    recovery: Recovery = Recovery()


class AgentResult(Protocol):
    reason: str
    exit_code: int | None
    started: float
    ended: float
    fault_at: float | None
    claude_session: str


@dataclass(frozen=True)
class RecoverFailure:
    error: Exception
    fatal: bool  # the launch gave up: the caller re-raises it after its bookkeeping


@dataclass
class SessionOutcome:
    result: AgentResult  # the last process's result
    started: float  # when the first process started (the session's clock)
    resumes: int = 0
    stop_resumes: int = 0
    gave_up: Exception | None = None  # a fatal RecoverFailure's error
    invalid: str | None = None  # max_resumes=0: the fault that ended the session


class SessionHooks(Protocol):
    def run_agent(
        self, prompt: str, minutes: float, client_session: str | None, resume: bool, tag: str
    ) -> AgentResult: ...
    def session_end(self, planned_end: float) -> float: ...  # the deadline with a fight hold applied
    def stop_check(self, session_end: float) -> str | None: ...  # why the session can't end yet, or None
    def game_ok(self) -> bool: ...  # after an agent hang: the game alive and answering
    def recover(self, why: str) -> RecoverFailure | None: ...  # relaunch the game, load the latest save
    def loaded(self) -> str | None: ...  # the save the recovery loaded
    def aborted(self) -> bool: ...  # a signal: no more processes
    def stop_requested(self) -> bool: ...  # an operator stop: no resume after a fault
    def notes(self) -> list[str]: ...  # the pending runner notes (taken)
    def resume_prompt(self, why: str, notes: list[str]) -> str: ...
    def agent_resume_prompt(self, notes: list[str]) -> str: ...
    def stop_resume_prompt(self, why: str) -> str: ...
    def event(self, kind: str, **fields: Any) -> None: ...
    def log(self, msg: str) -> None: ...
    def now(self) -> float: ...


def run_session(spec: SessionSpec, hooks: SessionHooks) -> SessionOutcome:
    """The session's processes (see the module doc); its clock starts with the first process."""
    rec, sid = spec.recovery, spec.sid
    res = hooks.run_agent(spec.prompt, spec.minutes, spec.client_session, False, "")
    out = SessionOutcome(res, res.started)
    if res.reason == "dry_run":
        return out
    planned_end = res.started + spec.minutes * 60
    while True:
        session_end = hooks.session_end(planned_end)
        if res.reason not in FAULTS:
            if (
                res.reason != "exited"
                or res.exit_code != 0
                or out.stop_resumes >= rec.max_stop_resumes
                or hooks.aborted()
                or hooks.now() > session_end + rec.hard_stop_grace_s - 60
            ):
                break
            why = hooks.stop_check(session_end)
            if not why:
                break
            out.stop_resumes += 1
            left = (session_end - hooks.now()) / 60
            hooks.log(
                f"{sid}: claude ended its turn but the session can't end yet ({left:.0f} min left); resuming it "
                f"(-c{out.stop_resumes}): {why[:160]}"
            )
            res = hooks.run_agent(
                hooks.stop_resume_prompt(why), left, res.claude_session, True, f"-c{out.stop_resumes}"
            )
            out.result = res
            continue
        fault, fault_at = res.reason, res.fault_at or res.ended
        if rec.max_resumes <= 0:
            out.invalid = fault
            hooks.log(f"{sid}: {fault} with no resume allowed: the session ends invalid")
            hooks.event("session_invalid", fault=fault, session=sid)
            break
        game_ok = fault == "agent_hang" and hooks.game_ok()
        why = {"crash": "crashed", "hang": "stopped responding"}.get(fault, "was unreachable after the agent hang")
        how = "claude restarted, game untouched"
        if not game_ok:
            failed = hooks.recover(why)
            if failed is not None:
                if not failed.fatal:
                    hooks.log(f"recovery after {sid} failed: {failed.error}; the next session setup retries")
                hooks.event("recovery_failed", fault=fault, session=sid, error=str(failed.error)[:300])
                out.gave_up = failed.error if failed.fatal else None
                break
            how = f"game relaunched, loaded {hooks.loaded() or 'main menu'}"
        left = (session_end - hooks.now()) / 60
        if out.resumes >= rec.max_resumes or left < rec.resume_min_minutes or hooks.aborted() or hooks.stop_requested():
            hooks.log(
                f"{sid}: not resuming after the {res.reason} ({left:.0f} min left, {out.resumes} resume(s) so "
                "far); the next session picks up"
            )
            hooks.event(
                "recovered",
                fault=fault,
                session=sid,
                seconds=round(hooks.now() - fault_at),
                how=how + "; session ended, the next one picks up",
            )
            break
        out.resumes += 1
        notes = hooks.notes()
        hooks.log(
            f"{sid}: resuming claude after the {res.reason} ({left:.0f} min left, resume {out.resumes}; "
            f"{hooks.now() - fault_at:.0f} s after the fault)"
        )
        hooks.event(
            "recovered",
            fault=fault,
            session=sid,
            seconds=round(hooks.now() - fault_at),
            how=how + f"; claude resumed (-r{out.resumes})",
        )
        prompt = hooks.agent_resume_prompt(notes) if game_ok else hooks.resume_prompt(why, notes)
        res = hooks.run_agent(prompt, left, res.claude_session, True, f"-r{out.resumes}")
        out.result = res
    return out

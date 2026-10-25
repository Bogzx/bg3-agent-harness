"""bg3harness.session.run_session end to end with a fake client and a fake game on a fake clock: an exit the stop
check accepts or sends back, a hard stop, an agent hang (game fine), a crash that is recovered and resumed, a crash
with Recovery(max_resumes=0) (invalid, the game untouched), a recovery that gives up. No processes, no sleeps."""

from __future__ import annotations

import unittest
from dataclasses import dataclass
from typing import Any

from bg3harness.session import RecoverFailure, Recovery, SessionSpec, run_session


@dataclass
class Result:
    reason: str
    exit_code: int | None
    started: float
    ended: float
    claude_session: str
    fault_at: float | None = None


class FakeGame:
    """The game process: alive and answering until it crashes; a recovery relaunches it and loads the newest save."""

    def __init__(self):
        self.alive, self.launches, self.save = True, 0, "Tav-1__S001"
        self.fail_recovery: RecoverFailure | None = None

    def crash(self) -> None:
        self.alive = False


class Hooks:
    """A fake client (each process plays `script`'s next step: (reason, minutes it runs, what it does to the game))
    and the runner's side of the session, all on one fake clock."""

    def __init__(self, script: list[tuple[str, float]], game: FakeGame, stop_checks: list[str] | None = None):
        self.t, self.script, self.game = 1000.0, list(script), game
        self.stop_checks: list[str] = list(stop_checks or [])
        self.runs: list[dict[str, Any]] = []
        self.events: list[tuple[str, dict[str, Any]]] = []
        self.logs: list[str] = []
        self.pending = ["RUNNER NOTE: x"]
        self.abort = self.stop = False

    def run_agent(self, prompt: str, minutes: float, client_session: str | None, resume: bool, tag: str) -> Result:
        reason, runs_min = self.script.pop(0)
        start = self.t
        self.t += min(runs_min, minutes + 10) * 60
        if reason in ("crash", "hang"):
            self.game.crash()
        self.runs.append(
            {"prompt": prompt, "minutes": round(minutes, 1), "session": client_session, "resume": resume, "tag": tag}
        )
        return Result(
            reason,
            0 if reason == "exited" else None,
            start,
            self.t,
            client_session or "conv-new",
            fault_at=self.t if reason in ("crash", "hang", "agent_hang") else None,
        )

    def session_end(self, planned_end: float) -> float:
        return planned_end

    def stop_check(self, session_end: float) -> str | None:
        return self.stop_checks.pop(0) if self.stop_checks else None

    def game_ok(self) -> bool:
        return self.game.alive

    def recover(self, why: str) -> RecoverFailure | None:
        if self.game.fail_recovery is not None:
            return self.game.fail_recovery
        self.game.alive, self.game.launches = True, self.game.launches + 1
        self.t += 120
        return None

    def loaded(self) -> str | None:
        return self.game.save

    def aborted(self) -> bool:
        return self.abort

    def stop_requested(self) -> bool:
        return self.stop

    def notes(self) -> list[str]:
        notes, self.pending = self.pending, []
        return notes

    def resume_prompt(self, why: str, notes: list[str]) -> str:
        return "\n".join([f"RESUME game {why}", *notes])

    def agent_resume_prompt(self, notes: list[str]) -> str:
        return "\n".join(["RESUME agent", *notes])

    def stop_resume_prompt(self, why: str) -> str:
        return f"STOP {why}"

    def event(self, kind: str, **fields: Any) -> None:
        self.events.append((kind, fields))

    def log(self, msg: str) -> None:
        self.logs.append(msg)

    def now(self) -> float:
        return self.t


SPEC = SessionSpec("S001", "KICKOFF", 60, "conv-1")


class RunSession(unittest.TestCase):
    def test_exit_ends_the_session_when_the_stop_check_agrees(self):
        h = Hooks([("exited", 55)], FakeGame())
        out = run_session(SPEC, h)
        self.assertEqual((out.result.reason, out.resumes, out.stop_resumes, out.started), ("exited", 0, 0, 1000.0))
        self.assertEqual([r["prompt"] for r in h.runs], ["KICKOFF"])

    def test_an_early_exit_is_sent_back_by_the_stop_check(self):
        h = Hooks([("exited", 20), ("exited", 38)], FakeGame(), ["Keep playing: 40 minutes left."])
        out = run_session(SPEC, h)
        self.assertEqual(out.stop_resumes, 1)
        self.assertEqual(
            h.runs[1],
            {
                "prompt": "STOP Keep playing: 40 minutes left.",
                "minutes": 40.0,
                "session": "conv-1",
                "resume": True,
                "tag": "-c1",
            },
        )

    def test_stop_resumes_are_bounded(self):
        h = Hooks([("exited", 1)] * 3, FakeGame(), ["again"] * 5)
        out = run_session(SessionSpec("S001", "K", 60, "conv-1", Recovery(max_stop_resumes=2)), h)
        self.assertEqual((len(h.runs), out.stop_resumes), (3, 2))

    def test_no_stop_resume_count_leaves_it_to_the_stop_check(self):  # a maximise bench run: resumed while it acts
        h = Hooks([("exited", 1)] * 9, FakeGame(), ["again"] * 8)
        out = run_session(SessionSpec("S001", "K", 60, "conv-1", Recovery(max_stop_resumes=None)), h)
        self.assertEqual((len(h.runs), out.stop_resumes, h.script), (9, 8, []))

    def test_the_first_end_of_turn_is_kept_whether_it_was_resumed_or_ended_the_session(self):
        h = Hooks([("exited", 20), ("exited", 38)], FakeGame(), ["Keep playing."])
        self.assertEqual(run_session(SPEC, h).first_stop, 1000.0 + 20 * 60)
        h = Hooks([("exited", 55)], FakeGame())
        self.assertEqual(run_session(SPEC, h).first_stop, 1000.0 + 55 * 60)
        h = Hooks([("agent_hang", 10), ("exited", 30)], FakeGame())  # a fault isn't an end of turn
        self.assertEqual(run_session(SPEC, h).first_stop, 1000.0 + 40 * 60)
        h = Hooks([("hard_stop", 70)], FakeGame())
        self.assertIsNone(run_session(SPEC, h).first_stop)

    def test_hard_stop_ends_the_session(self):
        h = Hooks([("hard_stop", 70)], FakeGame(), ["never asked"])
        out = run_session(SPEC, h)
        self.assertEqual((out.result.reason, len(h.runs), h.stop_checks), ("hard_stop", 1, ["never asked"]))

    def test_agent_hang_resumes_only_the_agent(self):
        g = FakeGame()
        h = Hooks([("agent_hang", 10), ("exited", 50)], g)
        out = run_session(SPEC, h)
        self.assertEqual((out.resumes, g.launches), (1, 0))
        self.assertEqual(h.runs[1]["prompt"], "RESUME agent\nRUNNER NOTE: x")
        self.assertEqual((h.runs[1]["tag"], h.runs[1]["session"]), ("-r1", "conv-1"))
        self.assertEqual(
            h.events,
            [
                (
                    "recovered",
                    {
                        "fault": "agent_hang",
                        "session": "S001",
                        "seconds": 0,
                        "how": "claude restarted, game untouched; claude resumed (-r1)",
                    },
                )
            ],
        )

    def test_crash_recovers_the_game_and_resumes_for_the_time_left(self):
        g = FakeGame()
        h = Hooks([("crash", 10), ("exited", 48)], g)
        out = run_session(SPEC, h)
        self.assertEqual((out.resumes, g.launches, out.result.reason, out.invalid), (1, 1, "exited", None))
        self.assertEqual(
            h.runs[1],
            {
                "prompt": "RESUME game crashed\nRUNNER NOTE: x",
                "minutes": 48.0,
                "session": "conv-1",
                "resume": True,
                "tag": "-r1",
            },
        )
        self.assertEqual(h.events[0][1]["how"], "game relaunched, loaded Tav-1__S001; claude resumed (-r1)")

    def test_crash_with_no_resume_is_invalid_and_leaves_the_game(self):  # a benchmark run
        g = FakeGame()
        h = Hooks([("crash", 10)], g)
        out = run_session(SessionSpec("S001", "K", 60, "conv-1", Recovery(max_resumes=0)), h)
        self.assertEqual((out.invalid, out.resumes, g.launches, len(h.runs)), ("crash", 0, 0, 1))
        self.assertEqual(h.events, [("session_invalid", {"fault": "crash", "session": "S001"})])

    def test_resumes_stop_at_the_limit_or_too_little_time_left(self):
        h = Hooks([("crash", 5), ("hang", 5)], FakeGame())
        out = run_session(SessionSpec("S001", "K", 60, "conv-1", Recovery(max_resumes=1)), h)
        self.assertEqual((out.resumes, len(h.runs), out.result.reason), (1, 2, "hang"))
        self.assertIn("session ended, the next one picks up", h.events[-1][1]["how"])
        h = Hooks([("crash", 55)], FakeGame())
        self.assertEqual(run_session(SPEC, h).resumes, 0)  # 3 min left < resume_min_minutes

    def test_a_recovery_that_gives_up_is_handed_back(self):
        g = FakeGame()
        g.fail_recovery = RecoverFailure(RuntimeError("launch gave up"), fatal=True)
        h = Hooks([("crash", 5)], g)
        out = run_session(SPEC, h)
        self.assertIs(out.gave_up, g.fail_recovery.error)
        self.assertEqual(h.events[0][0], "recovery_failed")
        g.fail_recovery = RecoverFailure(OSError("port"), fatal=False)
        out = run_session(SPEC, Hooks([("crash", 5)], g))
        self.assertIsNone(out.gave_up)

    def test_an_operator_stop_or_abort_means_no_resume(self):
        h = Hooks([("crash", 5)], FakeGame())
        h.stop = True
        self.assertEqual(run_session(SPEC, h).resumes, 0)
        h = Hooks([("exited", 5)], FakeGame(), ["keep playing"])
        h.abort = True
        self.assertEqual(run_session(SPEC, h).stop_resumes, 0)


if __name__ == "__main__":
    unittest.main()


class FightHold(unittest.TestCase):
    """held_deadline: the MCP server's fight hold moves the deadline, bounded by FIGHT_HOLD_S, never earlier."""

    def test_the_hold_file_moves_the_deadline_within_the_bound(self) -> None:
        import json
        import tempfile
        from pathlib import Path

        from bg3harness.session import FIGHT_HOLD_S, fight_hold_path, held_deadline

        with tempfile.TemporaryDirectory() as d:
            hold = fight_hold_path(Path(d), "S004")
            self.assertEqual(hold, Path(d) / "S004.hold.json")
            self.assertEqual(held_deadline(hold, 1000.0), 1000.0)  # no file
            hold.write_text(json.dumps({"deadline": 1300}))
            self.assertEqual(held_deadline(hold, 1000.0), 1300.0)
            hold.write_text(json.dumps({"deadline": 1000 + FIGHT_HOLD_S + 600}))
            self.assertEqual(held_deadline(hold, 1000.0), 1000.0 + FIGHT_HOLD_S)  # never past the bound
            for bad in ('{"deadline": 900}', "[1]", "not json"):
                hold.write_text(bad)
                self.assertEqual(held_deadline(hold, 1000.0), 1000.0)  # never earlier, never an error

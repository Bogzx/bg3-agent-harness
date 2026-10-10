"""Offline tests: the job-finished push (jobpush.py, Game.run_job / poll_job waiting on it).

Run: cd bridge && uv run python -m unittest tests.test_jobpush
"""

from __future__ import annotations

import queue
import threading
import time
import unittest

from bg3ai import game as gamemod
from bg3ai import jobpush
from bg3ai.luadebug import DebugOutput


class FakeClient:
    def __init__(self):
        self.output: queue.Queue = queue.Queue()
        self.connected = True

    def close(self):
        self.connected = False


class FakeGame(gamemod.Game):
    """A Game whose rpc() is a scripted mod: jobs finish `dur` s after they start and, with print=True,
    'print' the finish mark into the listener like RPC.finish_job does."""

    def __init__(self, push: jobpush.JobPush | None, dur: float = 0.3, prints: bool = True):
        super().__init__(port=1)
        self.pushed, self.dur, self.prints = push, dur, prints
        self.calls: list[str] = []
        self.next_id = 1
        self.jobs: dict[int, float] = {}

    def _push(self):
        return self.pushed if self.pushed is not None and self.pushed.usable else None

    def rpc(self, op, args=None, context="server", timeout=12.0):
        self.calls.append(op)
        if op == "act.cast":
            jid = self.next_id
            self.next_id += 1
            self.jobs[jid] = time.monotonic() + self.dur
            if self.prints and self.pushed is not None:
                threading.Timer(self.dur, self.pushed.feed, (f"BG3AI.job {context} {jid} done",)).start()
            return {"job": jid}
        if op == "job.poll":
            jid = int(args["id"])
            done = time.monotonic() >= self.jobs[jid]
            return {"status": "done" if done else "running", "result": {"ok": done}, "elapsed_ms": 1}
        raise AssertionError(op)


def listener() -> tuple[jobpush.JobPush, FakeClient]:
    c = FakeClient()
    jp = jobpush.JobPush(lambda: c)
    jp.ensure()
    return jp, c


class Listener(unittest.TestCase):
    def test_marks_wake_waiters_and_set_seen(self):
        jp, c = listener()
        self.assertFalse(jp.usable)  # nothing heard yet: callers keep polling
        c.output.put(DebugOutput("[BG3AI] server context ready, v1", "info"))
        c.output.put(DebugOutput("BG3AI.job server 7 done", "info"))
        t0 = time.monotonic()
        self.assertTrue(jp.wait("server", 7, t0 - 1, 2.0))
        self.assertTrue(jp.usable)
        self.assertFalse(jp.wait("client", 7, t0 - 1, 0.05))  # other context: not this job

    def test_stale_mark_does_not_count(self):
        jp, _ = listener()
        jp.feed("BG3AI.job server 3 done")
        self.assertFalse(jp.wait("server", 3, time.monotonic(), 0.05))

    def test_disconnect_falls_back(self):
        jp, c = listener()
        jp.feed("BG3AI.job server 1 done")
        self.assertTrue(jp.usable)
        c.connected = False
        time.sleep(0.7)
        self.assertFalse(jp.usable)


class RunJob(unittest.TestCase):
    def test_push_one_poll_per_job(self):
        jp, _ = listener()
        jp.feed("BG3AI.job server 999 done")  # the listener has heard the mod: push is on
        g = FakeGame(jp, dur=0.3)
        t0 = time.monotonic()
        r = g.run_job("act.cast", {}, wait_s=5)
        took = time.monotonic() - t0
        self.assertEqual(r.status, "done")
        self.assertEqual(g.calls.count("job.poll"), 1)
        self.assertLess(took, 0.45)  # woken by the mark, not a fallback second later

    def test_without_marks_polls_as_before(self):
        g = FakeGame(None, dur=0.3)
        r = g.run_job("act.cast", {}, wait_s=5)
        self.assertEqual(r.status, "done")
        self.assertGreaterEqual(g.calls.count("job.poll"), 3)

    def test_lost_mark_costs_one_fallback(self):
        jp, _ = listener()
        jp.feed("BG3AI.job server 999 done")
        g = FakeGame(jp, dur=0.2, prints=False)
        saved, jobpush.PUSH_FALLBACK_S = jobpush.PUSH_FALLBACK_S, 0.3
        self.addCleanup(setattr, jobpush, "PUSH_FALLBACK_S", saved)
        r = g.run_job("act.cast", {}, wait_s=5)
        self.assertEqual(r.status, "done")
        self.assertLessEqual(g.calls.count("job.poll"), 2)

    def test_deadline_still_running(self):
        jp, _ = listener()
        jp.feed("BG3AI.job server 999 done")
        g = FakeGame(jp, dur=5, prints=False)
        t0 = time.monotonic()
        r = g.run_job("act.cast", {}, wait_s=0.3)
        self.assertEqual(r.status, "still_running")
        self.assertLess(time.monotonic() - t0, 1.0)

    def test_poll_job_finished_job_answers_at_once(self):
        jp, _ = listener()
        jp.feed("BG3AI.job server 999 done")
        g = FakeGame(jp, dur=0.0, prints=False)
        jid = g.rpc("act.cast", {})["job"]
        time.sleep(0.01)
        g.calls.clear()
        t0 = time.monotonic()
        self.assertEqual(g.poll_job(jid, wait_s=5).status, "done")
        self.assertLess(time.monotonic() - t0, 0.1)
        self.assertEqual(g.calls, ["job.poll"])


if __name__ == "__main__":
    unittest.main()

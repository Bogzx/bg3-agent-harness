"""wait_new_session.sh on a scratch tree: wakes on the runner's Sxxx.review.md (written after the memory keeper),
maps a resume's triage to its session, falls back to the keeper event when no review comes, and ignores a
triage rewritten after its session's review.

    python3 tools/logs/test_wait_new_session.py
"""
import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent / "wait_new_session.sh"


class WaitNewSession(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "tools" / "logs").mkdir(parents=True)
        shutil.copy(SCRIPT, self.tmp / "tools" / "logs")
        self.sd = self.tmp / "runtime" / "sessions" / "pt"
        self.sd.mkdir(parents=True)
        self.stamps = self.tmp / "stamps"   # the script's mktemp stamp lands here (TMPDIR)
        self.stamps.mkdir()

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def start(self, keeper_max="20", grace="1"):
        env = dict(os.environ, TMPDIR=str(self.stamps), POLL_S="0.05", REVIEW_GRACE_S=grace)
        p = subprocess.Popen(["bash", str(self.tmp / "tools" / "logs" / "wait_new_session.sh"), "pt", "30",
                              keeper_max], stdout=subprocess.PIPE, text=True, env=env)
        deadline = time.time() + 10
        while not any(self.stamps.iterdir()):   # files made after this are -newer than the stamp
            self.assertLess(time.time(), deadline, "no stamp")
            time.sleep(0.01)
        time.sleep(0.02)
        return p

    def run_to_end(self, keeper_max):
        """The script on files all written before it starts, dated after its stamp: nothing to race; max_wait 2 s
        ends it when nothing wakes it."""
        env = dict(os.environ, TMPDIR=str(self.stamps), POLL_S="0.05", REVIEW_GRACE_S="1")
        return subprocess.run(["bash", str(self.tmp / "tools" / "logs" / "wait_new_session.sh"), "pt", "2",
                               keeper_max], capture_output=True, text=True, env=env, timeout=20).stdout

    def dated(self, path, t, text):
        path.write_text(text)
        os.utime(path, (t, t))

    def runner_event(self, t, kind, sid, **fields):
        with open(self.sd / "runner_events.jsonl", "a") as f:
            f.write(json.dumps({"t": t, "kind": kind, "fault": fields.pop("fault"), "session": sid, **fields}) + "\n")

    def keeper_event(self, sid):
        with open(self.sd / "runner_events.jsonl", "a") as f:
            f.write(json.dumps({"t": 1.0, "kind": "memory_keeper", "session": sid, "status": "ok"}) + "\n")

    def test_wakes_on_the_review(self):
        p = self.start()
        (self.sd / "S004.triage.md").write_text("# Triage S004\n")
        self.keeper_event("S004")
        time.sleep(0.02)
        (self.sd / "S004.review.md").write_text("# Review pt/S004\n")
        out, _ = p.communicate(timeout=20)
        self.assertEqual(out, "review: runtime/sessions/pt/S004.review.md\nNEW SESSION: S004\n")

    def test_a_resume_triage_is_its_session(self):
        p = self.start()
        (self.sd / "S005-r1.triage.md").write_text("# Triage S005-r1\n")
        time.sleep(0.02)
        (self.sd / "S005.review.md").write_text("# Review pt/S005\n")
        out, _ = p.communicate(timeout=20)
        self.assertEqual(out.splitlines()[-1], "NEW SESSION: S005")

    def test_a_triage_appended_after_its_review_is_not_new(self):
        # S079: check_all appended to S079.triage.md after S079.review.md; the next wait woke on it again
        (self.sd / "S079.triage.md").write_text("# Triage S079\n")
        time.sleep(0.02)
        (self.sd / "S079.review.md").write_text("# Review pt/S079\n")
        p = self.start()
        with open(self.sd / "S079.triage.md", "a") as f:
            f.write("## check_all after the session\n")
        time.sleep(0.5)   # ~10 polls
        self.assertIsNone(p.poll(), "woke on the rewritten S079 triage")
        (self.sd / "S080.triage.md").write_text("# Triage S080\n")
        time.sleep(0.02)
        (self.sd / "S080.review.md").write_text("# Review pt/S080\n")
        out, _ = p.communicate(timeout=20)
        self.assertEqual(out, "review: runtime/sessions/pt/S080.review.md\nNEW SESSION: S080\n")

    def test_a_resume_after_the_sessions_review_is_new(self):
        (self.sd / "S007.triage.md").write_text("# Triage S007\n")
        (self.sd / "S007.review.md").write_text("# Review pt/S007\n")   # of the crashed first stream
        p = self.start()
        (self.sd / "S007-r1.triage.md").write_text("# Triage S007-r1\n")
        time.sleep(0.02)
        (self.sd / "S007.review.md").write_text("# Review pt/S007 (both streams)\n")
        out, _ = p.communicate(timeout=20)
        self.assertEqual(out, "review: runtime/sessions/pt/S007.review.md\nNEW SESSION: S007\n")

    def test_a_triage_at_a_fault_is_not_the_end_while_the_runner_recovers(self):
        # S086: S086.triage.md at the 05:19 hang, `recovered` at 05:21 (136 s); the wait gave the recovery keeper_max
        # after the triage and woke at ~05:26 with S086-r1 playing
        t = time.time() + 100
        self.runner_event(t - 2, "fault", "S086", fault="hang")
        self.dated(self.sd / "S086.triage.md", t, "# Triage S086\n")
        self.assertEqual(self.run_to_end(keeper_max="0"), "no new session in 2s\n")

    def test_a_triage_of_a_fault_the_session_resumed_after_is_not_its_end(self):
        t = time.time() + 100
        self.runner_event(t - 2, "fault", "S086", fault="hang")
        self.dated(self.sd / "S086.triage.md", t, "# Triage S086\n")
        self.runner_event(t + 136, "recovered", "S086", fault="hang", how="game relaunched; claude resumed (-r1)")
        self.assertEqual(self.run_to_end(keeper_max="0"), "no new session in 2s\n")

    def test_a_recovery_that_ends_the_session_is_its_end(self):
        # S072: crash, `recovered` "session ended, the next one picks up", then the review; the S086 rule skipped it
        t = time.time() + 100
        self.runner_event(t - 2, "fault", "S072", fault="crash")
        self.dated(self.sd / "S072.triage.md", t, "# Triage S072\n")
        self.runner_event(t + 124, "recovered", "S072", fault="crash",
                          how="game relaunched; session ended, the next one picks up")
        self.dated(self.sd / "S072.review.md", t + 190, "# Review pt/S072\n")
        self.assertEqual(self.run_to_end(keeper_max="20"),
                         "review: runtime/sessions/pt/S072.review.md\nNEW SESSION: S072\n")

    def test_no_review_falls_back_to_the_keeper_event(self):
        p = self.start(keeper_max="20", grace="1")
        (self.sd / "S006.triage.md").write_text("# Triage S006\n")
        self.keeper_event("S006")
        t0 = time.time()
        out, _ = p.communicate(timeout=20)
        self.assertLess(time.time() - t0, 10)   # the grace after the keeper, not keeper_max
        self.assertEqual(out, "review: none (not written in time; run tools/logs/review_session.py pt S006)\n"
                              "NEW SESSION: S006\n")


if __name__ == "__main__":
    unittest.main()

"""wait_new_session.sh on a scratch tree: wakes on the runner's Sxxx.review.md (written after the memory keeper),
maps a resume's triage to its session, and falls back to the keeper event when no review comes.

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

"""load_game right after a launch, with the loading screen still up (live window #1, 2026-10-16 18:34: "could not
open the load page: no command 'OpenLoadGameDialog' on ls.LoadingScreen"). The mod's load job now waits for the
loading screen without pressing anything and fails with loading=true when it stays up; the bridge runs the job once
more then (nothing was pressed or loaded)."""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import test_se_tools as T
from bg3ai.game import JobResult
from bg3ai.tools import core, saving

STUCK = JobResult(status="failed", result={"stage": "open", "loading": True,
                                           "error": "a loading screen stayed up 25 s: the load page can't open"})


class LoadDuringLoadingScreen(unittest.TestCase):
    run_load = T.LoadVerified.run_load   # its fake game; run_job is swapped for the results below

    def _run(self, jobs, states):
        jobs = list(jobs)
        real_init = T.Fake.__init__

        def init(g, *a, **kw):
            real_init(g, *a, **kw)

            def run_job(op, args, context="server", wait_s=25.0, **kw2):
                g.presses = getattr(g, "presses", 0) + 1
                return jobs.pop(0) if jobs else JobResult(status="done", result={"pressed": "Load"})
            g.run_job = run_job
        with mock.patch.object(T.Fake, "__init__", init):
            return self.run_load(["a", "b"], ["game"], states)

    def test_loading_screen_then_loaded(self):
        # 1st job: the loading screen stayed up 25 s (no press); 2nd job: pressed, the load happens
        out = self._run([STUCK, JobResult(status="done", result={"pressed": "Load"})], states=["Running", "LoadSession"])
        self.assertIn("load_game: done", out)
        self.assertEqual(self.g.presses, 2)
        self.assertEqual(self.g.keys, [])        # nothing to close: no page was opened

    def test_loading_screen_never_goes(self):
        out = self._run([STUCK, STUCK], states=["Running"])
        self.assertIn("loading screen stayed up", out)
        self.assertEqual(self.g.presses, 1 + saving.LOAD_RETRIES)

    def test_other_failures_are_final(self):
        bad = JobResult(status="failed", result={"stage": "select"}, error="no playthrough named 'Claude' on the load page")
        out = self._run([bad], states=["Running"])
        self.assertIn("no playthrough", out)
        self.assertEqual(self.g.presses, 1)


class LoadOfASaveNotOnThePage(unittest.TestCase):
    def test_a_save_gone_from_the_save_dir_is_refused_without_a_press(self):
        # load_args gives no row and no date for a folder that isn't there (deleted since list_saves: the autosave
        # rotation); the mod would then load the playthrough's row 1 without its date check, maybe another save
        class Game:
            def __init__(self):
                self.jobs = []

            def run_job(self, op, args, **kw):
                self.jobs.append(args)
                return JobResult(status="done", result={"loading": True})

        g = Game()
        with tempfile.TemporaryDirectory() as td, mock.patch.object(core, "SAVE_DIR", Path(td)), \
                mock.patch.object(core, "game", g):
            r, retry = saving._load_once("Claude-1__AutoSave_3", None, None)
        self.assertEqual((r.status, r.error, retry), ("failed", "Claude-1__AutoSave_3: not on the Load page (gone or unknown)", False))
        self.assertEqual(g.jobs, [])


if __name__ == "__main__":
    unittest.main()

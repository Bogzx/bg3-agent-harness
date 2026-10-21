"""The acceptance package's runner, offline: a section process binds S and writes its results; the full run starts
each section through tools/dev/acceptance.py.

    uv run --project bridge python -m unittest tools/dev/test_acceptance_runner.py
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import acceptance  # noqa: E402
from acceptance import core, runner  # noqa: E402


class ServerRefTest(unittest.TestCase):
    def test_reads_and_writes_reach_the_bound_server(self):
        ref, srv = core.ServerRef(), types.SimpleNamespace(SAVE_PREFIX="Claude-", game="g")
        ref.bind(srv)
        self.assertEqual(ref.game, "g")
        ref.SAVE_PREFIX = "Marek Dunhallow-"
        self.assertEqual(srv.SAVE_PREFIX, "Marek Dunhallow-")
        self.assertFalse(hasattr(ref, "nope"))

    def test_every_section_module_holds_the_one_ref(self):
        for name in acceptance.SECTIONS:
            mod = sys.modules[acceptance.FUNCS[name].__module__]
            if "S" in vars(mod):
                self.assertIs(mod.S, core.S, name)


class RunSectionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="acc-runner-"))
        self.results: list[dict] = []
        srv = types.SimpleNamespace(game=types.SimpleNamespace(name="fake game"))
        patches = [
            mock.patch.object(runner, "OUT", self.tmp),
            mock.patch.object(core, "RESULTS", self.results),
            mock.patch.object(runner, "RESULTS", self.results),
            mock.patch.object(core, "LOGF", None),
            mock.patch.object(runner, "alive", lambda: True),
            mock.patch.dict(sys.modules, {"server_view": types.SimpleNamespace(S=srv)}),
            mock.patch.dict(os.environ),
            mock.patch.object(sys, "path", list(sys.path)),
            mock.patch.object(core, "S", core.ServerRef()),  # a fresh, unbound S; run_section binds it
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        if core.LOGF:
            core.LOGF.close()

    def section(self):
        core.item("saw the server", "PASS" if core.S.game.name == "fake game" else "FAIL", core.S.game.name)
        core.item("a failure", "FAIL", "counted")

    def test_binds_s_and_writes_the_results(self):
        with mock.patch.dict(runner.FUNCS, {"x": self.section}), redirect_stdout(io.StringIO()):
            fails = runner.run_section("x")
        self.assertEqual(fails, 1)
        rows = json.loads((self.tmp / "x.json").read_text())
        self.assertEqual([(r["item"], r["verdict"]) for r in rows], [("saw the server", "PASS"), ("a failure", "FAIL")])
        self.assertIn("section x start", (self.tmp / "x.log").read_text())
        self.assertEqual(os.environ["BG3AI_RUNTIME"], str(self.tmp / "rt"))

    def test_a_script_error_is_a_fail(self):
        def boom():
            raise RuntimeError("bad")

        with mock.patch.dict(runner.FUNCS, {"x": boom}), redirect_stdout(io.StringIO()):
            self.assertEqual(runner.run_section("x"), 1)
        self.assertEqual(self.results[0]["evidence"], "script error RuntimeError: bad")


class MainTest(unittest.TestCase):
    def main(self, *argv: str) -> tuple[int, str]:
        out = io.StringIO()
        with mock.patch.object(sys, "argv", ["acceptance.py", *argv]), redirect_stdout(out):
            rc = runner.main()
        return rc, out.getvalue()

    def test_list_and_unknown(self):
        rc, out = self.main("--list")
        self.assertEqual(rc, 0)
        self.assertTrue(out.startswith(" ".join(acceptance.SECTIONS)))
        rc, out = self.main("--only", "rest,nope")
        self.assertEqual(rc, 2)
        self.assertIn("unknown section(s) ['nope']", out)

    def test_each_section_runs_through_the_entry_point(self):
        tmp = Path(tempfile.mkdtemp(prefix="acc-main-"))
        argvs = []

        def run(argv, **kw):
            argvs.append(argv)
            name = argv[-1]
            (tmp / f"{name}.json").write_text(json.dumps([{"item": f"{name} item", "verdict": "PASS", "evidence": ""}]))
            return subprocess.CompletedProcess(argv, 0)

        with (
            mock.patch.object(runner, "OUT", tmp),
            mock.patch.object(runner.subprocess, "run", run),
            mock.patch.object(runner, "alive", lambda: True),
            mock.patch.object(runner, "game_memory", dict),
            mock.patch.dict(os.environ, {acceptance.LOCK_HELD_ENV: "1"}),
        ):
            rc, _ = self.main("--only", "rest,trade")
        self.assertEqual(rc, 0)
        script = str(HERE / "acceptance.py")
        self.assertEqual(
            argvs,
            [
                ["timeout", "900", sys.executable, script, "--section", "rest"],
                ["timeout", "900", sys.executable, script, "--section", "trade"],
            ],
        )
        rows = json.loads((tmp / "results.json").read_text())
        self.assertEqual([(r["section"], r["item"]) for r in rows], [("rest", "rest item"), ("trade", "trade item")])

    def test_the_entry_point_runs(self):
        p = subprocess.run(
            [sys.executable, str(HERE / "acceptance.py"), "--list"],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertTrue(p.stdout.startswith(" ".join(acceptance.SECTIONS)))


if __name__ == "__main__":
    unittest.main()

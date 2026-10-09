"""Offline tests for tools/dev/check_all.py with a fake game (no BG3 needed).

Run: uv run --project bridge python -m unittest tools/dev/test_check_all.py
"""

from __future__ import annotations

import fcntl
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import check_all as C  # noqa: E402
from bg3ai.game import GameError, JobResult  # noqa: E402

GOOD_KEYS = [{"key": k, "label": label, "mods": mods or False, "exists": True, "mods_ok": True}
             for k, label, mods in C.KEY_CASES]


class FakeGame:
    """Answers the evals/RPCs check_all makes; every field is a knob for a test."""

    def __init__(self, **kw):
        self.keys = {"enums": True, "keyspec": True, "cases": [dict(c) for c in GOOD_KEYS]}
        self.confirm = {"api": True}
        self.screen = "game"
        self.in_combat = False
        self.unlocked = [{"id": "WAYP_CHA_Top", "name": "Roadside Cliffs"}, {"id": "WAYP_Grove", "name": "Emerald Grove"}]
        self.ui_list = [{"region": "Wilderness", "name": "Roadside Cliffs"}, {"region": "Wilderness", "name": "Emerald Grove"}]
        self.picks = None  # None: every wanted name/id resolves
        self.job = None
        self.calls: list[tuple] = []
        self.__dict__.update(kw)

    def server_ready(self):
        return True

    def eval(self, code, context="client", timeout=12.0):
        import json
        self.calls.append(("eval", context))
        if "A.keySpec(c[1])" in code:
            return json.dumps(self.keys)
        if "A.confirmBox()" in code:
            return json.dumps(self.confirm)
        if "SV.pickWaypoint(list, w)" in code:
            import re
            lists = [m[1] for m in re.findall(r"Ext\.Json\.Parse\(\[(=*)\[(.*?)\]\1\]\)", code)]
            wants = json.loads(lists[1])
            picks = self.picks if self.picks is not None else [[w, "x", 1] for w in wants]
            picks = [{"want": w, "got": got or False, "n": n} for w, got, n in picks]
            return json.dumps({"api": True, "picks": picks})
        raise AssertionError("unexpected eval: " + code[:80])

    def rpc(self, op, args=None, context="server", timeout=12.0):
        self.calls.append((op, args))
        if op == "state.status":
            return {"active": "u1", "party": [{"uuid": "u1", "in_combat": self.in_combat or None}]}
        if op == "state.map":
            return {"region": "Wilderness", "waypoints": self.unlocked}
        if op == "ui.confirm":
            return {"pressed": "Yes", "via": "command", "buttons": ["Yes", "No"]}
        raise GameError("unknown op " + op)

    def try_rpc(self, op, args=None, context="server", default=None):
        if op == "ui.state":
            return {"screen": self.screen}
        return default

    def run_job(self, op, args, context="server", wait_s=25.0, poll_s=0.1, on_start=None):
        self.calls.append((op, args))
        return self.job or JobResult("done", {"waypoints": self.ui_list})


class SessionGuardTest(unittest.TestCase):
    def test_lock_free_or_missing(self):
        with tempfile.TemporaryDirectory() as d:
            lock = Path(d) / "runner.lock"
            self.assertFalse(C.runner_lock_held(lock))
            lock.write_text("123")
            self.assertFalse(C.runner_lock_held(lock))
            self.assertEqual(lock.read_text(), "123")  # never truncates the runner's pid

    def test_lock_held(self):
        with tempfile.TemporaryDirectory() as d:
            lock = Path(d) / "runner.lock"
            with open(lock, "w") as f:  # same as runner/play.py acquire_lock
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.assertTrue(C.runner_lock_held(lock))
                why = C.session_blockers(Path(d), Path(d) / "play", procs=[])
                self.assertEqual(len(why), 1)
                self.assertIn("runner.lock", why[0])

    def test_runner_pid_waves_its_own_lock_through(self):
        # the runner runs check_all between sessions with BG3AI_CHECK_ALL_RUNNER_PID=<its pid>
        with tempfile.TemporaryDirectory() as d:
            lock, play = Path(d) / "runner.lock", Path(d) / "play"
            with open(lock, "w") as f:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                f.write("4242")
                f.flush()
                self.assertEqual(C.session_blockers(Path(d), play, procs=[], runner_pid="4242"), [])
                self.assertEqual(len(C.session_blockers(Path(d), play, procs=[], runner_pid="999")), 1)
                with mock.patch.dict("os.environ", {"BG3AI_CHECK_ALL_RUNNER_PID": "4242"}):
                    self.assertEqual(C.session_blockers(Path(d), play, procs=[]), [])
                # a claude still playing in play/ blocks even the runner
                why = C.session_blockers(Path(d), play, procs=[(7, ["claude"], play)], runner_pid="4242")
                self.assertEqual(len(why), 1)
                self.assertIn("claude", why[0])

    def test_claude_in_play_dir(self):
        with tempfile.TemporaryDirectory() as d:
            play = Path(d) / "play"
            (play / "memory").mkdir(parents=True)
            procs = [
                (101, ["claude", "-p", "go", "--model", "opus"], play),                  # runner session
                (102, ["node", "/x/@anthropic-ai/claude-code/cli.js"], play / "memory"),  # manual, in a subdir
                (103, ["claude"], Path(d)),                                               # orchestrator at root
                (104, ["python3", "runner/play.py", "run"], Path(d)),                     # runner w/o lock: not claude
                (105, ["vim", "CLAUDE.md"], play),                                        # not claude
            ]
            got = C.playing_sessions(play, procs)
            self.assertEqual([g.split(":")[0] for g in got], ["pid 101", "pid 102"])
            self.assertEqual(C.session_blockers(Path(d), play, procs=procs[2:]), [])

    def test_main_refuses(self):
        out = io.StringIO()
        with mock.patch.object(C, "session_blockers", return_value=["a runner holds runtime/runner.lock"]), \
                mock.patch("sys.stdout", out):
            self.assertEqual(C.main([]), 3)
        self.assertIn("Refusing", out.getvalue())

    def test_proc_table_sees_self(self):
        import os
        pids = {pid for pid, _, _ in C.proc_table()}
        self.assertIn(os.getpid(), pids)


SMOKE = """✓ get_status          41 ms     300 chars  ~75 tok
     Claude ...
✗ get_map             12 ms      40 chars  ~10 tok
     FAILED: boom
total 53 ms, 1 failing
"""
ACT = "== latency\nPASS  latency sys.ping: median 20 ms\nINFO  approach: none\nWARN  x\n0 failure(s)\n"
VIS = "[PASS] markers: 2 objectives\n[WARN] rayapi: slow\n\n2 results: 0 FAIL, 1 WARN\n"
SE = "PASS hook.lib: ok\nFAIL hook.patched: UiSerialize missing\n\n1 PASS, 0 WARN, 1 FAIL\n"


class ParseTest(unittest.TestCase):
    def test_formats(self):
        for text, want, n in ((SMOKE, "FAIL", 2), (ACT, "WARN", 3), (VIS, "WARN", 2), (SE, "FAIL", 2)):
            verdicts, bad = C.parse_output(text)
            self.assertEqual((C.worst(verdicts), len(verdicts)), (want, n), text)
        self.assertEqual(C.parse_output(SE)[1], ["FAIL hook.patched: UiSerialize missing"])
        # summary lines like "0 failure(s)" / "1 PASS, 0 WARN, 1 FAIL" are not verdict lines
        self.assertEqual(len(C.parse_output("1 PASS, 0 WARN, 1 FAIL\n0 failure(s)\n")[0]), 0)

    def test_summarize(self):
        self.assertEqual(C.summarize("vis", 0, VIS, 1).verdict, "WARN")
        r = C.summarize("se", 1, SE, 1)
        self.assertEqual(r.verdict, "FAIL")
        self.assertIn("1 FAIL", r.detail)
        self.assertIn("UiSerialize", r.detail)
        # non-zero exit without a FAIL line (a traceback, "game not ready") still fails
        r = C.summarize("route", 2, "game not ready: connection refused\n", 1)
        self.assertEqual(r.verdict, "FAIL")
        self.assertIn("connection refused", r.detail)
        self.assertEqual(C.summarize("act", None, ACT, 30).verdict, "FAIL")  # timeout
        self.assertEqual(C.summarize("act", 0, "hello\n", 1).verdict, "WARN")  # no verdict lines


class LatencyTest(unittest.TestCase):
    def test_fast_pass_slow_warn_fail(self):
        import time as T
        r = C.check_latency({"look": lambda: "Party ...", "get_status": lambda: "ok"}, 10)
        self.assertEqual(r.verdict, "PASS")
        self.assertIn("look", r.detail)

        def slow():
            T.sleep(0.12)
            return "ok"
        self.assertEqual(C.check_latency({"get_nearby": slow}, 3).verdict, "WARN")

        def failing():
            return "FAILED: no active character"
        r = C.check_latency({"look": failing}, 3)
        self.assertEqual(r.verdict, "FAIL")
        self.assertIn("3/3 failed", r.lines[0])

        def boom():
            raise RuntimeError("x")
        self.assertEqual(C.check_latency({"look": boom}, 2).verdict, "FAIL")

    def test_pct(self):
        self.assertEqual(C.pct(list(range(1, 11)), 0.9), 9)
        self.assertEqual(C.pct([5.0], 0.9), 5.0)


class KeysTest(unittest.TestCase):
    def test_pass(self):
        self.assertEqual(C.check_keys(FakeGame()).verdict, "PASS")

    def test_wrong_mapping(self):
        g = FakeGame()
        g.keys["cases"][0] = {"key": "DELETE", "label": "DELETE", "mods": False, "exists": False, "mods_ok": True}  # the S007 bug
        r = C.check_keys(g)
        self.assertEqual(r.verdict, "FAIL")
        self.assertIn("DELETE", r.detail)

    def test_label_not_in_enum(self):
        g = FakeGame()
        g.keys["cases"][1].update(exists=False)
        self.assertEqual(C.check_keys(g).verdict, "FAIL")

    def test_no_enums_warns(self):
        g = FakeGame()
        g.keys["enums"] = False
        for c in g.keys["cases"]:
            c["exists"] = "?"
        self.assertEqual(C.check_keys(g).verdict, "WARN")

    def test_old_mod(self):
        self.assertEqual(C.check_keys(FakeGame(keys={"keyspec": False})).verdict, "FAIL")


class ConfirmTest(unittest.TestCase):
    def test_no_box(self):
        g = FakeGame()
        self.assertEqual(C.check_confirm(g).verdict, "PASS")
        self.assertNotIn("ui.confirm", [c[0] for c in g.calls])

    def test_box_dry_only(self):
        g = FakeGame(confirm={"api": True, "box": {"name": "Dialog_box", "title": "[ForceUpdate]",
                                                   "buttons": ["Yes", "No"], "yes": "Yes", "no": "No"}})
        r = C.check_confirm(g)
        self.assertEqual(r.verdict, "PASS")
        self.assertIn("yes -> Yes via command", r.detail)
        self.assertIn(("ui.confirm", {"answer": "yes", "dry": True}), g.calls)  # dry, never a press

    def test_box_without_yes(self):
        g = FakeGame(confirm={"api": True, "box": {"name": "Dialog_box", "buttons": ["Close"], "yes": False}})
        self.assertEqual(C.check_confirm(g).verdict, "FAIL")

    def test_old_mod(self):
        self.assertEqual(C.check_confirm(FakeGame(confirm={"api": False})).verdict, "FAIL")


class WaypointsTest(unittest.TestCase):
    def test_pass(self):
        g = FakeGame()
        r = C.check_waypoints(g)
        self.assertEqual(r.verdict, "PASS", r.detail)
        self.assertIn("4/4", r.detail)  # 2 names + 2 ids

    def test_empty_ui_list(self):  # the S007 bug: map open but no list
        self.assertEqual(C.check_waypoints(FakeGame(ui_list=[])).verdict, "FAIL")

    def test_misses(self):
        g = FakeGame(picks=[["Roadside Cliffs", "Roadside Cliffs", 1], ["Emerald Grove", "Emerald Grove", 1],
                            ["WAYP_CHA_Top", None, 0], ["WAYP_Grove", "Emerald Grove", 1]])
        r = C.check_waypoints(g)
        self.assertEqual(r.verdict, "WARN")
        self.assertIn("WAYP_CHA_Top", r.detail)
        g.picks = [[w, None, 2] for w, _, _ in g.picks]
        self.assertEqual(C.check_waypoints(g).verdict, "FAIL")

    def test_never_opens_map_off_game_view(self):
        for g in (FakeGame(screen="dialogue"), FakeGame(in_combat=True)):
            r = C.check_waypoints(g)
            self.assertEqual(r.verdict, "WARN")
            self.assertNotIn("ui.waypoints", [c[0] for c in g.calls])

    def test_nothing_unlocked(self):
        g = FakeGame(unlocked=[])
        self.assertEqual(C.check_waypoints(g).verdict, "PASS")
        self.assertNotIn("ui.waypoints", [c[0] for c in g.calls])

    def test_job_failed(self):
        g = FakeGame(job=JobResult("failed", None, "the map didn't open"))
        r = C.check_waypoints(g)
        self.assertEqual(r.verdict, "FAIL")
        self.assertIn("didn't open", r.detail)


class RunAllTest(unittest.TestCase):
    def fake_runner(self, outputs):
        def run(cmd, cwd=None, env=None, capture_output=True, text=True, timeout=None):
            name = Path(cmd[1]).name
            self.seen.append((name, cmd[2:], env, timeout))
            rc, out = outputs[name]
            if rc is None:
                raise subprocess.TimeoutExpired(cmd, timeout, output=out)
            return subprocess.CompletedProcess(cmd, rc, out, "")
        return run

    def setUp(self):
        self.seen = []
        self.tmp = Path(tempfile.mkdtemp())

    def test_table_and_exit(self):
        outputs = {"smoke.py": (0, SMOKE.replace("✗", "✓")), "check_act.py": (0, ACT), "check_vis.py": (0, VIS),
                   "check_se.py": (1, SE), "check_route.py": (None, "[PASS] route: ok\n")}
        tools = lambda: {"look": lambda: "ok"}  # noqa: E731
        with mock.patch("sys.stdout", io.StringIO()):
            rows = C.run_all(C.ORDER, FakeGame(), tools, 3, 120, False, self.tmp, self.fake_runner(outputs))
        got = {r.name: r.verdict for r in rows}
        self.assertEqual(got, {"latency": "PASS", "waypoints": "PASS", "keys": "PASS", "confirm": "PASS",
                               "smoke": "PASS", "act": "WARN", "vis": "WARN", "se": "FAIL", "route": "FAIL"})
        args = {n: a for n, a, _, _ in self.seen}
        self.assertEqual(args["check_se.py"], ["--only", "hook,children,autoskip"])  # never the stress/leak checks
        self.assertEqual(args["check_act.py"], ["latency", "approach", "forms"])  # read-only checks only
        self.assertEqual(args["check_vis.py"], [])  # no --move / --reload
        envs = {n: e for n, _, e, _ in self.seen}
        self.assertEqual(envs["smoke.py"]["BG3AI_RUNTIME"], str(self.tmp))
        self.assertEqual(envs["check_se.py"].get("BG3AI_RUNTIME"), C.ENV0.get("BG3AI_RUNTIME"))  # real logs
        out = io.StringIO()
        C.print_table(rows, out=out)
        text = out.getvalue()
        self.assertIn("5 PASS, 2 WARN, 2 FAIL", text)
        self.assertRegex(text, r"se\s+FAIL")

    def test_budget_skips(self):
        with mock.patch("sys.stdout", io.StringIO()):
            rows = C.run_all(["keys", "smoke"], FakeGame(), dict, 1, 4, False, self.tmp, self.fake_runner({}))
        self.assertEqual([r.verdict for r in rows], ["SKIP", "SKIP"])
        self.assertEqual(self.seen, [])

    def test_exception_is_a_row(self):
        g = FakeGame()
        g.rpc = mock.Mock(side_effect=GameError("unknown op state.map"))
        with mock.patch("sys.stdout", io.StringIO()):
            rows = C.run_all(["waypoints", "keys"], g, dict, 1, 120, False, self.tmp, self.fake_runner({}))
        self.assertEqual([r.verdict for r in rows], ["FAIL", "PASS"])
        self.assertIn("unknown op", rows[0].detail)

    def test_main_end_to_end(self):
        outputs = {"smoke.py": (0, "✓ get_status 4 ms\n"), "check_act.py": (0, "PASS  a\n"),
                   "check_vis.py": (0, "[PASS] m: x\n"), "check_se.py": (0, "PASS hook: ok\n"),
                   "check_route.py": (0, "[PASS] route: ok\n")}
        out = io.StringIO()
        with mock.patch.object(C, "session_blockers", return_value=[]), \
                mock.patch("bg3ai.game.Game", lambda port=9998: FakeGame()), \
                mock.patch.object(C, "server_tools", lambda: {"look": lambda: "ok"}), \
                mock.patch.object(C.subprocess, "run", self.fake_runner(outputs)), \
                mock.patch.dict("os.environ", {}), mock.patch("sys.stdout", out):
            self.assertEqual(C.main(["--calls", "2"]), 0)
            outputs["check_vis.py"] = (1, "[FAIL] markers: unknown op\n")
            self.assertEqual(C.main(["--calls", "2", "--skip", "latency"]), 1)
            self.assertEqual(C.main(["--only", "keys,confirm"]), 0)
        self.assertIn("9 PASS, 0 WARN, 0 FAIL", out.getvalue())

    def test_json_report(self):
        outputs = {"smoke.py": (0, "✓ get_status 4 ms\n"), "check_act.py": (0, "WARN  slow\n"),
                   "check_vis.py": (1, "[FAIL] markers: unknown op\n"), "check_se.py": (0, "PASS hook: ok\n"),
                   "check_route.py": (0, "[PASS] route: ok\n")}
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(C, "session_blockers", return_value=[]), \
                mock.patch("bg3ai.game.Game", lambda port=9998: FakeGame()), \
                mock.patch.object(C, "server_tools", lambda: {"look": lambda: "ok"}), \
                mock.patch.object(C.subprocess, "run", self.fake_runner(outputs)), \
                mock.patch.dict("os.environ", {}), mock.patch("sys.stdout", io.StringIO()):
            path = Path(d) / "r.json"
            self.assertEqual(C.main(["--calls", "2", "--json", str(path)]), 1)
            rep = json.loads(path.read_text())
        self.assertEqual(rep["exit"], 1)
        self.assertEqual(rep["counts"], {"PASS": 7, "WARN": 1, "FAIL": 1, "SKIP": 0})
        self.assertEqual([r["name"] for r in rep["rows"]], C.ORDER)
        self.assertEqual(next(r for r in rep["rows"] if r["name"] == "vis")["verdict"], "FAIL")

    def test_unreachable(self):
        g = FakeGame()
        g.server_ready = lambda: False
        with mock.patch.object(C, "session_blockers", return_value=[]), \
                mock.patch("bg3ai.game.Game", lambda port=9998: g), \
                mock.patch.dict("os.environ", {}), mock.patch("sys.stdout", io.StringIO()):
            self.assertEqual(C.main([]), 2)


if __name__ == "__main__":
    unittest.main()

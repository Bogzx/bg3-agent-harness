#!/usr/bin/env python3
"""runner/play.py between sessions: the mod's hot reload and snapshot, check_all, the runner's self-restart, the
memory session and keeper, triage, the session review and the plan check (moved from test_play.py). Never launches
claude or the game.

    python3 runner/test_play_between.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
# RUN-9: play.RUNTIME is read at import; a test must never append to the real runtime/runner.log (the phantom
# "0.01 s after SIGTERM" kill bursts the orchestrator chased). Tests that need a runtime set their own.
os.environ.setdefault("BG3AI_RUNTIME", tempfile.mkdtemp(prefix="bg3ai-test-runtime-"))
os.environ.setdefault("BG3AI_KNOWLEDGE_LOCK", "0")   # never chmod the real play/knowledge (Knowledge tests patch it)
import play as P  # noqa: I001  (play puts packages/harness/src on sys.path)
from bg3harness.game import io as G

G_FUNCS = {n for n, v in vars(G).items() if callable(v) and getattr(v, "__module__", None) == G.__name__
           and not isinstance(v, type)}


def home(name: str):
    """The module a patched name lives in: the game's I/O functions moved to bg3harness.game.io."""
    return G if name in G_FUNCS else P



class HotReload(unittest.TestCase):
    def setUp(self):
        self.saved = (G.port_open, G.game_state, G.lua_eval, P.log, P.time.sleep, P.mod_tree_hashes)
        self.logs, self.evals = [], []
        self.back_after = 2
        G.port_open = lambda *a, **k: True
        G.game_state = lambda: "Running"
        P.log = self.logs.append
        P.time.sleep = lambda s: None

        def ev(code, context="client", timeout=0):
            self.evals.append((code, context))
            if "Reset" in code:
                raise ConnectionError("dropped")
            self.back_after -= 1
            return "true" if self.back_after <= 0 else "false"
        G.lua_eval = ev
        self.r = P.Runner.__new__(P.Runner)
        self.r.game_proc, self.r.abort = None, False
        self.r.state = type("S", (), {"data": {}, "__getitem__": lambda s, k: s.data.get(k),
                                      "__setitem__": lambda s, k, v: s.data.__setitem__(k, v)})()

    def tearDown(self):
        (G.port_open, G.game_state, G.lua_eval, P.log, P.time.sleep, P.mod_tree_hashes) = self.saved

    def test_hashes_and_diff(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "a").mkdir()
            (root / "a" / "x.lua").write_text("one")
            (root / "y.lua").write_text("two")
            h1 = P.mod_tree_hashes(root)
            self.assertEqual(set(h1), {"a/x.lua", "y.lua"})
            (root / "y.lua").write_text("2")
            (root / "z.lua").write_text("new")
            h2 = P.mod_tree_hashes(root)
            self.assertEqual(P.changed_files(h1, h2), ["y.lua", "z.lua"])
            self.assertEqual(P.changed_files(h2, h2), [])
            self.assertEqual(P.changed_files(None, h2), sorted(h2))
            (root / "a" / "x.lua").unlink()
            self.assertEqual(P.changed_files(h2, P.mod_tree_hashes(root)), ["a/x.lua"])

    def test_unchanged_does_nothing(self):
        P.mod_tree_hashes = lambda: {"a.lua": "1"}
        self.r.state["mod_hashes"] = {"a.lua": "1"}
        self.assertEqual(self.r.hot_reload_mod(), 0)
        self.assertEqual(self.evals, [])

    def test_changed_resets_and_waits_for_rpc(self):
        P.mod_tree_hashes = lambda: {"a.lua": "2", "b.lua": "1"}
        self.r.state["mod_hashes"] = {"a.lua": "1", "b.lua": "1"}
        self.assertEqual(self.r.hot_reload_mod(), 1)
        self.assertEqual(self.evals[0], ("Ext.Debug.Reset() return 'reset'", "server"))
        self.assertTrue(any("Mods.BG3AI.BG3AI.RPC" in c for c, _ in self.evals[1:]))
        self.assertEqual(self.r.state["mod_hashes"], {"a.lua": "2", "b.lua": "1"})
        self.assertTrue(any(m.startswith("mod hot-reloaded (1 files changed") for m in self.logs), self.logs)

    def test_mod_never_back_raises_and_keeps_old_hashes(self):
        P.mod_tree_hashes = lambda: {"a.lua": "2"}
        self.r.state["mod_hashes"] = {"a.lua": "1"}
        self.back_after = 10 ** 9
        saved = P.RELOAD_TIMEOUT
        P.RELOAD_TIMEOUT = 0.05
        try:
            with self.assertRaises(RuntimeError):
                self.r.hot_reload_mod()
        finally:
            P.RELOAD_TIMEOUT = saved
        self.assertEqual(self.r.state["mod_hashes"], {"a.lua": "1"})  # retried next time

    def test_skipped_while_loading(self):
        P.mod_tree_hashes = lambda: {"a.lua": "2"}
        G.game_state = lambda: "LoadLevel"
        self.assertEqual(self.r.hot_reload_mod(), 0)
        self.assertEqual(self.evals, [])

    def test_menu_uses_client_context(self):
        P.mod_tree_hashes = lambda: {"a.lua": "2"}
        G.game_state = lambda: "Menu"
        self.r.hot_reload_mod()
        self.assertEqual({c for _, c in self.evals}, {"client"})


class ModSnapshot(unittest.TestCase):
    """S24: the runner's game loads the mod from MOD_LIVE, a copy of mod/ refreshed only at a session boundary (S090
    loaded a merge's Lua that landed 19 s after its bridge started). Fake trees only."""

    def test_sync_tree_in_place(self):
        with tempfile.TemporaryDirectory() as td:
            src, dst = Path(td) / "mod", Path(td) / "live"
            (src / "Mods" / "A").mkdir(parents=True)
            (src / "Mods" / "A" / "x.lua").write_text("one")
            (src / "Mods" / "A" / "y.lua").write_text("two")
            (src / "__pycache__").mkdir()
            (src / "__pycache__" / "c.pyc").write_text("no")
            self.assertEqual(P.sync_tree(src, dst), ["Mods/A/x.lua", "Mods/A/y.lua"])
            ino = dst.stat().st_ino
            self.assertEqual(P.mod_tree_hashes(dst), P.mod_tree_hashes(src))
            self.assertFalse((dst / "__pycache__").exists())
            self.assertEqual(P.sync_tree(src, dst), [])                       # nothing to do
            # a merge into main: x edited, y removed, a new dir; a stray temp file from an interrupted sync
            (src / "Mods" / "A" / "x.lua").write_text("ONE")
            (src / "Mods" / "A" / "y.lua").unlink()
            (src / "Mods" / "B").mkdir()
            (src / "Mods" / "B" / "z.lua").write_text("new")
            (dst / "Mods" / "A" / ".x.lua.sync").write_text("half")
            (dst / "Mods" / "Old").mkdir()
            (dst / "Mods" / "Old" / "gone.lua").write_text("old")
            self.assertEqual(P.sync_tree(src, dst),
                             ["Mods/A/.x.lua.sync", "Mods/A/x.lua", "Mods/A/y.lua", "Mods/B/z.lua",
                              "Mods/Old/gone.lua"])
            self.assertEqual(P.mod_tree_hashes(dst), P.mod_tree_hashes(src))
            self.assertEqual((dst / "Mods" / "A" / "x.lua").read_text(), "ONE")
            self.assertFalse((dst / "Mods" / "Old").exists())                # emptied dirs go too
            self.assertEqual(dst.stat().st_ino, ino)                          # the same directory (a bind mount's)

    def test_repo_commit(self):
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            self.assertEqual(P.repo_commit(repo), "unknown")                  # not a git tree
            git = ["git", "-C", td, "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false"]
            P.subprocess.run(git + ["init", "-q"], check=True)
            (repo / "mod").mkdir()
            (repo / "mod" / "a.lua").write_text("1")
            (repo / "docs.md").write_text("d")
            P.subprocess.run(git + ["add", "."], check=True)
            P.subprocess.run(git + ["commit", "-qm", "c"], check=True)
            head = P.subprocess.run(git + ["rev-parse", "--short=9", "HEAD"], capture_output=True,
                                    text=True).stdout.strip()
            self.assertEqual(P.repo_commit(repo), head)
            (repo / "docs.md").write_text("edited")                           # outside mod/ and bridge/src
            self.assertEqual(P.repo_commit(repo), head)
            (repo / "mod" / "a.lua").write_text("2")
            self.assertEqual(P.repo_commit(repo), head + "+dirty")

    def _runner(self, td: Path, commits: list[str]):
        self.logs, self.events = [], []
        saved = (P.MOD_DIR, P.MOD_LIVE, P.repo_commit, P.log)
        self.addCleanup(lambda: setattr(P, "MOD_DIR", saved[0]) or setattr(P, "MOD_LIVE", saved[1])
                        or setattr(P, "repo_commit", saved[2]) or setattr(P, "log", saved[3]))
        P.MOD_DIR, P.MOD_LIVE = td / "mod", td / "runtime" / "mod-live"
        (P.MOD_DIR / "Mods").mkdir(parents=True)
        (P.MOD_DIR / "Mods" / "a.lua").write_text("1")
        seq = iter(commits)
        P.repo_commit = lambda *a, **k: next(seq)
        P.log = self.logs.append
        r = P.Runner.__new__(P.Runner)
        r.event = lambda kind, **f: self.events.append((kind, f))
        return r

    def test_refresh_records_the_commit(self):
        with tempfile.TemporaryDirectory() as td:
            r = self._runner(Path(td), ["abc", "abc"])
            self.assertEqual(r.refresh_mod_snapshot(), ["Mods/a.lua"])
            self.assertEqual(r.mod_rev, "abc")
            self.assertEqual((P.MOD_LIVE / "Mods" / "a.lua").read_text(), "1")
            self.assertIn(f"mod snapshot {P.MOD_LIVE} = abc (1 files updated: Mods/a.lua)", self.logs)

    def test_refresh_copies_again_when_a_merge_lands_during_the_copy(self):
        with tempfile.TemporaryDirectory() as td:
            r = self._runner(Path(td), ["abc", "def", "def", "def"])
            r.refresh_mod_snapshot()
            self.assertEqual(r.mod_rev, "def")

    def test_main_moved_logged_once_per_commit(self):
        with tempfile.TemporaryDirectory() as td:
            r = self._runner(Path(td), ["abc", "abc", "abc", "def", "def", "fed"])
            r.code_moved("S089")                      # mod_rev unknown yet: nothing (and no git call)
            r.refresh_mod_snapshot()
            r.code_moved("S090")                      # abc: nothing moved
            r.code_moved("S090")                      # def: logged
            r.code_moved("S090-r1")                   # def again: once
            r.code_moved("S090-r1")                   # fed: logged
            moved = [m for m in self.logs if "main moved" in m]
            self.assertEqual(len(moved), 2)
            self.assertIn("S090: main moved since the session's mod snapshot (abc -> def): the game keeps abc's mod",
                          moved[0])
            self.assertEqual([f["head"] for k, f in self.events if k == "main_moved"], ["def", "fed"])

    def test_game_mod_path_from_the_game_environ(self):
        with tempfile.TemporaryDirectory() as td:
            proc = Path(td)
            for pid, env in ((11, b"HOME=/h\0BG3AI_GAME_STAMP=1\0"),                    # no bg3le: no mod path
                             (12, b"HOME=/h\0BG3LE_MOD_PATH=/rt/mod-live\0BG3LE_DEBUG=1\0")):
                (proc / str(pid)).mkdir()
                (proc / str(pid) / "environ").write_bytes(env)
            with mock.patch.object(G, "pids_of_game", lambda: [10, 11, 12]):   # 10: gone (no /proc entry)
                self.assertEqual(P.game_mod_path(proc), "/rt/mod-live")
            with mock.patch.object(G, "pids_of_game", lambda: [11]):
                self.assertIsNone(P.game_mod_path(proc))

    def test_wired_at_the_boundary(self):
        src = Path(P.__file__).read_text()
        body = src[src.index("    def play_session("):]
        self.assertLess(body.index("self.refresh_mod_snapshot()"), body.index("self.maybe_relaunch("))
        self.assertLess(body.index("self.refresh_mod_snapshot()"), body.index("self.hot_reload_mod()"))
        claude = src[src.index("    def run_claude("):]
        self.assertIn("code {self.mod_rev or 'unknown'}", claude[:claude.index("if self.opt.dry_run")])


class CheckAllBetweenSessions(unittest.TestCase):
    """check_all after the hot reload, before the next kickoff: spacing, verdict, triage append, status."""

    REPORT = {"exit": 1, "counts": {"PASS": 7, "WARN": 1, "FAIL": 1, "SKIP": 0},
              "rows": [{"name": "latency", "verdict": "PASS", "secs": 6.1, "detail": "p50 40 ms", "lines": []},
                       {"name": "vis", "verdict": "FAIL", "secs": 3.0, "detail": "markers | unknown op",
                        "lines": ["[FAIL] markers: unknown op", "[FAIL] second: x"]}]}

    def test_due(self):
        self.assertTrue(P.check_all_due(None, 1000.0))
        self.assertFalse(P.check_all_due(1000.0, 1000.0 + 29 * 60))
        self.assertTrue(P.check_all_due(1000.0, 1000.0 + 30 * 60))

    def test_result(self):
        res = P.check_all_result(1, "", self.REPORT)
        self.assertTrue(res["ok"])
        self.assertEqual(res["line"], "check_all: 7 pass, 1 warn, 1 fail")
        for rc, want in ((2, "not reachable"), (3, "refused"), (None, "timed out")):
            res = P.check_all_result(rc, "", None)
            self.assertFalse(res["ok"])
            self.assertIn(want, res["line"])
        self.assertIn("Traceback boom", P.check_all_result(1, "x\nTraceback boom\n", None)["line"])

    def test_markdown(self):
        md = P.check_all_markdown(P.check_all_result(1, "", self.REPORT), time.time(), 25.4, "after the session ")
        self.assertIn("## check_all after the session (", md)
        self.assertIn("25 s): 7 pass, 1 warn, 1 fail", md)
        self.assertIn("| vis | **FAIL** | 3.0 | markers \\| unknown op |", md)
        self.assertIn("[FAIL] second: x", md)
        self.assertEqual(md.count("\n|"), 5)  # header, rule, 2 rows, 1 extra line

    def test_cmd_uses_bridge_env(self):
        with mock.patch.object(P.shutil, "which", return_value="/u/uv"):
            cmd = P.check_all_cmd(Path("/r/x.json"))
        self.assertEqual(cmd[:2], ["/u/uv", "run"])
        self.assertEqual(cmd[-3:], [str(P.CHECK_ALL), "--json", "/r/x.json"])

    def _runner(self, d: Path, last=None):
        r = P.Runner.__new__(P.Runner)
        r.stop_requested = False
        r.pending_notes = []
        r.state = P.RunnerState.__new__(P.RunnerState)
        r.state.dir, r.state.path, r.state.data = d, d / "runner.json", {"check_all_at": last}
        triage_md = d / "S004.triage.md"
        triage_md.write_text("# S004 triage\n")
        r.last_triage = ("S004", triage_md)
        return r, triage_md

    def _run(self, d: Path, r, report, rc=1, screen="game"):
        logs, calls = [], []

        def fake_run(cmd, **kw):
            calls.append((cmd, kw))
            if report is not None:
                Path(cmd[-1]).write_text(json.dumps(report))
            return mock.Mock(returncode=rc, stdout="", stderr="")

        with mock.patch.object(P, "RUNTIME", d), mock.patch.object(P, "log", logs.append), \
                mock.patch.object(P.subprocess, "run", fake_run), mock.patch.object(G, "ui_screen", return_value=screen):
            res = r.between_sessions_check()
        return res, logs, calls

    def test_runs_appends_and_reports(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            r, triage_md = self._runner(d)
            res, logs, calls = self._run(d, r, self.REPORT)
            self.assertTrue(res["ok"])
            self.assertEqual(calls[0][1]["env"]["BG3AI_CHECK_ALL_RUNNER_PID"], str(P.os.getpid()))
            self.assertTrue(logs[0].startswith("check_all: 7 pass, 1 warn, 1 fail (after S004"))
            self.assertTrue(any(line.startswith("check_all FAIL vis") for line in logs))
            md = triage_md.read_text()
            self.assertTrue(md.startswith("# S004 triage\n"))
            self.assertIn("| vis | **FAIL** |", md)
            st = json.loads((d / "runner.status.json").read_text())["check_all"]
            self.assertEqual((st["session"], st["counts"]["FAIL"], st["ok"]), ("S004", 1, True))
            self.assertIsNotNone(r.state["check_all_at"])
            # within 30 min: skipped, nothing run
            res, logs, calls = self._run(d, r, self.REPORT)
            self.assertIsNone(res)
            self.assertEqual(calls, [])

    def test_no_verdict_retries_next_time(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            r, triage_md = self._runner(d, last=time.time() - 3600)
            res, logs, _ = self._run(d, r, None, rc=2)
            self.assertFalse(res["ok"])
            self.assertIn("not reachable", logs[0])
            self.assertLess(r.state["check_all_at"], time.time() - 3000)  # not recorded: retried next session
            self.assertIn("no verdict", triage_md.read_text())

    def test_screen_fail_is_loud(self):
        # S068 started on screen=map: a map check_all could not close fails the prep loudly and tells the session
        report = dict(self.REPORT, rows=self.REPORT["rows"] + [
            {"name": "screen", "verdict": "FAIL", "secs": 6.0, "lines": [],
             "detail": "screen map after the checks (was game): the next session would start there"}])
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            r, _ = self._runner(d)
            _, logs, _ = self._run(d, r, report)
            self.assertTrue(any(line.startswith("PREP FAIL: the game is not on the game view after check_all: "
                                                "screen map after the checks") for line in logs), logs)
            self.assertEqual(len(r.pending_notes), 1)
            self.assertIn("left the game off the game view (screen map after the checks", r.pending_notes[0])
            self.assertIn("ESCAPE", r.pending_notes[0])

    def test_screen_ok_is_quiet(self):
        report = dict(self.REPORT, rows=self.REPORT["rows"] + [
            {"name": "screen", "verdict": "WARN", "secs": 1.0, "lines": [],
             "detail": "game view; closed a map left open (ESCAPE x1)"}])
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            for rep, screen in ((report, "map"), (self.REPORT, "game")):  # no row: the runner asks ui.state
                r, _ = self._runner(d)
                _, logs, _ = self._run(d, r, rep, screen=screen)
                self.assertEqual(r.pending_notes, [])
                self.assertFalse(any("PREP FAIL" in line for line in logs))

    def test_no_verdict_map_up_is_loud(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            r, _ = self._runner(d)
            _, logs, _ = self._run(d, r, None, rc=None, screen="map")  # timed out mid-waypoints
            self.assertTrue(any(line.startswith("PREP FAIL") and "screen map after check_all (timed out" in line
                                for line in logs), logs)
            self.assertEqual(len(r.pending_notes), 1)

    def test_stop_requested_skips(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            r, _ = self._runner(d)
            r.stop_requested = True
            res, _, calls = self._run(d, r, self.REPORT)
            self.assertIsNone(res)
            self.assertEqual(calls, [])

    def test_wired_after_reload_before_kickoff(self):
        src = Path(P.__file__).read_text()
        body = src[src.index("    def play_session("):]
        self.assertLess(body.index("self.hot_reload_mod()"), body.index("self.between_sessions_check()"))
        self.assertLess(body.index("self.between_sessions_check()"), body.index("build_kickoff("))



class SelfRestart(unittest.TestCase):
    """Between sessions the runner execs itself when its own code changed on disk."""

    def test_code_watch(self):
        with tempfile.TemporaryDirectory() as d:
            a, b = Path(d, "a.py"), Path(d, "b.py")
            a.write_text("x = 1\n")
            b.write_text("y = 1\n")
            w = P.CodeWatch([a])
            self.assertEqual(w.changed([a]), [])
            self.assertEqual(w.changed([a, b]), [])          # a lazily imported module: its hash when first seen
            b.write_text("y = 2\n")
            self.assertEqual(w.changed([a, b]), [b])
            a.unlink()
            self.assertEqual(sorted(w.changed([])), sorted([a, b]))

    def test_loaded_code_files(self):
        repo = Path("/r")
        mods = {"play": mock.Mock(__file__="/r/runner/play.py"),
                "metrics": mock.Mock(__file__="/r/bridge/src/bg3ai/metrics.py"),
                "dep": mock.Mock(__file__="/r/bridge/.venv/lib/site-packages/x.py"),
                "json": mock.Mock(__file__="/usr/lib/python3/json/__init__.py"),
                "ext": mock.Mock(__file__="/r/x.so"), "builtin": object()}
        self.assertEqual(P.loaded_code_files(repo, mods),
                         [Path("/r/bridge/src/bg3ai/metrics.py"), Path("/r/runner/play.py")])
        self.assertIn(Path(P.__file__).resolve(), P.loaded_code_files())

    def test_handover(self):
        with tempfile.TemporaryDirectory() as d:
            h = Path(d, "handover.json")
            P.write_handover({"run_end": 5.0}, h)
            self.assertEqual(P.read_handover(h)["run_end"], 5.0)
            self.assertFalse(h.exists())                     # consumed
            self.assertIsNone(P.read_handover(h))
            P.write_handover({}, h)
            self.assertIsNone(P.read_handover(h, now=time.time() + P.HANDOVER_MAX_AGE + 1))  # stale
            h.write_text(json.dumps({"pid": -1, "t": time.time()}))
            self.assertIsNone(P.read_handover(h))            # another process's

    def _runner(self, stale):
        r = object.__new__(P.Runner)
        r.stop_requested = False
        r.bridge_hashes = None
        r.code_watch = mock.Mock(changed=mock.Mock(return_value=stale), hashes={})
        r.event = mock.Mock()
        return r

    def test_between_sessions(self):
        f = Path(P.__file__).resolve()
        logs = []
        with mock.patch.object(P, "log", logs.append), mock.patch.object(P, "tree_hashes", return_value={"a": "1"}):
            r = self._runner([])
            r.code_update_check()                             # nothing changed
            with mock.patch.object(P, "tree_hashes", return_value={"a": "2", "b": "1"}):
                r.code_update_check()
            self.assertIn("bridge code changed since last session: a, b", logs)
            ok = mock.Mock(returncode=0, stdout="", stderr="")
            r = self._runner([f])
            with mock.patch.object(P.subprocess, "run", return_value=ok) as run:
                with self.assertRaises(P.RestartRunner):
                    r.code_update_check()
            self.assertEqual(run.call_args.args[0][-2:], ["run", "--help"])
            self.assertTrue(any(m.startswith("runner code changed: restarting") for m in logs))
            r = self._runner([f])
            r.stop_requested = True                          # stopping anyway: no restart
            r.code_update_check()
            bad = mock.Mock(returncode=1, stdout="", stderr="Traceback\nSyntaxError: invalid syntax")
            r = self._runner([f])
            with mock.patch.object(P.subprocess, "run", return_value=bad):
                r.code_update_check()                         # broken new code: keep running the old one
            self.assertIn(f, r.code_watch.hashes)            # and don't retry until it changes again
            r.event.assert_called_once()
            self.assertIn("SyntaxError", r.event.call_args.kwargs["error"])

    def test_handover_data_roundtrip(self):
        r = self._runner([])
        r.pending_notes, r.quick_failures, r.new_game = ["note"], 1, True
        r.last_triage = ("S007", Path("/x/S007.triage.md"))
        d = json.loads(json.dumps(r.handover_data(123.0, 100.0)))
        self.assertEqual((d["run_end"], d["run_started"], d["pending_notes"]), (123.0, 100.0, ["note"]))
        self.assertEqual(d["last_triage"], ["S007", "/x/S007.triage.md"])

    def test_fake_runner_execs_itself(self):
        """A real execv: same pid, same argv, the lock taken again, the handover read by the successor."""
        script = (
            "import os, sys\n"
            f"sys.path.insert(0, {str(Path(P.__file__).resolve().parent)!r})\n"
            "import play as P\n"
            "lock = P.acquire_lock()\n"
            "h = P.read_handover()\n"
            "if h is None:\n"
            "    P.write_handover({'n': 1})\n"
            "    print('first', os.getpid(), sys.argv[1:], flush=True)\n"
            "    P.exec_new_runner(lock, None)\n"
            "print('second', os.getpid(), sys.argv[1:], h['n'], flush=True)\n")
        with tempfile.TemporaryDirectory() as d:
            Path(d, "fake_runner.py").write_text(script)
            env = {**__import__("os").environ, "BG3AI_RUNTIME": d}
            p = __import__("subprocess").run([sys.executable, "fake_runner.py", "run", "--x"], cwd=d, env=env,
                                             capture_output=True, text=True, timeout=60)
            self.assertEqual(p.returncode, 0, p.stderr)
            first, second = p.stdout.split("\n")[:2]
            self.assertEqual(first.split()[1], second.split()[1])     # same pid
            self.assertIn("['run', '--x']", second)
            self.assertTrue(second.endswith(" 1"))
            self.assertFalse(Path(d, "runner-handover.json").exists())

class MemorySessionWithoutHooks(unittest.TestCase):
    """The runner opens the memory session and makes the stop check itself (were the SessionStart and Stop hooks),
    through the real tools/memory/session.py on a real memory repo."""

    def test_open_then_stop_check(self):
        import _testutil as TU
        import memlib as M
        import new_playthrough as NP
        TU.isolate_git()
        with tempfile.TemporaryDirectory() as d, mock.patch.object(P, "log", lambda msg: None):
            r = P.Runner.__new__(P.Runner)
            r.opt = P.Options("pt", None, 30, "opus")
            r.mem = Path(d) / "memory"
            NP.create_memory("pt", r.mem)
            deadline = time.time() + 30 * 60
            sid, brief = r.open_memory_session(deadline, 30)
            self.assertEqual(sid, "S001")
            self.assertTrue(brief.startswith("=== BG3 PLAYTHROUGH MEMORY — session S001 ==="))
            self.assertIn("Autonomous session: keep playing until the ⏱ line", brief)
            self.assertIn("Keep playing: you'll be told when to wrap up", r.stop_check(sid, deadline, 30))
            near = time.time() + 120   # inside the last 3 minutes: wrap-up is due
            self.assertIn("HANDOFF.md hasn't been rewritten", r.stop_check(sid, near, 30))
            (r.mem / "HANDOFF.md").write_text("# Handoff S001 → S002\nSave to load: \"S001-end\" — camp\n\n"
                                              "## Doing right now\nResting.\n\n## Next steps\n1. Go north.\n")
            (r.mem / "objectives.md").write_text("# Objectives\n## Main\n- reach camp [seen S001]\n")
            self.assertIsNone(r.stop_check(sid, near, 30))     # ends: linted and committed
            self.assertFalse(M.load_state(r.mem)["open"])
            self.assertEqual(M.git(r.mem, "log", "-1", "--format=%s").stdout.strip(), "S001")

    def test_no_memory_falls_back_to_session_start(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(P, "log", lambda msg: None):
            r = P.Runner.__new__(P.Runner)
            r.opt = P.Options("pt", None, 30, "opus")
            r.mem = Path(d) / "missing"
            self.assertEqual(r.open_memory_session(time.time() + 1800, 30), (None, ""))
            self.assertIsNone(r.stop_check("S001", time.time() + 1800, 30))


class MemoryKeeperHook(unittest.TestCase):
    """Runner.start_keeper/join_keeper: threaded pass, logged to events + keeper_history, joined before play."""

    def test_recover_at_start(self):  # MEM-1: a pass the runner died in is undone before any session
        import memory_keeper as K
        with tempfile.TemporaryDirectory() as d:
            events = []
            mem = Path(d) / "memory"
            mem.mkdir()
            (mem / "quests.md").write_text("# Quests\n")
            K.save_pending(Path(d), mem, "S007", K.snapshot(mem))
            (mem / "quests.md").write_text("# Quests\n- unvalidated [seen]\n")

            class FakeState:
                dir = Path(d)

            class FakeRunner:
                opt = P.Options("pt", None, 20, "opus")
                state = FakeState()
                def event(self, kind, **f): events.append((kind, f))
                recover_keeper = P.Runner.recover_keeper
            r = FakeRunner()
            r.mem = mem
            with mock.patch.object(P, "log", lambda *a, **k: None):
                r.recover_keeper()
            self.assertEqual((mem / "quests.md").read_text(), "# Quests\n")
            self.assertEqual(events[0][0], "keeper_recovered")
            self.assertIn("never finished", events[0][1]["detail"])

    def test_start_and_join(self):
        import memory_keeper as K
        with tempfile.TemporaryDirectory() as d:
            calls, events = [], []

            class FakeState:
                dir = Path(d)

            class FakeRunner:
                opt = P.Options("pt", None, 20, "opus")
                state, mem, keeper_thread = FakeState(), Path(d) / "memory", None
                def event(self, kind, **f): events.append((kind, f))
                def memory_state(self): return {"open": False}
                start_keeper = P.Runner.start_keeper
                join_keeper = P.Runner.join_keeper
                def write_review(self, sid): pass   # SessionReview tests it

            def fake_run(mem, n, base, model, effort, minutes, refused=None, client=None):
                calls.append((n, Path(base).name, model, effort, minutes, client))
                got_refused.append(refused)
                release.wait(10)  # alive until the test looked (a 0.2 s sleep could end first on a loaded machine)
                return K.KeeperResult("S007", status="ok", seconds=0.2, files=["quests.md"], commit="abc")

            release = threading.Event()

            r = FakeRunner()
            res = P.SessionResult("S007", "c", 0, "exited", 0.0, 1200.0)
            got_refused = []
            wyll = {"tool": "level_up", "result": "level_up: FAILED — passive Thirsting Blade: not offered; can pick: "
                    "Devil's Sight\n(plan: Wyll L5 from memory/party-build.md)"}
            with mock.patch.object(K, "run_keeper", fake_run), \
                    mock.patch.object(P, "read_actions_since", lambda t, *a, **k: [wyll]):
                r.start_keeper("S007", res, [{"name": "Gale", "level": 8, "problems": "check_plans …: no plan line"},
                                             {"name": "wyll", "level": 5, "problems": "dup of the refused line"}])
                self.assertTrue(r.keeper_thread.is_alive())
                release.set()
                r.join_keeper()
            self.assertIsNone(r.keeper_thread)
            # default: the play model and the play client (claude: run_keeper's default non-MCP file-tools pass)
            self.assertEqual(calls, [(7, "S007-keeper", "opus", "low", 5.0, "claude")])
            self.assertEqual([(x["name"], x["level"]) for x in got_refused[0]], [("Wyll", 5), ("Gale", 8)])
            self.assertEqual(events[0][0], "memory_keeper")
            self.assertEqual(events[0][1]["status"], "ok")
            hist = json.loads((Path(d) / "keeper_history.jsonl").read_text())
            self.assertEqual(hist["files"], ["quests.md"])
            self.assertEqual((hist["model"], events[0][1]["model"]), ("opus", "opus"))
            self.assertEqual((hist["client"], events[0][1]["client"]), ("claude", "claude"))
            # a codex run keeps its memory with codex (run_keeper then goes through the keeper-mode MCP server)
            r.opt = P.Options("pt", None, 20, "gpt-5", client="codex")
            with mock.patch.object(K, "run_keeper", fake_run), \
                    mock.patch.object(P, "read_actions_since", lambda t, *a, **k: []):
                r.start_keeper("S007", res)
                r.join_keeper()
            self.assertEqual(calls[-1][-1], "codex")
            self.assertEqual(calls[-1][2], "gpt-5")
            r.opt = P.Options("pt", None, 20, "opus")
            # quick exits and --no-memory-keeper skip the pass
            r.start_keeper("S008", P.SessionResult("S008", "c", 0, "exited", 0.0, 30.0))
            self.assertIsNone(r.keeper_thread)
            r.opt = P.Options("pt", None, 20, "opus", keeper=False)
            r.pending_notes = []
            r.start_keeper("S009", res, [{"name": "Gale", "level": 8, "problems": "no plan line"}])
            self.assertIsNone(r.keeper_thread)
            self.assertIn("Gale L8: no plan line", r.pending_notes[0])  # no keeper: the player gets the note


class Triage(unittest.TestCase):
    def _session(self, d: Path) -> Path:
        base = d / "S009"
        use = lambda i, name, inp: {"type": "tool_use", "id": f"t{i}", "name": f"mcp__bg3__{name}", "input": inp}
        res = lambda i, text, err=False: {"type": "tool_result", "tool_use_id": f"t{i}", "is_error": err,
                                          "content": [{"type": "text", "text": json.dumps({"result": text})}]}
        calls = [
            (use(1, "move_to", {"x": 1}), res(1, "move_to: done")),
            (use(2, "use_ability", {"target": "e4"}), res(2, "use_ability: FAILED — target is out of range (2.2 m, range 0.0 m)")),
            (use(3, "do_actions", {"actions": "[]"}), res(3, "1. move_to: done\n2. use_ability: FAILED — target is out of range (1.9 m, range 0.0 m)\nSTOPPED: step 2 failed")),
            ({"type": "tool_use", "id": "t4", "name": "ToolSearch", "input": {"query": "select:mcp__bg3__click"}}, res(4, "ok")),
            (use(5, "click", {"x": 1, "y": 2}), res(5, "clicked")),
            (use(6, "click", {"x": 1, "y": 2}), res(6, "clicked")),
            (use(7, "click", {"x": 1, "y": 2}), res(7, "clicked")),
            (use(8, "screenshot", {}), res(8, "img")),
            (use(9, "move_to", {"x": 2}), res(9, "move_to: done")),
            (use(10, "travel_to", {"objective": "o1"}), {"type": "tool_result", "tool_use_id": "t10", "is_error": True,
                                                       "content": "<tool_use_error>Error: No such tool available: travel_to</tool_use_error>"}),
        ]
        stream, turns, t = [], [], 1000.0
        for i, (u, r) in enumerate(calls, 1):
            stream.append({"type": "assistant", "message": {"content": [u]}})
            stream.append({"type": "user", "message": {"content": [r]}})
            turns += [{"type": "use", "t": t, "id": f"t{i}", "name": u["name"]}, {"type": "result", "t": t + 1, "id": f"t{i}"}]
            t += 2 if i != 8 else 30  # a 30 s gap before the 2nd move_to
        stream.append({"type": "result", "result": "Reached the Helm. Saved as S009-end."})
        Path(f"{base}.jsonl").write_text("\n".join(map(json.dumps, stream)) + "\n")
        Path(f"{base}.turns.jsonl").write_text("\n".join(map(json.dumps, turns)) + "\n")
        metrics = [{"type": "tool", "tool": "move_to", "t0": 1000.0, "t1": 1001.0},
                   {"type": "tool", "tool": "move_to", "t0": 1044.0, "t1": 1045.0}]
        Path(f"{base}.metrics.jsonl").write_text("\n".join(map(json.dumps, metrics)) + "\n")
        Path(f"{base}.progress.jsonl").write_text(json.dumps({"type": "stuck", "t": 1500, "since": 1000,
                                                              "idle_min": 8, "actions": 40}) + "\n")
        return base

    def test_report(self):
        with tempfile.TemporaryDirectory() as d:
            base = self._session(Path(d))
            md = P.triage.write(base).read_text()
        self.assertIn("**use_ability** ×2 (via do_actions)", md)              # top-level + batch step, one group
        self.assertIn("range N m", md)
        self.assertIn("**travel_to** ×1", md)                                 # is_error
        self.assertIn("43.0 s", md)                                           # the long gap
        self.assertIn("during: ", md)
        self.assertIn("select:mcp__bg3__click", md)
        self.assertIn("screenshots: 1; clicks: 3", md)
        self.assertIn("`click` repeated 3×", md)
        self.assertIn("no story progress for 8 min", md)
        self.assertIn("Reached the Helm", md)

    def test_combat_gaps_split_own_turn(self):
        # combat: end_turn at 1001, enemies act 1002-1020 (pturn false), next action 1035: 34 s raw, 15 s own;
        # then a 25 s gap with a 19 s enemy turn: not long once enemies are excluded.
        with tempfile.TemporaryDirectory() as d:
            base = Path(d) / "S011"
            metrics = [{"type": "tool", "tool": "end_turn", "t0": 1000.0, "t1": 1001.0},
                       {"type": "tool", "tool": "use_ability", "t0": 1035.0, "t1": 1036.0},
                       {"type": "tool", "tool": "use_ability", "t0": 1061.0, "t1": 1062.0}]
            metrics += [{"type": "sample", "t": float(t), "moving": False, "job": 0, "dlg": False, "combat": True,
                         "pturn": not (1002 <= t <= 1020 or 1037 <= t <= 1055)} for t in range(1000, 1063)]
            Path(f"{base}.metrics.jsonl").write_text("\n".join(map(json.dumps, metrics)) + "\n")
            md = P.triage.write(base).read_text()
        self.assertIn("Idle gaps > 10 s between visible actions (1)", md)
        self.assertIn("**15.0 s** idle (combat; own turn 15.0 s of 34.0 s, enemy turns 19.0 s)", md)
        self.assertIn("not counted: 1 gap(s)", md)
        self.assertIn("25 s = 19 s enemy + 6 s ours", md)  # the parts, not "25 s with 19 s enemy"

    def test_bare_failed_header_takes_the_next_line(self):
        # S034/S037: "equip_best: FAILED\nFAILED: Claude: …" grouped as a bare "FAILED:" plus the real reason
        text = "equip_best: FAILED\nFAILED: Claude: e599 Boots (rare vs common): in a conversation\nMAIN HANDS x"
        self.assertEqual(P.triage.failures("equip_best", {}, text),
                         [("equip_best", "FAILED: Claude: e599 Boots (rare vs common): in a conversation")])

    def test_missing_files_still_write(self):
        with tempfile.TemporaryDirectory() as d:
            md = P.triage.write(Path(d) / "S001").read_text()
        self.assertIn("# Triage S001", md)
        self.assertIn("(none)", md)

    def test_wired_into_run_claude(self):
        src = Path(P.__file__).read_text()
        self.assertIn('self.write_triage(f"{sid}{tag}", base)', src)



class SessionReview(unittest.TestCase):
    """Runner.write_review: tools/logs/review_session.py's Sxxx.review.md once the keeper pass is done."""

    def _runner(self, d: Path, reviews: list | None = None):
        class FakeState:
            dir = d / "sessions" / "pt"

        class FakeRunner:
            opt = P.Options("pt", None, 20, "opus")
            state, mem, keeper_thread, pending_notes = FakeState(), d / "memory", None, []
            def event(self, kind, **f):
                with open(self.state.dir / "runner_events.jsonl", "a") as fh:
                    fh.write(json.dumps({"t": time.time(), "kind": kind, **f}) + "\n")
            def memory_state(self): return {"open": False}
            start_keeper = P.Runner.start_keeper
            join_keeper = P.Runner.join_keeper
            write_review = P.Runner.write_review
        FakeState.dir.mkdir(parents=True)
        r = FakeRunner()
        if reviews is not None:
            r.write_review = lambda sid: reviews.append(
                (sid, (r.state.dir / "runner_events.jsonl").read_text() if (r.state.dir / "runner_events.jsonl").exists() else ""))
        return r

    def test_real_review_after_the_keeper(self):
        import memory_keeper as K
        with tempfile.TemporaryDirectory() as d:
            r = self._runner(Path(d))
            Triage._session(None, r.state.dir)   # S009 stream + turns + metrics
            logs = []
            res = P.SessionResult("S009", "c", 0, "exited", 0.0, 1200.0)
            with mock.patch.object(K, "run_keeper", lambda *a, **k: K.KeeperResult("S009", status="ok", seconds=1.0,
                                                                                   files=["quests.md"], commit="abc")), \
                    mock.patch.object(P, "read_actions_since", lambda t, *a, **k: []), \
                    mock.patch.object(P, "RUNTIME", Path(d)), mock.patch.object(P, "log", logs.append):
                r.start_keeper("S009", res)
                r.join_keeper()
            md = (r.state.dir / "S009.review.md").read_text()
        self.assertIn("# Review pt/S009", md)
        self.assertIn("keeper: ok in 1.0 s", md)   # written after the keeper's memory_keeper event
        self.assertIn(f"S009: review {r.state.dir / 'S009.review.md'}", logs)

    def test_review_without_a_keeper_pass_and_not_in_dry_run(self):
        with tempfile.TemporaryDirectory() as d:
            reviews = []
            r = self._runner(Path(d), reviews)
            r.start_keeper("S008", P.SessionResult("S008", "c", 0, "exited", 0.0, 30.0))   # too short for a keeper
            self.assertEqual([x[0] for x in reviews], ["S008"])
            r.opt = P.Options("pt", None, 20, "opus", dry_run=True)
            r.start_keeper("S009", P.SessionResult("S009", "c", 0, "exited", 0.0, 1200.0))
            self.assertEqual([x[0] for x in reviews], ["S008"])

    def test_review_even_when_the_keeper_raises(self):
        import memory_keeper as K
        with tempfile.TemporaryDirectory() as d:
            reviews = []
            r = self._runner(Path(d), reviews)

            def boom(*a, **k):
                raise RuntimeError("keeper crashed")
            with mock.patch.object(K, "run_keeper", boom), mock.patch.object(P, "log", lambda *a, **k: None), \
                    mock.patch.object(P, "read_actions_since", lambda t, *a, **k: []), \
                    mock.patch("threading.excepthook", lambda *a: None):
                r.start_keeper("S007", P.SessionResult("S007", "c", 0, "exited", 0.0, 1200.0))
                r.join_keeper()
            self.assertEqual([x[0] for x in reviews], ["S007"])

    def test_failure_and_timeout_never_raise(self):
        with tempfile.TemporaryDirectory() as d:
            r = self._runner(Path(d))
            logs = []
            with mock.patch.object(P, "RUNTIME", Path(d)), mock.patch.object(P, "log", logs.append):
                self.assertIsNone(r.write_review("S001"))     # no transcript: review_session exits 1
                with mock.patch.object(P.subprocess, "run", side_effect=P.subprocess.TimeoutExpired("x", 120)):
                    self.assertIsNone(r.write_review("S001"))
            self.assertIn("S001: review failed (exit 1)", logs[0])
            self.assertIn("no timed calls", logs[0])
            self.assertEqual(logs[1], f"S001: review killed after {P.REVIEW_TIMEOUT} s")


class PlanCheck(unittest.TestCase):
    ROWS = [{"name": "Gale", "level": 6, "plan": {"spells": ["Haste", "Slow"]}, "problems": [], "offers": "pick 2"},
            {"name": "Wyll", "level": 5, "plan": {"passives": ["Thirsting Blade"]},
             "problems": ["passive Thirsting Blade: not offered", "spells: 0 of 1 picks"],
             "offers": "pick 1 of Warlock invocations L5: Devil's Sight, Fiendish Vigour"}]

    def test_all_valid(self):
        text, bad = P.plan_check_result(self.ROWS[:1])
        self.assertEqual(bad, [])
        self.assertIn("1 checked, all valid", text)

    def test_invalid_line_goes_to_keeper_format(self):
        text, bad = P.plan_check_result(self.ROWS)
        self.assertIn("1 of 2 INVALID: Wyll L5: passive Thirsting Blade: not offered", text)
        self.assertEqual((bad[0]["name"], bad[0]["level"]), ("Wyll", 5))
        self.assertIn("Fiendish Vigour", bad[0]["problems"])
        self.assertIn('"Thirsting Blade"', bad[0]["problems"])
        self.assertIn("Wyll L5:", P.plan_note(bad))

    def test_cmd_points_at_memory(self):
        cmd = P.check_plans_cmd(Path("/tmp/x.json"), Path("/m"))
        self.assertEqual(cmd[-4:], ["--json", "/tmp/x.json", "--memory", "/m"])
        self.assertTrue(cmd[-5].endswith("check_plans.py"))


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""runner/play.py's long runs and --runs series: run budgets and caps, the cost pause, frozen series and their run
records (moved from test_play.py). Never launches claude or the game.

    python3 runner/test_play_series.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import testenv  # noqa: I001  (first: the game's profile folders are a temp dir before play)
# RUN-9: play.RUNTIME is read at import; a test must never append to the real runtime/runner.log (the phantom
# "0.01 s after SIGTERM" kill bursts the orchestrator chased). Tests that need a runtime set their own.
os.environ.setdefault("BG3AI_RUNTIME", tempfile.mkdtemp(prefix="bg3ai-test-runtime-"))
os.environ.setdefault("BG3AI_KNOWLEDGE_LOCK", "0")   # never chmod the real play/knowledge (Knowledge tests patch it)
import play as P  # noqa: I001  (play puts packages/harness/src on sys.path)
from bg3harness.runner import checks_side as rchecks
from bg3harness.runner import codewatch as rcode
from bg3harness.runner import env as renv
from bg3harness.runner import policy as rpolicy
from bg3harness.runner import state as rstate
import legacy_cli as lcli
import legacy_playthrough as lpt
import legacy_series as lser
from bg3harness.game import io as G

G_FUNCS = {n for n, v in vars(G).items() if callable(v) and getattr(v, "__module__", None) == G.__name__
           and not isinstance(v, type)}


def home(name: str):
    """The module a patched name lives in: the game's I/O functions moved to bg3harness.game.io."""
    return G if name in G_FUNCS else testenv.home(name)



def _stream(path: Path, *events) -> Path:
    path.write_text("".join(json.dumps(e) + "\n" for e in events))
    return path


def _use(i: str, name: str, inp: dict) -> dict:
    return {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": i, "name": name, "input": inp}]}}


def _res(i: str, text: str, is_error: bool = False) -> dict:
    return {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": i, "is_error": is_error,
                                                     "content": [{"type": "text", "text": text}]}]}}


class FakeUI:
    """ui.state + the ui.key / ui.press RPCs over a game screen. screen None: the game doesn't answer."""

    def __init__(self, screen=None, panels=(), resume_works=True):
        self.screen, self.panels, self.resume_works, self.calls = screen, list(panels), resume_works, []

    def ui_state(self):
        return None if self.screen is None else {"screen": self.screen, "panels_open": self.panels}

    def ui_screen(self):
        return self.screen

    def rpc(self, op, args=None, context="server", timeout=None):
        self.calls.append((op, (args or {}).get("key") or (args or {}).get("button")))
        if op == "ui.key" and args.get("key") == "ESCAPE":
            self.screen = {"game": "game_menu", "game_menu": "game"}.get(self.screen, self.screen)
        elif op == "ui.press" and args.get("button") == "Resume" and self.resume_works and self.screen == "game_menu":
            self.screen = "game"
        return {}


class LongRun(unittest.TestCase):
    """Playthrough mode for many hours: the crash-loop stop, the rolling 24 h cost cap, the status line."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="bg3ai-long-"))
        self.addCleanup(__import__("shutil").rmtree, self.tmp, True)
        self.saved = {n: getattr(home(n), n) for n in ("RUNTIME", "PLAYTHROUGHS", "COST_PAUSE_CHECK_S", "day_cost")}
        self.addCleanup(lambda: [setattr(home(n), n, v) for n, v in self.saved.items()])
        renv.RUNTIME, renv.PLAYTHROUGHS = self.tmp / "runtime", self.tmp / "playthroughs"
        renv.RUNTIME.mkdir(parents=True)
        self.ui = FakeUI()   # never the real game's UI (the heat pause presses ESCAPE in it)
        for name in ("ui_state", "ui_screen", "rpc"):
            p = mock.patch.object(home(name), name, getattr(self.ui, name))
            p.start()
            self.addCleanup(p.stop)

    def test_crash_loop(self):
        ev = [{"t": 1000.0 + 60 * k, "kind": "fault", "fault": "crash"} for k in range(5)]
        self.assertIsNone(rpolicy.crash_loop(ev, 1300.0, 0.0))
        ev.append({"t": 1310.0, "kind": "setup_failed"})
        self.assertEqual(rpolicy.crash_loop(ev, 1320.0, 0.0), "crash loop: 6 game crashes/hangs/setup failures in 5 min")
        self.assertIsNone(rpolicy.crash_loop(ev, 1320.0, 1100.0))                 # an operator restart counts from its start
        self.assertIsNone(rpolicy.crash_loop(ev, 1000.0 + 3600 + 61, 0.0))        # the first one left the hour
        agent = [{"t": 1000.0 + k, "kind": "fault", "fault": "agent_hang"} for k in range(9)]
        self.assertIsNone(rpolicy.crash_loop(agent, 1100.0, 0.0))                 # the agent's hangs aren't the game's

    def test_status_line(self):
        seen = {"regions": ["Lower City"], "areas": ["Lower City|Bhaal Temple"]}
        line = rpolicy.status_line(1_000_000.0, "claude-run-2", "playing S070 (until 16:30)", 1_000_000.0 - 5400, 3, seen,
                             12.345, 40.0, 1)
        self.assertRegex(line, r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d claude-run-2: playing S070 \(until 16:30\) \| up 1.5 h, "
                               r"3 session\(s\) \| milestones \d+/\d+ \(Act 3\), next .+ \| \$12.35 in 24 h \(cap \$40\) \| "
                               r"1 game fault\(s\) in 60 min \(stop at 6\)$")
        line = rpolicy.status_line(1.0, "pt", "STOPPED (x)", 0.0, 0, dict(seen, game_end={"t": 1}), 0.0, None, 0)
        self.assertIn("THE GAME IS FINISHED", line)
        self.assertNotIn("cap", line)

    def test_cost_pause_waits_and_resumes(self):
        r = lpt.Runner(rstate.Options("long", None, 30, "opus", max_cost_per_day=20.0))
        renv.COST_PAUSE_CHECK_S = 0.05
        spent = {"usd": 25.0}
        rpolicy.day_cost = lambda d, now: spent["usd"]
        self.assertTrue(r.cost_paused())
        self.assertIn("PAUSED: $25.00 in 24 h >= --max-cost-per-day $20", (renv.RUNTIME / renv.STATUS_LINE).read_text())
        self.assertTrue(r.cost_paused())   # still over: one pause event, not one per check
        spent["usd"] = 19.5
        self.assertFalse(r.cost_paused())
        evs = [json.loads(ln) for ln in (r.state.dir / "runner_events.jsonl").read_text().splitlines()]
        self.assertEqual([(e["kind"], e["spent"]) for e in evs], [("cost_pause", 25.0), ("cost_resume", 19.5)])
        r2 = lpt.Runner(rstate.Options("long", None, 30, "opus"))
        self.assertFalse(r2.cost_paused())   # no cap: never

    def thermal(self, temps: list[tuple[int, int]], end: float | None = None) -> None:
        """runtime/thermal.log in thermal_log.sh's format: one line per 5 s ending at `end` (default now)."""
        end = time.time() if end is None else end
        t0 = end - 5 * (len(temps) - 1)
        (renv.RUNTIME / "thermal.log").write_text("".join(
            f"{renv.datetime.fromtimestamp(t0 + 5 * i):%Y-%m-%d %H:%M:%S} cpu={c} acpi={c} amdgpu=50 nvme=40 "
            f"nv=[{g},35.0,1500,99,0x0] profile=balanced load=5.0 bg3=up\n" for i, (c, g) in enumerate(temps)))

    def test_heat_pause_waits_until_cool(self):
        # the test machine cut its power twice under the naut series load: no session starts while it is too hot
        r = lpt.Runner(rstate.Options("long", None, 30, "opus"))
        self.thermal([(93, 70)] * 14)                                    # 65 s at 93 C (pause at 92 C for 60 s)
        sleeps = []

        def sleep(s):   # the next check: cool for a minute
            sleeps.append(s)
            self.thermal([(93, 70)] * 3 + [(80, 70)] * 12 if len(sleeps) == 1 else [(80, 60)] * 14)
        with mock.patch.object(rcode.time, "sleep", side_effect=sleep):
            self.assertTrue(r.heat_paused())
        self.assertEqual(sleeps, [renv.HEAT_PAUSE_CHECK_S] * 2)              # 1st check: cool for only 55 s
        self.assertIn("PAUSED: too hot (CPU 80 C, GPU 70 C; limit 92/85 C)", (renv.RUNTIME / renv.STATUS_LINE).read_text())
        evs = [json.loads(ln) for ln in (r.state.dir / "runner_events.jsonl").read_text().splitlines()]
        self.assertEqual([(e["kind"], e["cpu_c"]) for e in evs], [("heat_pause", 93), ("heat_resume", 80)])
        self.thermal([(80, 86)] * 10)                                    # GPU 86 C for 45 s: not yet
        self.assertFalse(r.heat_paused())
        self.thermal([(99, 99)] * 14, end=time.time() - 120)               # stale log: no pause
        self.assertFalse(r.heat_paused())
        (renv.RUNTIME / "thermal.log").unlink()
        self.assertFalse(r.heat_paused())                                # no log (no ramwatch): no pause

    def _hot_pause(self, r, logs, stop_at=None):
        """A heat pause that cools at the 2nd check (stop_at: STOP written at that check instead)."""
        self.thermal([(93, 70)] * 14)
        n = []

        def sleep(s):
            if s != renv.HEAT_PAUSE_CHECK_S:
                return
            n.append(s)
            self.assertEqual(self.ui.screen, "game_menu" if self.ui.calls else self.ui.screen)  # open while hot
            if stop_at and len(n) == stop_at:
                (renv.RUNTIME / "STOP").write_text("x")
            self.thermal([(80, 60)] * 14 if len(n) >= 2 else [(93, 70)] * 14)
        with mock.patch.object(rcode.time, "sleep", side_effect=sleep), mock.patch.object(renv, "log", logs.append):
            return r.heat_paused()

    def test_heat_pause_opens_the_game_menu_and_closes_it_when_cool(self):
        r = lpt.Runner(rstate.Options("long", None, 30, "opus"))
        self.ui.screen, logs = "game", []
        self.assertTrue(self._hot_pause(r, logs))
        self.assertEqual(self.ui.calls, [("ui.key", "ESCAPE"), ("ui.press", "Resume")])   # the player's ESC; Resume
        self.assertEqual(self.ui.screen, "game")
        self.assertIn("heat: game menu open (ESCAPE) while it cools", logs)
        self.assertIn("heat: game menu closed", logs)
        self.assertLess(logs.index("heat: game menu closed"), [i for i, x in enumerate(logs) if "playing again" in x][0])

    def test_heat_pause_resume_button_fails_then_escape(self):
        r = lpt.Runner(rstate.Options("long", None, 30, "opus"))
        self.ui.screen, self.ui.resume_works, logs = "game", False, []
        with mock.patch.object(renv, "HEAT_MENU_WAIT_S", 0.0):
            r.wait_screen = lambda want, timeout=0.0: lpt.Runner.wait_screen(want, 0.0)
            self.assertTrue(self._hot_pause(r, logs))
        self.assertEqual(self.ui.calls, [("ui.key", "ESCAPE"), ("ui.press", "Resume"), ("ui.key", "ESCAPE")])
        self.assertEqual(self.ui.screen, "game")

    def test_heat_pause_skips_the_menu_where_escape_does_something_else(self):
        for screen, panels, why in (("dialogue", [], "the dialogue screen is up"),
                                    ("loading", [], "the loading screen is up"),
                                    ("movie", [], "the movie screen is up"),
                                    ("game", ["Trade"], "the game screen with Trade is up"),
                                    (None, [], "the game's UI doesn't answer")):
            with self.subTest(screen=screen):
                r = lpt.Runner(rstate.Options("long", None, 30, "opus"))
                self.ui.screen, self.ui.panels, self.ui.calls, logs = screen, panels, [], []
                self.assertTrue(self._hot_pause(r, logs))           # still paused: only the menu is skipped
                self.assertEqual(self.ui.calls, [])                 # no key pressed
                self.assertTrue(any(why in x for x in logs), logs)
                self.assertTrue(any("playing again" in x for x in logs))

    def test_heat_pause_stopped_while_paused_still_closes_the_menu(self):
        r = lpt.Runner(rstate.Options("long", None, 30, "opus"))
        self.ui.screen, logs = "game", []
        self.assertTrue(self._hot_pause(r, logs, stop_at=1))
        self.assertEqual(self.ui.screen, "game")
        self.assertIn("heat: game menu closed", logs)

    def test_heat_menu_never_after_a_heat_kill(self):
        r = lpt.Runner(rstate.Options("long", None, 30, "opus"))
        self.ui.screen = "game"   # (a game the kill left would be dead; nothing is pressed either way)
        self.thermal([(98, 70)] * 4 + [(85, 60)] * 3)
        with mock.patch.object(rcode.time, "sleep", side_effect=lambda s: self.thermal([(70, 50)] * 14)):
            self.assertTrue(r.heat_paused(after_kill=True))
        self.assertEqual(self.ui.calls, [])

    def test_heat_kill_waits_before_the_relaunch(self):
        r = lpt.Runner(rstate.Options("long", None, 30, "opus"))
        r.state["game"] = {"stamp": "s", "launched": time.time() - 600}
        self.assertIsNone(r.heat_killed())
        (renv.RUNTIME / "heat_kills.jsonl").write_text(
            json.dumps({"kind": "heat_kill", "t": time.time() - 900, "why": "old launch"}) + "\n"
            + json.dumps({"kind": "heat_kill", "t": time.time() - 10, "why": "CPU 98 C / GPU 70 C for 20 s"}) + "\n")
        self.assertEqual(r.heat_killed(), "CPU 98 C / GPU 70 C for 20 s")
        self.thermal([(98, 70)] * 4 + [(85, 60)] * 3)                    # cooling, but not for 60 s yet
        self.assertFalse(r.heat_paused())                                # between sessions: not a pause
        with mock.patch.object(rcode.time, "sleep", side_effect=lambda s: self.thermal([(70, 50)] * 14)):
            self.assertTrue(r.heat_paused(after_kill=True))              # after the kill: wait for 60 s cool
        self.thermal([(70, 50)] * 14)
        self.assertFalse(r.heat_paused(after_kill=True))                 # already cool: relaunch at once

    def test_recover_after_a_heat_kill_names_it_and_waits(self):
        r = lpt.Runner(rstate.Options("long", None, 30, "opus"))
        r.state["game"] = {"stamp": "s", "launched": time.time() - 600}
        (renv.RUNTIME / "heat_kills.jsonl").write_text(json.dumps({"kind": "heat_kill", "t": time.time() - 10,
                                                                "why": "CPU 98 C / GPU 70 C for 20 s"}) + "\n")
        order = []
        with mock.patch.multiple(r, game_alive=lambda: False, check_game_oom=lambda: None,
                                 kill_game=lambda: order.append("kill"), choose_save=lambda: None,
                                 load_save=lambda s: order.append("relaunch") or "S070-end",
                                 heat_paused=lambda after_kill=False: order.append(("cool", after_kill)) or True), \
                mock.patch.object(rpolicy, "read_actions_since", return_value=[]):
            r.recover("crashed")
        self.assertEqual(order, ["kill", ("cool", True), "relaunch"])
        self.assertIn("crashed (the heat guard stopped it: CPU 98 C / GPU 70 C for 20 s)", r.pending_notes[0])

    def test_status_prints_the_last_thermal_line(self):
        import contextlib
        import io
        self.thermal([(80, 68)])
        with mock.patch.object(G, "port_open", return_value=False), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            lcli.cmd_status(P.argparse.Namespace(playthrough=None))
        self.assertRegex(out.getvalue(), r"thermal:  \d{4}-\d\d-\d\d \d\d:\d\d:\d\d cpu=80 .* nv=\[68,")
        (renv.RUNTIME / "thermal.log").unlink()
        with mock.patch.object(G, "port_open", return_value=False), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            lcli.cmd_status(P.argparse.Namespace(playthrough=None))
        self.assertIn("thermal:  - (no runtime/thermal.log)", out.getvalue())

    def test_cli_refuses_a_bad_day_cap(self):
        import contextlib
        import io
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(P.main(["run", "--playthrough", "x", "--max-cost-per-day", "0"]), 2)
        self.assertIn("--max-cost-per-day", err.getvalue())


class SeriesRuns(unittest.TestCase):
    """run --until M --runs N: naming, fresh memory and the same profile per run, the per-run reset (saves, the nav
    kill list, knowledge/), failure classes, the clean flag and the runs table."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="bg3ai-series-"))
        self.addCleanup(__import__("shutil").rmtree, self.tmp, True)
        self.saved = {n: getattr(home(n), n) for n in ("RUNTIME", "PLAYTHROUGHS", "SE_BG3AI_DIR", "KNOWLEDGE_DIR",
                                                 "PLAY_DIR", "list_saves", "read_actions_since")}
        self.addCleanup(lambda: [setattr(home(n), n, v) for n, v in self.saved.items()])
        renv.RUNTIME, renv.PLAYTHROUGHS = self.tmp / "runtime", self.tmp / "playthroughs"
        renv.SE_BG3AI_DIR = self.tmp / "se" / "BG3AI"
        renv.PLAY_DIR = self.tmp / "play"
        renv.KNOWLEDGE_DIR = renv.PLAY_DIR / "knowledge"
        rpolicy.read_actions_since = lambda t, path=None: []

    # ---------------------------------------------------------------- naming, memory, profile

    def test_run_names_and_the_next_run(self):
        self.assertEqual([lser.bseries.run_name("naut", k) for k in (1, 2, 3)], ["naut", "naut-r2", "naut-r3"])
        f = self.tmp / "runs.jsonl"
        self.assertEqual(lser.bseries.next_run_index(lser.bseries.series_records(f, "naut")), 1)    # no file yet
        f.write_text("".join(json.dumps(r) + "\n" for r in (
            {"playthrough": "naut", "milestone": "nautiloid_escaped"},                   # a plain --until run
            {"series": "naut", "run": 2, "playthrough": "naut-r2", "outcome": "timeout"},
            {"series": "other", "run": 7, "playthrough": "other-r7"})) + "not json\n")
        recs = lser.bseries.series_records(f, "naut")
        self.assertEqual([r["playthrough"] for r in recs], ["naut", "naut-r2"])
        self.assertEqual(lser.bseries.next_run_index(recs), 3)

    def test_each_run_gets_a_fresh_memory_and_the_series_profile(self):
        prof = {"character": {"brief": "a dwarf cleric", "roleplay": "strict"}, "difficulty": "Explorer"}
        lser.freeze_series("naut", 3, "nautiloid_escaped", prof)
        lser.freeze_series("naut", 3, "nautiloid_escaped", {"changed": True})                # frozen once
        # run 1 adopted the name its agent chose: run 2 still gets the brief without it
        (renv.PLAYTHROUGHS / "naut").mkdir(parents=True)
        (renv.PLAYTHROUGHS / "naut" / "profile.json").write_text(json.dumps(
            {**prof, "character": {**prof["character"], "name": "Mira"}}))
        lser.prepare_series_run("naut", "naut", 1)
        lser.prepare_series_run("naut", "naut-r2", 2)
        for pt in ("naut", "naut-r2"):
            mem = renv.PLAYTHROUGHS / pt / "memory"
            self.assertTrue((mem / "HANDOFF.md").is_file(), pt)
            self.assertTrue((mem / ".git").is_dir(), pt)
            self.assertIn(pt, (mem / "PLAYTHROUGH.md").read_text())
        self.assertEqual(json.loads((renv.PLAYTHROUGHS / "naut-r2" / "profile.json").read_text()), prof)
        self.assertEqual(rpolicy.load_profile(renv.PLAYTHROUGHS / "naut-r2" / "profile.json")["character"]["brief"],
                         "a dwarf cleric")
        # a resumed run keeps what it has
        (renv.PLAYTHROUGHS / "naut-r2" / "memory" / "lessons.md").write_text("kept\n")
        lser.prepare_series_run("naut", "naut-r2", 2)
        self.assertEqual((renv.PLAYTHROUGHS / "naut-r2" / "memory" / "lessons.md").read_text(), "kept\n")

    # ---------------------------------------------------------------- per-run reset

    def _series_runner(self, pt="naut-r2", k=2) -> lpt.Runner:
        return lpt.Runner(rstate.Options(pt, None, 20, "opus", save_owner="Claude", until="nautiloid_escaped",
                                  series="naut", run_index=k))

    def test_a_fresh_run_resets_the_nav_kill_list_saves_and_snapshots_knowledge(self):
        renv.SE_BG3AI_DIR.mkdir(parents=True)
        (renv.SE_BG3AI_DIR / "nav_guard.json").write_text('{"slow":{"TUT_Avernus_C":{"why":"slow"}}}')
        (renv.SE_BG3AI_DIR / "nav_1234.json").write_text('{"levels":{}}')
        (renv.SE_BG3AI_DIR / "other.json").write_text("{}")
        renv.KNOWLEDGE_DIR.mkdir(parents=True)
        (renv.KNOWLEDGE_DIR / "builds.md").write_text("builds\n")
        r = self._series_runner()
        t0 = time.time()
        self.assertEqual(r.begin_run(t0), t0)
        self.assertEqual(sorted(p.name for p in renv.SE_BG3AI_DIR.iterdir()), ["other.json"])   # only nav state moved
        self.assertEqual(sorted(p.name for p in (r.state.dir / "reset").iterdir()), ["nav_1234.json", "nav_guard.json"])
        self.assertEqual(json.loads((r.state.dir / "knowledge_snapshot.json").read_text()), {"builds.md": "builds\n"})
        self.assertEqual(r.state["run"]["reset"], {"nav_files": ["nav_1234.json", "nav_guard.json"],
                                                   "knowledge_files": 1})
        self.assertEqual(r.state["saves_after"], t0)
        # saves older than the run (run 1's, same character name) are never this run's
        rcode.list_saves = lambda: [rpolicy.Save("Claude-1__S009-escaped", "Claude", "S009-escaped", t0 - 60),
                                rpolicy.Save("Claude-2__S001-start", "Claude", "S001-start", t0 + 60)]
        self.assertEqual(r.choose_save().folder, "Claude-2__S001-start")
        self.assertEqual(r.save_env()["BG3AI_SAVES_AFTER"], str(int(t0)))
        rcode.list_saves = lambda: [rpolicy.Save("Claude-1__S009-escaped", "Claude", "S009-escaped", t0 - 60)]
        self.assertIsNone(r.choose_save())                                        # → a new game
        # a restart of the same run keeps its start and doesn't reset again
        (renv.SE_BG3AI_DIR / "nav_guard.json").write_text("{}")
        r2 = self._series_runner()
        self.assertEqual(r2.begin_run(t0 + 500), t0)
        self.assertTrue((renv.SE_BG3AI_DIR / "nav_guard.json").exists())

    def test_no_reset_outside_a_series_or_for_a_playthrough_that_already_played(self):
        renv.SE_BG3AI_DIR.mkdir(parents=True)
        (renv.SE_BG3AI_DIR / "nav_guard.json").write_text("{}")
        r = lpt.Runner(rstate.Options("solo", None, 20, "opus", until="nautiloid_escaped"))
        r.begin_run(time.time())
        r2 = self._series_runner("played", 1)
        r2.state["history"] = [{"kind": "play", "session": "S001"}]
        r2.begin_run(time.time())
        self.assertTrue((renv.SE_BG3AI_DIR / "nav_guard.json").exists())
        self.assertIsNone(r.state["saves_after"])
        self.assertNotIn("BG3AI_SAVES_AFTER", r.save_env())

    def test_agent_knowledge_edits_are_put_back_and_others_reported(self):
        renv.KNOWLEDGE_DIR.mkdir(parents=True)
        (renv.KNOWLEDGE_DIR / "builds.md").write_text("builds\n")
        (renv.KNOWLEDGE_DIR / "tools.md").write_text("tools\n")
        r = self._series_runner()
        t0 = time.time() - 5
        r.begin_run(t0)
        (renv.KNOWLEDGE_DIR / "builds.md").write_text("builds\nthe answer to the nautiloid\n")    # the agent
        (renv.KNOWLEDGE_DIR / "new.md").write_text("cached answers\n")                           # the agent
        (renv.KNOWLEDGE_DIR / "tools.md").write_text("tools v2\n")                               # an operator
        _stream(r.state.dir / "S001.jsonl",
                _use("a", "Edit", {"file_path": str(renv.KNOWLEDGE_DIR / "builds.md")}),
                _use("b", "Write", {"file_path": "knowledge/new.md"}),                     # relative to play/
                _use("c", "Edit", {"file_path": "memory/lessons.md"}))
        k = r.restore_knowledge(t0)
        self.assertEqual(k["agent_edits"], ["builds.md (changed)", "new.md (added)"])
        self.assertEqual(k["other_changes"], ["tools.md (changed)"])
        self.assertEqual((renv.KNOWLEDGE_DIR / "builds.md").read_text(), "builds\n")
        self.assertFalse((renv.KNOWLEDGE_DIR / "new.md").exists())
        self.assertEqual((renv.KNOWLEDGE_DIR / "tools.md").read_text(), "tools v2\n")
        self.assertIn("nautiloid", (r.state.dir / "knowledge_edits" / "builds.md").read_text())   # kept
        self.assertEqual((r.state.dir / "knowledge_edits" / "new.md").read_text(), "cached answers\n")

    def test_knowledge_change_makes_the_run_unclean(self):  # FAIR-2
        renv.KNOWLEDGE_DIR.mkdir(parents=True)
        (renv.KNOWLEDGE_DIR / "tools.md").write_text("tools\n")
        r = self._series_runner()
        t0 = time.time() - 5
        r.begin_run(t0)
        (renv.KNOWLEDGE_DIR / "tools.md").write_text("tools v2\n")   # changed mid-run (by anyone)
        with mock.patch.object(lser, "run_record", lambda *a, **k: {"clean": True, "milestone": "m", "wall_min": 1,
                                                                  "sessions": 1, "cost_usd": 0.0, "crashes": 0,
                                                                  "recoveries": 0, "reloads": 0, "harness_failures": 0,
                                                                  "agent_failures": 0, "unknown_failures": 0,
                                                                  "t": time.time()}), \
                mock.patch.object(lser.model_time, "model_time", lambda *a, **k: None), \
                mock.patch.object(lser.runs, "model_time_field", lambda *a, **k: None):
            r.opt.judge = False
            rec = r.write_run_record(t0)
        self.assertFalse(rec["clean"])
        self.assertTrue(rec["knowledge_changed"])

    def test_play_settings_keep_knowledge_unwritable(self):  # FAIR-2: the agent's only file access is the MCP's
        settings = json.loads((Path(P.__file__).resolve().parents[1] / "play" / ".claude" / "settings.json").read_text())
        deny = settings["permissions"]["deny"]
        for tool in ("Write", "Edit", "MultiEdit", "NotebookEdit", "Bash"):
            self.assertIn(tool, deny)

    # ---------------------------------------------------------------- failure classes

    def kinds(self, fails) -> list[tuple[str, str]]:
        return [(f["kind"], f["class"]) for f in fails]

    def test_runner_events(self):
        fails, ops = lser.runs.event_failures([
            {"t": 1, "kind": "runner_start"},
            {"t": 2, "kind": "fault", "fault": "crash", "session": "S001", "detail": "game process exited"},
            {"t": 3, "kind": "recovered", "fault": "crash"},
            {"t": 4, "kind": "fault", "fault": "hang", "session": "S001"},
            {"t": 5, "kind": "fault", "fault": "agent_hang", "session": "S002", "detail": "no claude output"},
            {"t": 6, "kind": "game_oom", "why": "oom-kill"},
            {"t": 7, "kind": "setup_failed", "error": "the main menu never came up"},
            {"t": 8, "kind": "launch_failed", "error": "crash: signal 11"},
            {"t": 9, "kind": "load_refused", "error": "gate"},
            {"t": 10, "kind": "launch_gave_up", "error": "3 launches"},
            {"t": 11, "kind": "recovery_failed", "error": "x"},
            {"t": 12, "kind": "steam_refused", "why": "online"},
            {"t": 13, "kind": "runner_restart", "files": ["runner/play.py"]},
            {"t": 14, "kind": "runner_stop", "why": "stopped"},
            {"t": 15, "kind": "runner_start"},
            {"t": 16, "kind": "milestone", "milestone": "nautiloid_escaped"}])
        self.assertEqual(self.kinds(fails), [
            ("game_crash", "harness"), ("game_hang", "harness"), ("agent_hang", "unknown"), ("game_oom", "harness"),
            ("setup_failed", "harness"), ("launch_failed", "harness"), ("load_refused", "harness"),
            ("launch_gave_up", "harness"), ("recovery_failed", "harness"), ("steam_refused", "harness")])
        self.assertIn("game process exited", fails[0]["detail"])
        self.assertEqual([o["t"] for o in ops], [13, 14, 15])          # self-restart, operator stop, manual start

    def test_session_history(self):
        fails = lser.runs.history_failures([
            {"session": "S001", "reason": "exited", "started": 0, "ended": 20, "exit": 1},
            {"session": "S002", "reason": "killed", "started": 100, "ended": 900},
            {"session": "S003", "reason": "exited", "started": 1000, "ended": 3000, "early_exit_min": 12.5},
            {"session": "S004", "reason": "hard_stop", "started": 4000, "ended": 8000, "started_at_game_over": True},
            {"session": "S005", "reason": "milestone", "started": 9000, "ended": 9030}])   # quick, but the goal
        self.assertEqual(self.kinds(fails), [("claude_quick_exit", "harness"), ("claude_killed", "unknown"),
                                             ("early_exit", "agent"), ("party_wipe", "agent")])

    def test_stream_failures(self):
        d = self.tmp / "s"
        d.mkdir()
        s = _stream(d / "S001.jsonl",
                    {"type": "system", "subtype": "init", "mcp_servers": [{"name": "bg3", "status": "failed"}]},
                    _use("1", "mcp__bg3__move_to", {"x": 1}), _res("1", "FAILED: no path to the destination"),
                    _use("2", "mcp__bg3__look", {}), _res("2", "Error executing tool look: KeyError 'pos'", True),
                    _use("3", "mcp__bg3__look", {}), _res("3", "Error executing tool look: KeyError 'pos'", True),
                    _use("4", "mcp__bg3__travel_to", {}), _res("4", "MCP error -32001: Request timed out", True),
                    _use("5", "mcp__bg3__combat_turn", {}), _res("5", "2. end_turn: TIMEOUT — still my turn"),
                    _use("6", "Read", {"file_path": "../runner/play.py"}),
                    _res("6", "Blocked by the sandbox/fairness rule: Read outside memory/", True),
                    _use("7", "mcp__bg3__get_status", {}), _res("7", "HP 0/12 ... screen=game_over"),
                    _use("8", "mcp__bg3__look", {}),
                    _res("8", "e3 goblin 7f0c6a1e-1111-2222-3333-444455556666 hp 7"),
                    _use("9", "mcp__bg3__talk_to", {}), _res("9", "<tool_use_error>InputValidationError: npc</tool_use_error>",
                                                            True),
                    {"type": "assistant", "message": {"content": [{"type": "text", "text": "API Error: 529 overloaded"}]}},
                    {"type": "result", "is_error": True, "result": "Claude AI usage limit reached|1760000000"})
        fails = lser.runs.stream_failures(s)
        got = sorted(self.kinds(fails))
        self.assertEqual(got, sorted([
            ("mcp_failed", "harness"), ("claude_api", "harness"), ("claude_api", "harness"),
            ("tool_exception", "harness"), ("tool_timeout", "harness"), ("tool_timeout", "harness"),
            ("fairness_block", "agent"), ("party_wipe", "agent"), ("fairness_leak", "harness")]))
        exc = next(f for f in fails if f["kind"] == "tool_exception")
        self.assertEqual((exc["n"], exc["detail"][:10]), (2, "S001: look"))       # grouped, FAILED: not counted

    def test_failed_tools_and_hand_input_are_unknown(self):
        d = self.tmp / "h"
        d.mkdir()
        s = _stream(d / "S001.jsonl",
                    _use("1", "mcp__bg3__create_character", {}), _res("1", "FAILED: the name box never reported"),
                    _use("2", "mcp__bg3__create_character", {}), _res("2", "FAILED: the name box never reported"),
                    _use("3", "mcp__bg3__click", {"x": 0.5, "y": 0.5}), _res("3", "clicked left at 960,540"),
                    _use("4", "mcp__bg3__move_to", {}), _res("4", "FAILED: destination is beyond remaining movement"),
                    _use("5", "mcp__bg3__combat_turn", {}), _res("5", "1. move: FAILED: no path\n2. attack: done"))
        fails = lser.runs.stream_failures(s)
        self.assertEqual(sorted(self.kinds(fails)), [("hand_input", "unknown"), ("tool_failed", "unknown")])
        tf = next(f for f in fails if f["kind"] == "tool_failed")
        self.assertEqual((tf["n"], tf["detail"][:21]), (2, "S001: create_characte"))
        self.assertTrue(lser.runs.is_clean("reached", fails, []))   # unknown never breaks "clean"

    def test_stalls_reloads_and_the_outcome(self):
        p = _stream(self.tmp / "S001.progress.jsonl", {"type": "stuck", "t": 50, "idle_min": 12.0, "actions": 40},
                    {"type": "stuck", "t": 200, "idle_min": 15.0, "actions": 60}, {"type": "unstuck", "t": 300})
        self.assertEqual(self.kinds(lser.runs.progress_failures([p], 100)), [("stall", "agent")])
        self.assertEqual(self.kinds(lser.runs.reload_failures([{"t": 1, "tool": "load_game", "args": {"save": "latest"}},
                                                            {"t": 2, "tool": "look"}])), [("reload", "agent")])
        harness = [{"class": "harness"}]
        self.assertEqual(self.kinds(lser.runs.outcome_failure("timeout", "", 9, [])), [("timeout", "agent")])
        self.assertEqual(self.kinds(lser.runs.outcome_failure("timeout", "", 9, harness)), [("timeout", "unknown")])
        self.assertEqual(self.kinds(lser.runs.outcome_failure("gave_up", "setup", 9, [])), [("gave_up", "harness")])
        self.assertEqual(self.kinds(lser.runs.outcome_failure("runner_error", "x", 9, [])), [("runner_error", "harness")])
        self.assertEqual(lser.runs.outcome_failure("reached", "", 9, harness), [])

    def test_clean(self):
        agent = [{"class": "agent", "kind": "reload"}, {"class": "unknown", "kind": "agent_hang"}]
        self.assertTrue(lser.runs.is_clean("reached", agent, []))
        self.assertFalse(lser.runs.is_clean("reached", agent + [{"class": "harness", "kind": "game_crash"}], []))
        self.assertFalse(lser.runs.is_clean("reached", [], [{"what": "runner stopped by the operator"}]))
        self.assertFalse(lser.runs.is_clean("timeout", [], []))

    def test_run_record_tags_the_whole_run(self):
        d = renv.RUNTIME / "sessions" / "naut"
        d.mkdir(parents=True)
        t0 = time.time() - 3600
        _stream(d / "S001.jsonl", _use("1", "mcp__bg3__look", {}), _res("1", "Error executing tool look: boom", True),
                {"type": "result", "num_turns": 3, "total_cost_usd": 1.0, "usage": {"output_tokens": 5}})
        _stream(d / "S001.progress.jsonl", {"type": "stuck", "t": t0 + 60, "idle_min": 11, "actions": 30})
        _stream(d / "runner_events.jsonl", {"t": t0 - 10, "kind": "fault", "fault": "crash"},     # before the run
                {"t": t0 + 1, "kind": "runner_start"}, {"t": t0 + 5, "kind": "fault", "fault": "crash"})
        rpolicy.read_actions_since = lambda t, path=None: [{"t": t0 + 30, "tool": "load_game", "args": {"save": "latest"}}]
        hist = [{"kind": "play", "session": "S001", "reason": "milestone", "started": t0 + 2, "ended": t0 + 3000,
                 "minutes": 50}]
        rec = lser.run_record(d, t0, t0 + 3600, {"id": "nautiloid_escaped", "label": "Nautiloid escaped",
                                              "session": "S001"}, "naut", hist)
        self.assertEqual((rec["outcome"], rec["clean"], rec["crashes"], rec["reloads"]), ("reached", False, 1, 1))
        self.assertEqual(sorted(self.kinds(rec["failures"])), [("game_crash", "harness"), ("reload", "agent"),
                                                               ("stall", "agent"), ("tool_exception", "harness")])
        self.assertEqual((rec["harness_failures"], rec["agent_failures"], rec["unknown_failures"]), (2, 2, 0))
        self.assertEqual(rec["operator"], [])
        rec = lser.run_record(d, t0, t0 + 3600, {}, "naut", hist, "timeout", "time cap used up", take_over=True)
        self.assertEqual((rec["outcome"], rec["milestone"], rec["clean"]), ("timeout", None, False))
        self.assertEqual(rec["failures"][-1]["kind"], "timeout")
        self.assertIn("--take-over", rec["operator"][0]["what"])

    # ---------------------------------------------------------------- the table

    def test_runs_table_and_streak(self):
        recs = [{"series": "naut", "run": 1, "playthrough": "naut", "outcome": "reached", "clean": False,
                 "wall_min": 80, "sessions": 2, "cost_usd": 12.5,
                 "failures": [{"kind": "game_crash", "class": "harness"}, {"kind": "reload", "class": "agent"}]},
                {"series": "naut", "run": 2, "playthrough": "naut-r2", "outcome": "reached", "clean": True,
                 "wall_min": 60, "sessions": 1, "cost_usd": 9,
                 "failures": [{"kind": "stall", "class": "agent", "n": 2}]},
                {"series": "naut", "run": 3, "playthrough": "naut-r3", "outcome": "reached", "clean": True,
                 "wall_min": 55, "sessions": 1, "cost_usd": 8, "failures": []}]
        self.assertEqual((lser.brecords.clean_streak(recs), lser.brecords.best_streak(recs)), (2, 2))
        self.assertEqual(lser.brecords.clean_streak(recs + [{"clean": False}]), 0)
        out = lser.brecords.runs_table(recs)
        lines = out.splitlines()
        self.assertTrue(lines[0].startswith("run  playthrough  outcome"))
        self.assertIn("naut-r2", lines[3])
        self.assertIn("stall(a)×2", lines[3])
        self.assertIn("game_crash(h)", lines[2])
        self.assertTrue(out.endswith("3 run(s), 2 clean; 2 clean in a row (latest), best streak 2"))

    def test_runs_command(self):
        d = renv.RUNTIME / "sessions" / "naut"
        d.mkdir(parents=True)
        (d / "runs.jsonl").write_text(json.dumps({"series": "naut", "run": 1, "playthrough": "naut",
                                                  "outcome": "timeout", "why": "time cap used up", "clean": False,
                                                  "wall_min": 240, "sessions": 4, "cost_usd": 30,
                                                  "failures": [{"t": time.time(), "kind": "timeout", "class": "agent",
                                                                "detail": "time cap used up"}],
                                                  "operator": [{"what": "runner stopped by the operator"}]}) + "\n")
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(P.main(["runs", "--playthrough", "naut", "--failures"]), 0)
        out = buf.getvalue()
        self.assertIn("0 clean in a row", out)
        self.assertIn("agent    timeout: time cap used up", out)
        self.assertIn("operator: runner stopped by the operator", out)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(P.main(["runs", "--playthrough", "nothing"]), 1)

    def _plan(self, *argv: str, env: dict | None = None, unset: tuple = (), xwayland: str = "") -> str:
        import contextlib
        import io
        from unittest import mock
        pins = {"game_build": "25605617", "repo": {"commit": "abc", "dirty": False, "diff": None}}
        buf = io.StringIO()
        with mock.patch.object(lcli, "acquire_lock", side_effect=AssertionError("--plan took the runner lock")), \
                mock.patch.object(lpt, "Runner", side_effect=AssertionError("--plan built a Runner")), \
                mock.patch.object(lser, "current_pins", lambda: pins), \
                mock.patch.object(renv.steam_paths, "offline_required", lambda: (True, "marker")), \
                mock.patch.object(renv.steam_paths, "steam_offline", lambda: (False, "last login online")), \
                mock.patch.object(rcode.bg3le_buildinfo, "problems", lambda: []), \
                mock.patch.object(G, "pids_of_game", lambda: []), \
                mock.patch.object(G, "foreign_bg3_pids", lambda: []), \
                mock.patch.dict(renv.os.environ, env or {}), \
                mock.patch.object(lcli.display_env, "pgrep_xwayland", lambda: xwayland), \
                contextlib.redirect_stdout(buf):
            for k in unset:
                renv.os.environ.pop(k, None)
            self.assertEqual(P.main(["run", *argv, "--plan"]), 0)
        return buf.getvalue()

    def test_plan_fills_the_display_env_from_xwayland(self):
        # a multiplexer pane has no DISPLAY/XAUTHORITY: the runner finds them itself (no exports in the start command)
        out = self._plan("--playthrough", "np", unset=("DISPLAY", "XAUTHORITY"),
                         xwayland="9 /usr/bin/Xwayland :1 -rootless -auth /run/user/1000/xauth_Ab -listenfd 4\n")
        self.assertIn("display:        DISPLAY=:1 XAUTHORITY=/run/user/1000/xauth_Ab", out)
        self.assertNotIn("NO XAUTHORITY", out)
        with tempfile.TemporaryDirectory() as home:   # no ~/.Xauthority either
            out = self._plan("--playthrough", "np", unset=("DISPLAY", "XAUTHORITY"), xwayland="", env={"HOME": home})
        self.assertIn("display:        DISPLAY=:1 XAUTHORITY=unset", out)
        self.assertIn("NO XAUTHORITY", out)
        self.assertIn("THE RUNNER WOULD REFUSE TO LAUNCH: ", out)   # no X socket :1, or no auth file
        with tempfile.NamedTemporaryFile() as x:
            out = self._plan("--playthrough", "np", env={"DISPLAY": ":7", "XAUTHORITY": x.name},
                             xwayland="9 /usr/bin/Xwayland :1 -auth /run/user/1000/xauth_Ab\n")
        self.assertIn(f"display:        DISPLAY=:7 XAUTHORITY={x.name}", out)   # inherited values win
        # naut3: an exported value from before Xwayland restarted (the file is gone) is replaced
        out = self._plan("--playthrough", "np", env={"DISPLAY": ":7", "XAUTHORITY": "/run/user/1000/xauth_old"},
                         xwayland="9 /usr/bin/Xwayland :1 -auth /run/user/1000/xauth_Ab\n")
        self.assertIn("display:        DISPLAY=:7 XAUTHORITY=/run/user/1000/xauth_Ab", out)
        out = self._plan("--playthrough", "np", env={"DISPLAY": ":7", "XAUTHORITY": "/run/user/1000/xauth_old"},
                         xwayland="")
        self.assertIn("XAUTHORITY=unset", out)
        self.assertIn("NO XAUTHORITY", out)

    def test_plan_prints_the_freeze_and_the_command_and_writes_nothing(self):
        out = self._plan("--playthrough", "np", "--until", "nautiloid_escaped", "--runs", "3", "--model", "opus",
                         "--effort", "medium", "--session-minutes", "30", "--character-brief", "A watch sergeant",
                         "--roleplay", "strict")
        self.assertIn("series:         np: run 1/3 next (np), 0 recorded", out)
        start = out.index("would be:") + len("would be:")
        frozen = json.loads(out[start:out.index("\n}\n", start) + 3])
        self.assertEqual((frozen["series"], frozen["runs"], frozen["until"]), ("np", 3, "nautiloid_escaped"))
        self.assertEqual(frozen["profile"]["character"], {"brief": "A watch sergeant", "roleplay": "strict"})
        self.assertEqual((frozen["client"], frozen["sandbox"], frozen["judge_model"]),
                         ("claude", "none", lser.judging.DEFAULT_MODEL))
        self.assertEqual(frozen["pins"]["game_build"], "25605617")
        self.assertIn("THE RUNNER WOULD REFUSE TO LAUNCH", out)   # Steam online where offline is required
        cmd = json.loads(out[out.index('{\n "cmd"'):])
        self.assertEqual(cmd["cmd"][cmd["cmd"].index("--model") + 1], "opus")
        self.assertEqual(cmd["cmd"][cmd["cmd"].index("--effort") + 1], "medium")
        self.assertEqual(cmd["env"]["BG3AI_PLAYTHROUGH"], "np")
        self.assertEqual(cmd["env"]["BG3_SESSION_MINUTES"], "30.0")
        written = [x for d in (renv.RUNTIME, renv.PLAYTHROUGHS) if d.exists() for x in d.rglob("*")]
        self.assertEqual(written, [])

    def test_plan_of_a_resume_shows_the_next_session_save_and_hidden_saves(self):
        # claude-run-2 resume check (2026-10-17): --plan printed "S001" and no save for a playthrough at S065
        from unittest import mock
        mem = renv.PLAYTHROUGHS / "cr" / "memory"
        (mem / "sessions").mkdir(parents=True)
        for n in (64, 65):
            (mem / "sessions" / f"S{n:03d}.md").write_text(f"# S{n:03d}\n")
        (mem / "HANDOFF.md").write_text('Save to load: "S065-end"\n')
        d = renv.RUNTIME / "sessions" / "cr"
        d.mkdir(parents=True)
        t = 1_000_000.0
        (d / "runner.json").write_text(json.dumps({"save_owner": "Claude", "history": [
            {"kind": "play", "session": "S065", "started": t, "ended": t + 1800}]}))
        (d / "session-config.json").write_text(json.dumps({"minutes": 30, "effort": "high"}))
        saves = [rpolicy.Save("Claude-1__S065-end", "Claude", "S065-end", t + 1700),
                 rpolicy.Save("Claude-2__S027-end", "Claude", "S027-end", t + 90000),       # another playthrough's
                 rpolicy.Save("Claude-3__S999-savetest", "Claude", "S999-savetest", t + 95000)]
        with mock.patch.object(rcode, "list_saves", lambda: saves):
            out = self._plan("--playthrough", "cr", "--save-owner", "Claude", "--session-minutes", "20")
        self.assertIn("resume:         S066; loads Claude-1__S065-end (HANDOFF names S065-end)", out)
        self.assertIn("saves hidden:   Claude-2__S027-end, Claude-3__S999-savetest", out)
        self.assertIn("session-config: 30 min, model opus, effort high", out)
        cmd = json.loads(out[out.index('{\n "cmd"'):])
        self.assertEqual(cmd["env"]["BG3AI_SESSION"], "S066")
        self.assertEqual(cmd["env"]["BG3_SESSION_MINUTES"], "30.0")
        self.assertEqual(cmd["env"]["BG3AI_SAVES_EXCLUDE"], "Claude-2__S027-end\nClaude-3__S999-savetest")
        self.assertEqual(cmd["cmd"][cmd["cmd"].index("--effort") + 1], "high")

    def test_plan_of_a_frozen_series_shows_the_next_run_and_drift(self):
        d = renv.RUNTIME / "sessions" / "np"
        d.mkdir(parents=True)
        meta = {"series": "np", "runs": 3, "until": "nautiloid_escaped", "profile": {}, "created": 1.0,
                "pins": {"game_build": "1"}, "caps": {}, "judge_model": "j", "client": "claude"}
        (d / "series.json").write_text(json.dumps(meta))
        (d / "runs.jsonl").write_text(json.dumps({"series": "np", "run": 1, "playthrough": "np",
                                                  "outcome": "reached"}) + "\n")
        before = sorted(renv.RUNTIME.rglob("*"))
        out = self._plan("--playthrough", "np", "--until", "nautiloid_escaped", "--runs", "3", "--judge-model", "j",
                         "--allow-judge-change")
        self.assertIn("run 2/3 next (np-r2), 1 recorded", out)
        self.assertIn("exists with every key (frozen", out)
        self.assertIn("REFUSED (--allow-version-drift)", out)
        self.assertEqual(sorted(renv.RUNTIME.rglob("*")), before)

    def test_cli_refuses_runs_without_until(self):
        import contextlib
        import io
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(P.main(["run", "--playthrough", "x", "--runs", "3"]), 2)
            self.assertEqual(P.main(["run", "--playthrough", "x", "--until", "nautiloid_escaped", "--runs", "0"]), 2)
            self.assertEqual(P.main(["run", "--playthrough", "x", "--run-hours", "2"]), 2)
        self.assertIn("--until", err.getvalue())


    # ---------------------------------------------------------------- a half-played run (naut2 run 2, 2026-10-16)

    def _half_run(self, pt="np-r2", k=2, finished=False):
        """naut2 run 2 as the machine crash left it: runner.json run start, a S001 stream, the memory session
        open with edits, saves/<pt>.txt and a save since the start; no runs.jsonl record."""
        from unittest import mock
        d = renv.RUNTIME / "sessions" / "np"
        d.mkdir(parents=True, exist_ok=True)
        (d / "series.json").write_text(json.dumps({"series": "np", "runs": 3, "until": "nautiloid_escaped",
                                                   "profile": {"character": {"brief": "a sergeant"}}}))
        (d / "runs.jsonl").write_text("" if k == 1 else json.dumps(
            {"series": "np", "run": 1, "playthrough": "np", "outcome": "reached"}) + "\n")
        (d / "runner_events.jsonl").write_text('{"kind": "runner_start"}\n')
        lser.prepare_series_run("np", pt, k)
        sd = renv.RUNTIME / "sessions" / pt
        sd.mkdir(parents=True, exist_ok=True)
        hist = [{"kind": "play", "session": "S001", "started": 1001.0}] if finished else []
        (sd / "runner.json").write_text(json.dumps({"history": hist, "runner_saves": [], "saves_after": 1000.0,
                                                    "run": {"started": 1000.0, "series": "np", "index": k}}))
        (sd / "S001.jsonl").write_text("{}\n")
        mem = renv.PLAYTHROUGHS / pt / "memory"
        (mem / ".session.json").write_text(json.dumps({"current": 1, "open": True}))
        (mem / "CHARACTER.md").write_text("# Character: Brannoc Vale\n")
        (renv.RUNTIME / "saves").mkdir(parents=True, exist_ok=True)
        lser._save_record_path(pt).write_text("Brannoc Vale-51612617911__S001-start\n")
        saves = [rpolicy.Save("Brannoc Vale-51612617911__S001-start", "Brannoc Vale", "S001-start", 1100.0),
                 rpolicy.Save("Corran Vell-1__S001-start", "Corran Vell", "S001-start", 900.0)]   # run 1's: older
        rcode.list_saves = lambda: saves
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(rcode, "HANDOVER", self.tmp / "runtime" / "runner-handover.json").start()
        return sd, mem

    def test_half_run_state_reads_what_a_crash_left(self):
        rcode.list_saves = lambda: []
        lser.prepare_series_run("np", "np-r2", 2)
        self.assertIsNone(lser.half_run_state("np-r2"))     # a fresh run: template memory only
        sd, mem = self._half_run()
        h = lser.half_run_state("np-r2")
        self.assertEqual((h["started"], h["finished_sessions"], h["streams"], h["open_session"]),
                         (1000.0, [], ["S001.jsonl"], "S001"))
        self.assertTrue(any("CHARACTER.md" in x for x in h["uncommitted"]))
        self.assertEqual(h["recorded_saves"], ["Brannoc Vale-51612617911__S001-start"])
        self.assertEqual(h["saves_since_start"], ["Brannoc Vale-51612617911__S001-start"])
        self.assertTrue(h["restartable"])
        lines = "\n".join(lser.half_run_lines(h))
        self.assertIn("HALF-PLAYED:    np-r2 started", lines)
        self.assertIn("session S001 open", lines)
        self.assertIn("RESTARTS it (no finished play session)", lines)
        self.assertIn("CONTINUES",
                      "\n".join(lser.half_run_lines({**h, "restartable": False, "finished_sessions": ["S001"]})))
        self.assertIn("RESTARTS it (--restart-run)",
                      "\n".join(lser.half_run_lines({**h, "restartable": False}, forced=True)))
        self.assertNotIn(", \n", lines)
        busy = "\n".join(lser.half_run_lines(h, active=True))
        self.assertIn("a runner holds the lock", busy)
        self.assertNotIn("RESTARTS", busy)

    def test_plan_warns_about_a_half_played_run_and_writes_nothing(self):
        self._half_run()
        before = sorted(x for d in (renv.RUNTIME, renv.PLAYTHROUGHS) for x in d.rglob("*"))
        out = self._plan("--playthrough", "np", "--until", "nautiloid_escaped", "--runs", "3", "--allow-judge-change")
        self.assertIn("run 2/3 next (np-r2), 1 recorded", out)
        self.assertIn("HALF-PLAYED:    np-r2", out)
        self.assertIn("-> run RESTARTS it", out)
        self.assertEqual(sorted(x for d in (renv.RUNTIME, renv.PLAYTHROUGHS) for x in d.rglob("*")), before)
        self.assertTrue(rcode.HANDOVER.parent.is_dir())

    def _restart(self, pt="np-r2", k=2, forced=False):
        logged = []
        from unittest import mock
        with mock.patch.object(renv, "log", logged.append):
            h = lser.restart_half_run(rstate.Options(pt, None, 20, "opus", until="nautiloid_escaped", series="np",
                                             run_index=k), "np", pt, k, forced)
        return h, logged

    def test_a_crashed_run_without_a_finished_session_restarts_from_scratch(self):
        sd, mem = self._half_run()
        h, logged = self._restart()
        self.assertIsNotNone(h)
        self.assertFalse(sd.exists())
        self.assertFalse((renv.PLAYTHROUGHS / "np-r2").exists())
        self.assertFalse(lser._save_record_path("np-r2").exists())
        arch = [x.name for x in (renv.RUNTIME / "sessions").iterdir() if x.name.startswith("np-r2.restarted-")]
        self.assertEqual(len(arch), 1)
        self.assertTrue((renv.RUNTIME / "sessions" / arch[0] / "S001.jsonl").is_file())        # moved, not deleted
        self.assertTrue((renv.PLAYTHROUGHS / arch[0] / "memory" / "CHARACTER.md").is_file())
        self.assertTrue((renv.RUNTIME / "saves" / f"{arch[0]}.txt").is_file())
        self.assertIn("RESTARTED from scratch (its runner died without a handover", logged[-1])
        # then run_series' prepare_series_run gives it a template memory and the series' profile again
        lser.prepare_series_run("np", "np-r2", 2)
        self.assertIsNone(lser.half_run_state("np-r2"))
        self.assertFalse((renv.PLAYTHROUGHS / "np-r2" / "memory" / "CHARACTER.md").exists())
        # and the fresh Runner resets the run (begin_run: saves_after = now, so the crashed saves are old)
        r = self._series_runner("np-r2", 2)
        self.assertIsNone(r.state["run"])
        self.assertGreater(r.begin_run(5000.0), 1100.0)
        self.assertEqual(r.state["saves_after"], 5000.0)

    def test_a_run_with_a_finished_session_continues_unless_forced(self):
        sd, mem = self._half_run(finished=True)
        h, _ = self._restart()
        self.assertIsNone(h)
        self.assertTrue((sd / "runner.json").is_file())
        h, logged = self._restart(forced=True)
        self.assertEqual(h["finished_sessions"], ["S001"])
        self.assertFalse(sd.exists())
        self.assertIn("(--restart-run;", logged[-1])

    def test_never_restarts_its_own_execv_successor(self):
        sd, mem = self._half_run()
        rcode.write_handover({"run_started": 1000.0}, rcode.HANDOVER)   # same pid: this process is the successor
        h, _ = self._restart(forced=True)
        self.assertIsNone(h)
        self.assertTrue((sd / "runner.json").is_file())
        self.assertTrue(rcode.HANDOVER.exists())                      # peeked, not consumed: the Runner reads it

    def test_restarting_run_1_keeps_the_series_files_and_profile(self):
        sd, mem = self._half_run(pt="np", k=1)
        (renv.PLAYTHROUGHS / "np" / "profile.json").write_text(json.dumps(
            {"character": {"brief": "a sergeant", "name": "Brannoc Vale"}}))   # the half run adopted its name
        h, _ = self._restart(pt="np", k=1)
        self.assertIsNotNone(h)
        self.assertEqual(sorted(x.name for x in sd.iterdir()), ["runner_events.jsonl", "runs.jsonl", "series.json"])
        arch = next(x for x in (renv.RUNTIME / "sessions").iterdir() if x.name.startswith("np.restarted-"))
        self.assertTrue((arch / "runner.json").is_file())
        lser.prepare_series_run("np", "np", 1)
        self.assertEqual(json.loads((renv.PLAYTHROUGHS / "np" / "profile.json").read_text()),
                         {"character": {"brief": "a sergeant"}})          # the frozen brief, no half-run name

    def test_restart_run_needs_runs(self):
        import contextlib
        import io
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(P.main(["run", "--playthrough", "x", "--until", "nautiloid_escaped", "--restart-run"]), 2)
        self.assertIn("--restart-run", err.getvalue())

    # ---------------------------------------------------------------- the game at the end (naut2 left it running)

    def _series_with_fake_runners(self, keep_game=False, n=2):
        from unittest import mock
        calls = []

        class FakeRunner:
            def __init__(fr, opt):
                fr.opt, fr.outcome, fr.fatal, fr.stop_requested, fr.profile = opt, None, False, False, {}

            def run(fr):
                path = renv.RUNTIME / "sessions" / "np" / "runs.jsonl"
                path.parent.mkdir(parents=True, exist_ok=True)
                with open(path, "a") as f:
                    f.write(json.dumps({"series": "np", "run": fr.opt.run_index, "playthrough": fr.opt.playthrough,
                                        "outcome": "reached"}) + "\n")
                fr.outcome = "reached"
                return 0

            def end_run_game(fr, when="between runs", after=""):
                calls.append((fr.opt.run_index, when))

            def event(fr, *a, **k):
                pass

        opt = rstate.Options("np", None, 20, "opus", until="nautiloid_escaped", series="np", keep_game=keep_game)
        with mock.patch.object(lpt, "Runner", FakeRunner), \
                mock.patch.object(lser, "prepare_series_run", lambda *a, **k: None), \
                mock.patch.object(lser, "freeze_series", lambda *a, **k: None), \
                mock.patch.object(lser, "gate_series_run", lambda *a, **k: None), \
                mock.patch.object(lser, "current_pins", lambda: {}), \
                mock.patch.object(lser, "restart_half_run", lambda *a, **k: None), \
                mock.patch.object(renv, "log"):
            lser.run_series(opt, n, {"runner": None, "stop": False})
        return calls

    def test_a_series_stops_its_last_game_unless_keep_game(self):
        self.assertEqual(self._series_with_fake_runners(), [(1, "between runs"), (2, "series end")])
        __import__("shutil").rmtree(renv.RUNTIME / "sessions" / "np")
        self.assertEqual(self._series_with_fake_runners(keep_game=True), [(1, "between runs")])

    def test_stop_game_at_end_only_for_until_runs(self):
        from unittest import mock
        r = mock.Mock()
        until = rstate.Options("x", None, 20, "opus", until="nautiloid_escaped")
        self.assertTrue(lser.stop_game_at_end(r, until, "run end"))
        r.end_run_game.assert_called_once_with("run end", "--keep-game leaves it up")
        r.reset_mock()
        for opt in (rstate.Options("x", None, 20, "opus"),                                   # continuous play
                    rchecks.replace(until, keep_game=True), rchecks.replace(until, dry_run=True)):
            self.assertFalse(lser.stop_game_at_end(r, opt, "run end"))
        r.end_run_game.assert_not_called()
        self.assertFalse(lser.stop_game_at_end(None, until, "run end"))

    def test_end_run_game_names_when(self):
        from unittest import mock
        r = self._series_runner()
        logged = []
        with mock.patch.object(G, "port_open", lambda: True), mock.patch.object(r, "owns_game", lambda: True), \
                mock.patch.object(r, "kill_game") as kill, mock.patch.object(renv, "log", logged.append):
            r.end_run_game("series end", "--keep-game leaves it up")
        kill.assert_called_once()
        self.assertEqual(logged, ["series end: game stopped; --keep-game leaves it up"])
        with mock.patch.object(G, "port_open", lambda: True), mock.patch.object(r, "owns_game", lambda: False), \
                mock.patch.object(r, "kill_game") as kill, mock.patch.object(renv, "log", logged.append):
            r.end_run_game("run end")
        kill.assert_not_called()                                   # not this runner's game: never killed
        self.assertEqual(logged[-1], "run end: the game up is not one this runner launched; left running")

    # ---------------------------------------------------------------- fix-notes after the --until stop

    HANDOFF_ONLY = ("ERROR  handoff     HANDOFF.md was written by S000, not by the current session S001. Rewrite it\n"
                    "ERROR  handoff     HANDOFF.md is missing the required '## Next steps' line/section\n")
    REAL = HANDOFF_ONLY + "ERROR  objectives  objectives.md: o3 has no status\n"

    def test_until_stop_handoff_only(self):
        self.assertTrue(rcode.until_stop_handoff_only("milestone", True, self.HANDOFF_ONLY))
        self.assertFalse(rcode.until_stop_handoff_only("milestone", True, self.REAL))          # a real notes problem
        self.assertFalse(rcode.until_stop_handoff_only("milestone", True, "Traceback: boom"))  # session.py failed
        self.assertFalse(rcode.until_stop_handoff_only("exited", True, self.HANDOFF_ONLY))     # it had its wrap-up time
        self.assertFalse(rcode.until_stop_handoff_only("milestone", False, self.HANDOFF_ONLY))

    def _close(self, report: str, reason="milestone"):
        from unittest import mock
        r = self._series_runner()
        fixes, events = [], []
        res = mock.Mock(sid="S001", reason=reason, claude_session="u", started=0.0, ended=500.0)
        with mock.patch.object(r, "memory_state", return_value={"open": True}), \
                mock.patch.object(r, "session_py", return_value=mock.Mock(returncode=1, stdout=report)), \
                mock.patch.object(r, "run_claude", side_effect=lambda *a, **k: fixes.append(a)), \
                mock.patch.object(r, "event", side_effect=lambda kind, **f: events.append(kind)), \
                mock.patch.object(renv, "log"):
            r.close_memory_session(res)
        return fixes, events

    def test_no_fix_notes_session_after_the_milestone_when_only_the_handoff_fails(self):
        fixes, events = self._close(self.HANDOFF_ONLY)
        self.assertEqual(fixes, [])
        self.assertIn("fix_notes_skipped", events)
        fixes, events = self._close(self.REAL)
        self.assertEqual(len(fixes), 1)                 # a real lint failure still gets its fix pass
        self.assertEqual(fixes[0][0], "S001")
        fixes, _ = self._close(self.HANDOFF_ONLY, reason="exited")
        self.assertEqual(len(fixes), 1)                 # not the --until stop: the agent skipped its wrap-up


if __name__ == "__main__":
    unittest.main()

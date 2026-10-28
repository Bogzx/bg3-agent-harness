#!/usr/bin/env python3
"""runner/play.py's saves: the handoff save and its identity, the agent's own saves, relaunch before a session, the
save gates and the session's ensured save (moved from test_play.py). Never launches claude or the game.

    python3 runner/test_play_saves.py
"""
from __future__ import annotations

import contextlib
import dataclasses
import io
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import testenv  # noqa: F401, I001  (first: the game's profile folders are a temp dir before play)
# RUN-9: play.RUNTIME is read at import; a test must never append to the real runtime/runner.log (the phantom
# "0.01 s after SIGTERM" kill bursts the orchestrator chased). Tests that need a runtime set their own.
os.environ.setdefault("BG3AI_RUNTIME", tempfile.mkdtemp(prefix="bg3ai-test-runtime-"))
os.environ.setdefault("BG3AI_KNOWLEDGE_LOCK", "0")   # never chmod the real play/knowledge (Knowledge tests patch it)
import play as P  # noqa: I001  (play puts packages/harness/src on sys.path)
from bg3harness.game import saves as gsaves
from bg3harness.runner import agent_side as ragent
from bg3harness.runner import codewatch as rcode
from bg3harness.runner import env as renv
from bg3harness.runner import game_side as rgame
from bg3harness.runner import policy as rpolicy
from bg3harness.runner import state as rstate
import legacy_playthrough as lpt
from bg3play.runner import prompts as pprompts
from bg3harness.game import io as G
from bg3ai import lspk

G_FUNCS = {n for n, v in vars(G).items() if callable(v) and getattr(v, "__module__", None) == G.__name__
           and not isinstance(v, type)}


def sv(folder: str, t: float, saved: float | None = None) -> G.Save:
    """A save as controller.list_saves gives it: its .lsv mtime and meta.lsf SaveTime (`saved`, default the same)."""
    s = G.parse_save_folder(folder, t)
    assert s is not None
    return dataclasses.replace(s, saved=t if saved is None else saved)


def home(name: str):
    """The module a patched name lives in: the game's I/O functions moved to bg3harness.game.io."""
    return G if name in G_FUNCS else P



class HandoffAndSaves(unittest.TestCase):
    def test_load_menu_args(self):
        t = time.mktime((2026, 10, 9, 22, 39, 30, 0, 0, -1))
        saves = [sv("Claude-1__S001-start", t - 400),
                 sv("Claude-2__S001-imps-done", t),
                 sv("Claude-3__S001-end", t + 360),
                 sv("Tav-4__S002-end", t + 900)]
        a = G.load_menu_args(saves[1], saves)
        self.assertEqual((a["action"], a["name"], a["owner"], a["index"]), ("load", "Claude-2__S001-imps-done", "Claude", 2))
        self.assertEqual(a["date"], {"y": 2026, "m": 10, "d": 9, "H": 22, "M": 39})
        # Steam's local cloud restored S001-start and imps-done at a launch (mtime = the launch, SaveTime unchanged):
        # the game still lists S001-end first; by mtime the runner clicked row 1 for imps-done (S001-end) and checked
        # the restored copy's mtime as its date
        saves[0] = sv("Claude-1__S001-start", t + 5000, saved=t - 400)
        saves[1] = sv("Claude-2__S001-imps-done", t + 5001, saved=t)
        a = G.load_menu_args(saves[1], saves)
        self.assertEqual((a["index"], a["date"]["H"], a["date"]["M"]), (2, 22, 39))
        self.assertNotIn("index", G.load_menu_args(dataclasses.replace(saves[1], saved=None), saves))

    def test_parse_handoff(self):
        self.assertEqual(rgame.parse_handoff_save('# Handoff\nSave to load: "S042-end" — party at the well\n'), "S042-end")
        self.assertEqual(rgame.parse_handoff_save("Save to load: S042-end — x"), "S042-end")
        self.assertEqual(rgame.parse_handoff_save("Save to load: `S007-pre-boss` (out of combat)"), "S007-pre-boss")
        self.assertIsNone(rgame.parse_handoff_save("Save to load: (none — new game; start from the main menu)"))
        self.assertIsNone(rgame.parse_handoff_save("no such line"))
        # the template's example inside an HTML comment must be ignored
        self.assertIsNone(rgame.parse_handoff_save('<!--\nSave to load: "S042-end"\n-->\nSave to load: (none)\n'))

    def test_parse_folder(self):
        s = G.parse_save_folder("pibooool-10812619937__QuickSave_42", 5.0)
        self.assertEqual((s.owner, s.label, s.mtime), ("pibooool", "QuickSave_42", 5.0))
        s = G.parse_save_folder("Tav-Name-52012618851__S003-pre-boss")
        self.assertEqual((s.owner, s.label), ("Tav-Name", "S003-pre-boss"))
        self.assertIsNone(G.parse_save_folder("garbage"))

    def test_is_ai_label(self):
        self.assertTrue(rgame.is_ai_label("S001-start", ""))
        self.assertFalse(rgame.is_ai_label("QuickSave_3", ""))
        self.assertFalse(rgame.is_ai_label("Dank Crypt - 3h 15m", ""))
        self.assertTrue(rgame.is_ai_label("pt1-S010-end", "pt1-"))
        self.assertFalse(rgame.is_ai_label("S010-end", "pt1-"))

    def _saves(self):
        f = sv
        return [
            f("pibooool-1__Dank Crypt - 3h 15m", 900),      # the user's own playthrough
            f("pibooool-2__QuickSave_42", 950),
            f("Ilse-3__S001-start", 100),
            f("Ilse-4__S001-end", 200),
            f("Ilse-5__QuickSave_1", 300),                 # a runner quicksave
            f("Other-6__S001-end", 400),                   # another playthrough, same label
        ]

    def test_named_saves_with_a_folder_prefix(self):
        # BG3AI_SAVE_PREFIX=Claude- (docs/SETUP.md, the MCP server) names the folder, and the agent's
        # save_game labels are plain "S001-end": the session's named save must count (S001 quicksaved over it).
        saves = [sv("Claude-10__S001-end", 200), sv("Claude-11__QuickSave_0", 300),
                 sv("Tav-12__S001-end", 250)]
        self.assertEqual([s.folder for s in rgame.saves_since(saves, 100, "Claude", "Claude-")], ["Claude-10__S001-end"])
        self.assertEqual(rgame.pick_save(saves, None, "Claude", set(), "Claude-").folder, "Claude-10__S001-end")
        # a label prefix still works, and a foreign character never matches
        self.assertTrue(rpolicy.is_ai_save(G.parse_save_folder("Ilse-1__pt1-S010-end"), "pt1-"))
        self.assertFalse(rpolicy.is_ai_save(G.parse_save_folder("Ilse-1__S010-end"), "pt1-"))
        self.assertFalse(rpolicy.is_ai_save(G.parse_save_folder("Tav-12__S001-end"), "Claude-"))

    def test_pick_skips_worker_saves(self):
        # 2026-10-10 live-test break: workers saved LIVETEST-* after S027-start; the playthrough's save stays the pick
        saves = [sv("Claude-48__S027-start", 100), sv("Claude-14__LIVETEST-act", 200),
                 sv("Claude-12__LIVETEST-vis-open", 300), sv("Claude-13__AutoSave_42", 250)]
        self.assertEqual(rgame.pick_save(saves, "S027-start", "Claude", set(), "Claude-").folder, "Claude-48__S027-start")

    def test_pick_only_saves_written_while_this_playthrough_played(self):
        # 2026-10-17: claude-run-2's newest save is S065-end (10-11 12:03), but replay-hoh saved S027-end and
        # S028-lastlight-prep as "Claude" on 10-15 and a worker saved S999-savetest on 10-13: without the play
        # windows the resume would have loaded Act 2's S028-lastlight-prep
        f = sv
        saves = [f("Claude-1__S065-pre-orin", 1000.0), f("Claude-2__S065-end", 1500.0),
                 f("Claude-3__S999-savetest", 5000.0), f("Claude-4__S027-end", 9000.0),
                 f("Claude-5__S028-lastlight-prep", 9500.0), f("Claude-6__QuickSave_9", 9600.0)]
        windows = [(100.0, 600.0), (900.0, 1450.0)]
        self.assertEqual(rgame.pick_save(saves, None, "Claude", set(), "Claude-").label, "S028-lastlight-prep")
        self.assertEqual(rgame.pick_save(saves, None, "Claude", set(), "Claude-", windows).label, "S065-end")  # +50 s slack
        self.assertEqual(rgame.pick_save(saves, "S027-end", "Claude", set(), "Claude-", windows).label, "S065-end")
        # the runner's / agent's own quicksaves count by folder, whenever written; no windows = a new playthrough
        self.assertEqual(rgame.pick_save(saves, None, "Claude", {"Claude-6__QuickSave_9"}, "Claude-", windows).label,
                         "QuickSave_9")
        self.assertEqual(rgame.pick_save(saves, None, "Claude", set(), "Claude-", []).label, "S028-lastlight-prep")
        self.assertFalse(gsaves.in_windows(1571.0, windows))
        self.assertTrue(gsaves.in_windows(1570.0, windows))
        # a save older than the playthrough's first window is judged as before (a crash in the first session)
        older = [f("Claude-0__S001-start", -500.0)] + saves[3:4]
        self.assertEqual(rgame.pick_save(older, None, "Claude", set(), "Claude-", windows).label, "S001-start")
        self.assertFalse(gsaves.played_elsewhere(-500.0, windows))
        self.assertTrue(gsaves.played_elsewhere(750.0, windows))   # 600 + 120 s slack < 750 < 900 - 120

    def test_pick_goes_by_the_saves_own_time_never_its_mtime(self):
        # bench B3 (PR #14): Steam's local cloud restores a moved-aside save at a launch with mtime = the launch; only
        # its meta.lsf SaveTime still says when it was made. S002 launched at 5000 and Steam restored two copies.
        windows = [(900.0, 1450.0), (5000.0, 6000.0)]
        own = sv("Claude-2__S001-end", 1400.0)
        restored = sv("Claude-9__S027-end", 5010.0, saved=3000.0)   # another playthrough's (same name)
        older = sv("Claude-8__S001-start", 5010.0, saved=950.0)       # this playthrough's older save
        unread = dataclasses.replace(sv("Claude-7__S002-x", 5500.0), saved=None)   # meta.lsf unreadable
        saves = [own, restored, older, unread]
        SKIPPED = "; skipped, SaveTime unreadable: Claude-7__S002-x"  # named, never silently dropped  # noqa: N806
        self.assertEqual(rgame.pick_save(saves, None, "Claude", set(), "Claude-", windows), own)
        self.assertEqual(rgame.pick_save_why(saves, "S001-end", "Claude", set(), "Claude-", windows, 1, {1: 1450.0}),
                         (own, 'HANDOFF\'s "Save to load", also the newest save' + SKIPPED))
        self.assertEqual(rgame.pick_save_why(saves, "S001-start", "Claude", set(), "Claude-", windows, 1, {1: 1450.0}),
                         (older, 'HANDOFF\'s "Save to load" (S001\'s choice), not the newer "S001-end" from the same '
                                 'session' + SKIPPED))
        self.assertIsNone(rgame.pick_save([unread], None, "Claude", {unread.folder}, "Claude-", windows))

    def test_handoff_save_is_the_agents_choice(self):
        # L8: S076 started from S075-fight (newest, mid-fight try 6) while S075's HANDOFF named S075-end; S076's
        # HANDOFF named S075-end again ("S076-end = try 7 mid-fight, losing: don't continue it")
        f = sv
        ends = {75: 2000.0, 76: 3000.0}
        s75 = [f("Claude-1__S074-end", 900.0), f("Claude-2__S075-end", 1500.0), f("Claude-3__S075-fight", 1900.0)]
        save, why = rgame.pick_save_why(s75, "S075-end", "Claude", set(), "Claude-", None, 75, ends)
        self.assertEqual(save.label, "S075-end")
        self.assertIn("S075's choice", why)
        self.assertIn('not the newer "S075-fight"', why)
        s76 = s75 + [f("Claude-4__S076-end", 2900.0)]
        self.assertEqual(rgame.pick_save_why(s76, "S075-end", "Claude", set(), "Claude-", None, 76, ends)[0].label,
                         "S075-end")   # an older save on purpose
        # HANDOFF from S075, but S076 played on and saved (crashed before its wrap-up): the newest
        save, why = rgame.pick_save_why(s76, "S075-end", "Claude", set(), "Claude-", None, 75, ends)
        self.assertEqual(save.label, "S076-end")
        self.assertIn("after S075, which wrote HANDOFF, ended", why)
        # S077 crashed without saving: S076's HANDOFF still holds
        self.assertEqual(rgame.pick_save_why(s76, "S075-end", "Claude", set(), "Claude-", None, 76,
                                         {**ends, 77: 4000.0})[0].label, "S075-end")
        # a runner quicksave after the session ended is later play too
        q = s76 + [f("Claude-5__QuickSave_3", 3010.0)]
        self.assertEqual(rgame.pick_save_why(q, "S075-end", "Claude", {"Claude-5__QuickSave_3"}, "Claude-", None, 76,
                                         ends)[0].label, "QuickSave_3")
        # unknown HANDOFF session, no heading, no such save, no label: the newest, each with its reason
        for args, needle in [((76, {}), "S076 never played here"), ((None, ends), "no '# Handoff Sxxx' heading")]:
            save, why = rgame.pick_save_why(s76, "S075-end", "Claude", set(), "Claude-", None, *args)
            self.assertEqual((save.label, needle in why), ("S076-end", True))
        save, why = rgame.pick_save_why(s76, "S070-end", "Claude", set(), "Claude-", None, 76, ends)
        self.assertEqual((save.label, "no such save" in why), ("S076-end", True))
        save, why = rgame.pick_save_why(s76, None, "Claude", set(), "Claude-", None, 76, ends)
        self.assertEqual((save.label, why), ("S076-end", "the newest save (HANDOFF names no save)"))
        self.assertEqual(rgame.pick_save_why(s76, "S076-end", "Claude", set(), "Claude-", None, 76, ends),
                         (s76[-1], "HANDOFF's \"Save to load\", also the newest save"))
        self.assertEqual(rgame.pick_save_why([], "S075-end", "Claude", set(), "Claude-"), (None, "no save of this playthrough"))
        # pick_save stays the newest (status, crash fallbacks)
        self.assertEqual(rgame.pick_save(s76, "S075-end", "Claude", set(), "Claude-").label, "S076-end")

    def test_handoff_session_and_session_ends(self):
        self.assertEqual(lpt.parse_handoff_session("# Handoff S076 → S077  (2026-10-18 00:45)\nSave to load: x\n"), 76)
        self.assertIsNone(lpt.parse_handoff_session("<!--\n# Handoff S001 → S002\n-->\nSave to load: x\n"))
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "S076.turns.jsonl").write_text('{"t": 100}\n{"t": 250}\n')   # crashed, never recorded
            (Path(d) / "S075.turns.jsonl").write_text('{"t": 10}\n{"t": 40}\n')
            hist = [{"kind": "play", "session": "S075", "started": 5.0, "ended": 60.0},
                    {"kind": "keeper", "session": "S075", "ended": 90.0}]
            self.assertEqual(rgame.session_ends(hist, Path(d)), {75: 60.0, 76: 250.0})
        self.assertEqual(rgame.session_ends(None, None), {})

    def test_choose_save_follows_handoff_and_tells_the_kickoff(self):
        f = sv
        saves = [f("Claude-2__S075-end", 1500.0), f("Claude-3__S075-fight", 1900.0)]
        with tempfile.TemporaryDirectory() as d:
            mem = Path(d) / "memory"
            mem.mkdir()
            (mem / "HANDOFF.md").write_text('# Handoff S075 → S076\nSave to load: "S075-end" (pre-fight)\n')
            r = lpt.Runner.__new__(lpt.Runner)
            r.mem, r.opt = mem, rstate.Options("pt", None, 20, "opus", save_owner="Claude", dry_run=True)
            data = {"save_owner": "Claude", "history": [{"kind": "play", "session": "S075", "started": 1000.0,
                                                         "ended": 2000.0}]}
            r.state = type("S", (), {"data": data, "dir": Path(d), "__getitem__": lambda s, k: s.data.get(k),
                                     "__setitem__": lambda s, k, v: s.data.__setitem__(k, v)})()
            with mock.patch.object(rcode, "list_saves", lambda: saves), mock.patch.object(renv, "log"):
                self.assertEqual(r.choose_save().label, "S075-end")
        self.assertIn("S075's choice", r.loaded_why("S075-end"))
        self.assertEqual(r.loaded_why("S075-fight"), "")   # a fallback loaded another save: no reason
        self.assertEqual(r.loaded_why(None), "")
        k = pprompts.build_kickoff("S076", "pt", "S075-end", [], save_why=r.loaded_why("S075-end"))
        self.assertIn('already loaded save "S075-end" (HANDOFF\'s "Save to load" (S075\'s choice), not the newer '
                      '"S075-fight" from the same session). Don\'t reload it', k)

    def test_foreign_saves_for_the_mcp_server(self):
        f = sv
        saves = [f("Claude-2__S065-end", 1500.0), f("Claude-3__S999-savetest", 5000.0),
                 f("Claude-5__S028-lastlight-prep", 9500.0), f("Claude-6__QuickSave_9", 9600.0),
                 f("Claude-7__LIVETEST-x", 9700.0), f("Tav-8__S001-end", 9800.0)]
        windows = [(900.0, 1450.0)]
        self.assertEqual(ragent.foreign_saves(saves, "Claude", set(), windows, "Claude-"),
                         ["Claude-3__S999-savetest", "Claude-5__S028-lastlight-prep"])   # not quicksaves/LIVETEST/Tav
        self.assertEqual(ragent.foreign_saves(saves, "Claude", {"Claude-3__S999-savetest"}, windows, "Claude-"),
                         ["Claude-5__S028-lastlight-prep"])
        self.assertEqual(ragent.foreign_saves(saves, "Claude", set(), [], "Claude-"), [])     # a new playthrough
        # bench B3: Steam restored another playthrough's save at this session's launch (9900): mtime = the launch,
        # inside the running window; its SaveTime says another playthrough wrote it. Unread times are hidden too.
        restored = [f("Claude-9__S027-end", 9900.0, saved=3000.0), f("Claude-10__S066-start", 9950.0),
                    dataclasses.replace(f("Claude-11__S066-x", 9990.0), saved=None)]
        self.assertEqual(ragent.foreign_saves(restored, "Claude", set(), [(900.0, 1450.0), (9900.0, 10000.0)],
                                              "Claude-"), ["Claude-11__S066-x", "Claude-9__S027-end"])
        with tempfile.TemporaryDirectory() as d:
            r = lpt.Runner(rstate.Options("solo", None, 20, "opus", save_owner="Claude"))
            r.state.dir = Path(d)
            r.state["history"] = [{"kind": "play", "session": "S065", "started": 900.0, "ended": 1450.0}]
            with mock.patch.object(rcode, "list_saves", lambda: saves):
                self.assertEqual(r.save_env()["BG3AI_SAVES_EXCLUDE"],
                                 "Claude-3__S999-savetest\nClaude-5__S028-lastlight-prep")
                r.state["history"] = []
                self.assertNotIn("BG3AI_SAVES_EXCLUDE", r.save_env())

    def test_play_windows_include_sessions_the_runner_never_recorded(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            (d / "S001.turns.jsonl").write_text('{"type":"assistant","t":100.0}\n{"type":"use","t":150.5}\n'
                                                 '{"type":"result","t":290.0}\n')
            (d / "S002.turns.jsonl").write_text('not json\n{"type":"use","t":400.0}\n')   # crashed: one event
            (d / "S003.turns.jsonl").write_text("")
            hist = [{"kind": "play", "session": "S001", "started": 90.0, "ended": 300.0},
                    {"kind": "play", "session": "S000"}]                                       # no times: skipped
            self.assertEqual(rgame.play_windows(hist, d), [(90.0, 300.0), (100.0, 290.0), (400.0, 400.0)])
            self.assertEqual(rgame.play_windows(None, None), [])

    def test_foreign_use_since(self):
        best = sv("Claude-48__S027-start", 100)
        mine = [best, sv("Claude-13__AutoSave_42", 150), sv("Claude-9__S026-end", 90)]
        f = lambda saves=mine, acts=(), started=None, rs=set(): rpolicy.foreign_use_since(best, saves, list(acts), started, rs, "Claude-")
        self.assertIsNone(f())
        self.assertIn("LIVETEST-setup", f(mine + [sv("Claude-21__LIVETEST-setup", 120)]))
        self.assertIsNone(f(mine + [sv("Claude-21__LIVETEST-setup", 80)]))   # older: not a sign
        # Steam's local cloud restored the older one at a launch: mtime = the launch, its SaveTime still 80
        self.assertIsNone(f(mine + [sv("Claude-21__LIVETEST-setup", 500, saved=80)]))
        self.assertIsNone(f(mine + [sv("Claude-5__QuickSave_1", 130)], rs={"Claude-5__QuickSave_1"}))
        load = {"t": 130, "tool": "load_game", "args": {"save": "Claude-9__S026-end"}, "result": "load_game: done — loaded=S026-end"}
        self.assertIn("S026-end", f(acts=[load]))                  # a worker loaded an older playthrough save
        self.assertIsNone(f(acts=[dict(load, t=90)]))              # before our save: irrelevant
        self.assertIsNone(f(acts=[dict(load, result="load_game: done — loaded=S027-start")]))
        self.assertIn("relaunched", f(started=110))
        self.assertIsNone(f(started=95))
        # our save itself restored by Steam at a launch (mtime 500): the game started at 110 still came after it
        restored = dataclasses.replace(best, mtime=500.0)
        self.assertIn("relaunched", rpolicy.foreign_use_since(restored, mine, [], 110, set(), "Claude-"))

    def test_read_actions_since(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "actions.jsonl"
            p.write_text('{"t": 5, "tool": "a"}\nnot json\n{"t": 15, "tool": "load_game"}\n')
            self.assertEqual([r["tool"] for r in rpolicy.read_actions_since(10, p)], ["load_game"])
            self.assertEqual(rpolicy.read_actions_since(10, Path(d) / "missing"), [])

    def test_a_save_whose_savetime_cant_be_read_is_named_never_silently_dropped(self):
        start = G.parse_save_folder("Tav-14221265938__SCN-fight_act2_balthazar", 100.0)  # saved None: meta unreadable
        mine, other = sv("Tav-2__S001-end", 200.0), G.parse_save_folder("Karlach-3__S004-end", 300.0)
        save, why = rgame.pick_save_why([start, other], None, "Tav", {start.folder})
        self.assertIsNone(save)
        self.assertEqual(why, "no save of this playthrough; skipped, SaveTime unreadable: " + start.folder)
        save, why = rgame.pick_save_why([start, mine, other], None, "Tav", {start.folder})
        self.assertEqual(save, mine)
        self.assertTrue(why.endswith("; skipped, SaveTime unreadable: " + start.folder), why)  # not Karlach's
        self.assertNotIn("skipped", rgame.pick_save_why([mine], None, "Tav", set())[1])

    def test_an_unreadable_save_of_another_playthrough_is_not_named(self):
        # app review of PR #30: a same-owner save written while another playthrough played (outside this one's play
        # windows) is no candidate of this choice, readable or not; with no SaveTime the window goes by its mtime
        windows = [(1000.0, 2000.0), (5000.0, 6000.0)]
        ours = G.parse_save_folder("Tav-1__S002-end", 1900.0)
        theirs = G.parse_save_folder("Tav-2__S007-end", 3000.0)
        save, why = rgame.pick_save_why([ours, theirs], None, "Tav", set(), windows=windows)
        self.assertIsNone(save)
        self.assertEqual(why, "no save of this playthrough; skipped, SaveTime unreadable: " + ours.folder)

    def test_play_refuses_a_python_that_cant_read_a_savetime(self):
        err = io.StringIO()
        with mock.patch.object(lspk, "zstd", None), contextlib.redirect_stderr(err), \
                mock.patch.object(P.lcli, "cmd_run") as run, mock.patch.object(P.lcli, "cmd_bench") as bench:
            self.assertEqual(P.main(["run", "--playthrough", "x"]), 2)
            self.assertEqual(P.main(["bench", "run", "--matrix", "m.toml"]), 2)
        run.assert_not_called()
        bench.assert_not_called()
        self.assertIn("runner/play.py refused: Python ", err.getvalue())
        self.assertIn("needs Python 3.14+", err.getvalue())
        self.assertIn("uv run python runner/play.py run --playthrough x\n", err.getvalue())
        self.assertIn("uv run python runner/play.py bench run --matrix m.toml\n", err.getvalue())

    def test_play_commands_that_read_no_savetime_run_under_any_python(self):
        # app review of PR #30: `python3 runner/play.py stop` must still stop a runner started with uv run
        names = ("cmd_stop", "cmd_runs", "cmd_judge", "cmd_bench")
        with mock.patch.object(lspk, "zstd", None), contextlib.ExitStack() as st:
            cmds = {n: st.enter_context(mock.patch.object(P.lcli, n, return_value=0)) for n in names}
            self.assertEqual(P.main(["stop"]), 0)
            self.assertEqual(P.main(["runs", "--playthrough", "x"]), 0)
            self.assertEqual(P.main(["judge", "--playthrough", "x"]), 0)
            self.assertEqual(P.main(["bench", "status", "--matrix", "m.toml"]), 0)
            with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as e:
                P.main(["run", "--help"])
            self.assertEqual(e.exception.code, 0)
        self.assertTrue(all(c.call_count == 1 for c in cmds.values()), cmds)
        cmds["cmd_bench"].assert_called_once_with(["status", "--matrix", "m.toml"])

    def test_pick_never_takes_foreign_saves(self):
        saves = self._saves()
        self.assertIsNone(rgame.pick_save(saves, None, None, set(), ""))
        best = rgame.pick_save(saves, None, "Ilse", set(), "")
        self.assertEqual(best.folder, "Ilse-4__S001-end")
        self.assertNotIn("pibooool", best.folder)

    def test_pick_includes_runner_quicksave(self):
        best = rgame.pick_save(self._saves(), "S001-end", "Ilse", {"Ilse-5__QuickSave_1"}, "")
        self.assertEqual(best.folder, "Ilse-5__QuickSave_1")

    def test_pick_by_handoff_before_owner_known(self):
        best = rgame.pick_save(self._saves(), "S001-start", None, set(), "")
        self.assertEqual(best.folder, "Ilse-3__S001-start")
        # with an owner, the other playthrough's identical label is excluded
        best = rgame.pick_save(self._saves(), "S001-end", "Ilse", set(), "")
        self.assertEqual(best.folder, "Ilse-4__S001-end")

    def test_saves_since(self):
        new = rgame.saves_since(self._saves(), 150, "Ilse", "")
        self.assertEqual([s.folder for s in new], ["Ilse-4__S001-end"])
        self.assertEqual(rgame.saves_since(self._saves(), 1000, None, ""), [])
        # bench B3: Steam restored another run's save at this session's launch (1000): mtime = the launch, so it
        # passed as the session's save and, with no owner known yet, named the owner
        restored = [sv("Vessa-7__S001-end", 1005.0, saved=400.0), sv("Ilse-8__S002-start", 1200.0),
                    dataclasses.replace(sv("Ilse-9__S002-x", 1300.0), saved=None)]
        self.assertEqual([s.folder for s in rgame.saves_since(restored, 1000, None, "")], ["Ilse-8__S002-start"])


class AgentSaves(unittest.TestCase):
    """RUN-6: the agent's quicksaves (actions.jsonl save_game.bg records) are this playthrough's saves."""

    def test_agent_quicksaves(self):
        acts = [{"tool": "save_game.bg", "ok": True, "folder": "Claude-52151269953__QuickSave_7"},
                {"tool": "save_game.bg", "ok": True, "folder": "Claude-52151269953__S028-prep"},   # named: by label
                {"tool": "save_game.bg", "ok": False, "folder": None},
                {"tool": "save_game.bg", "ok": True, "folder": "Tav-1__QuickSave_3"},               # another owner
                {"tool": "save_game", "result": "QuickSave_9"},
                {"tool": "save_game.bg", "ok": True, "folder": "Claude-52151269953__QuickSave_7"}]  # once
        self.assertEqual(rpolicy.agent_quicksaves(acts, "claude"), ["Claude-52151269953__QuickSave_7"])
        self.assertEqual(rpolicy.agent_quicksaves(acts, None), [])

    def test_record_and_pick(self):
        r = lpt.Runner.__new__(lpt.Runner)
        r.opt = rstate.Options("pt", None, 20, "opus", save_owner="Claude")
        r.state = type("S", (), {"data": {"runner_saves": [], "save_owner": "Claude"},
                                 "__getitem__": lambda s, k: s.data.get(k),
                                 "__setitem__": lambda s, k, v: s.data.__setitem__(k, v)})()
        acts = [{"t": 5, "tool": "save_game.bg", "ok": True, "folder": f"Claude-1__QuickSave_{i}"} for i in range(60)]
        with mock.patch.object(rpolicy, "read_actions_since", lambda t, *a, **k: acts):
            r.record_agent_saves(0)
        self.assertEqual(len(r.state["agent_saves"]), rpolicy.AGENT_SAVES_KEPT)
        self.assertEqual(r.state["agent_saves"][-1], "Claude-1__QuickSave_59")
        saves = [sv("Claude-1__S010-end", 10.0), sv("Claude-1__QuickSave_59", 20.0),
                 sv("Claude-1__QuickSave_99", 30.0)]
        self.assertEqual(rgame.pick_save(saves, None, "Claude", r.ours()).label, "QuickSave_59")

    def test_position_keys_are_not_progress(self):
        status = {"level": "L", "party": [{"name": "Claude", "pos": [1, 2, 3]}, {"name": "Gale", "pos": [4, 5, 6]}]}
        a = G.progress_signature(status, [])
        self.assertEqual((a["pos_name"], a["party_pos"]["Gale"]), ("Claude", [4.0, 5.0, 6.0]))
        b = G.progress_signature(dict(status, party=status["party"][::-1]), [])
        self.assertFalse(rpolicy.made_progress(a, b))
        self.assertIsNone(rpolicy.state_drift(a, b))           # same members, same places, other order


class Relaunch(unittest.TestCase):
    """se's discriminator (2026-10-10 18:07-18:18): party ready checks break in a long-lived game process; the
    runner relaunches it between sessions (age ≥ GAME_MAX_AGE_H, or the last session logged a ready-check failure)."""

    def test_reason(self):
        now = 100_000.0
        self.assertIsNone(rpolicy.relaunch_reason(now - 3600, now, "fine", 2))
        self.assertIn("2.0 h old", rpolicy.relaunch_reason(now - 7200, now, "", 2))
        self.assertIsNone(rpolicy.relaunch_reason(now - 9 * 3600, now, "", 0))   # 0 = never by age
        why = rpolicy.relaunch_reason(now - 60, now, "EVENTS\n  rest_flow step=ready_failed:ReadyCheckContent_GoToNight\n", 2)
        self.assertIn("ready_failed:ReadyCheckContent_GoToNight", why)
        self.assertIn("end_day_failed", rpolicy.relaunch_reason(None, now, "rest_flow step=end_day_failed", 2))
        self.assertIsNone(rpolicy.relaunch_reason(None, now, None, 2))

    def test_reason_counts_the_coming_session_and_ram(self):
        now, session = 100_000.0, 30 * 60 + rpolicy.FIGHT_HOLD_S
        # S082: 1.9 h old at the session start was kept and ran to 2.3 h; with the session counted it relaunches
        why = rpolicy.relaunch_reason(now - 1.9 * 3600, now, "", 2, session_s=session)
        self.assertIn("1.9 h old and the session may run 45 min (to 2.6 h)", why)
        self.assertIsNone(rpolicy.relaunch_reason(now - 1.2 * 3600, now, "", 2, session_s=session))
        why = rpolicy.relaunch_reason(now - 600, now, "", 2, session_s=session, ram_mb=7100, ram_limit_mb=7000)
        self.assertIn("7100 MB RAM+swap (relaunched at 7000 MB", why)
        self.assertIsNone(rpolicy.relaunch_reason(now - 600, now, "", 2, ram_mb=6900, ram_limit_mb=7000))
        self.assertIsNone(rpolicy.relaunch_reason(now - 600, now, "", 2, ram_mb=9000, ram_limit_mb=0))   # 0 = off

    def test_pick_save_never_an_autosave(self):
        f = sv
        saves = [f("Claude-1__S032-end", 10.0), f("Claude-2__AutoSave_59", 20.0), f("Claude-3__QuickSave_4", 30.0)]
        self.assertEqual(rgame.pick_save(saves, None, "Claude", set()).label, "S032-end")
        self.assertEqual(rgame.pick_save(saves, "AutoSave_59", "Claude", set()).label, "S032-end")   # not even by HANDOFF
        self.assertEqual(rgame.pick_save(saves, None, "Claude", {"Claude-3__QuickSave_4"}).label, "QuickSave_4")  # runner's own

    def _runner(self, d: Path, *, launched, log_text, save_mtime, last_started=50.0, steam=(True, "offline"),
                owns=True, first=False, ram=5000, mod_path=None, acts=(), extra_saves=(), runner_saves=(),
                save_combat=None, combat_now=False, last_ended=None, save_saved=None):
        self.saved = getattr(self, "saved", None) or (G.port_open, rcode.steam_ready, rcode.list_saves, rpolicy.game_started_at,
                      renv.log, rpolicy.game_ram_mb, rcode.game_mod_path, rpolicy.read_actions_since, G.party_in_combat)  # first call's
        G.port_open = lambda *a, **k: True
        G.party_in_combat = lambda: combat_now
        rpolicy.read_actions_since = lambda t, *a, **k: [x for x in acts if x.get("t", 0) > t]   # never the real log
        rpolicy.game_ram_mb = lambda: ram
        rcode.game_mod_path = lambda: mod_path   # never the machine's real game
        rcode.steam_ready = lambda: steam
        rcode.list_saves = lambda: [sv("Claude-1__S032-end", save_mtime, saved=save_saved), *extra_saves]
        rpolicy.game_started_at = lambda: launched
        renv.log = lambda m: self.logs.append(m)
        self.logs, self.killed, self.events = [], [], []
        r = lpt.Runner.__new__(lpt.Runner)
        r.opt = rstate.Options("pt", None, 30, "opus", save_owner="Claude", take_over=True)
        r.mem = d
        r.pending_notes, r.relaunch_save = [], None
        r.state = type("S", (), {"dir": d, "data": {"game": None, "save_owner": "Claude",
                                                       "runner_saves": list(runner_saves),
                                                       "runner_save_combat": save_combat, "loaded_save": "x",
                                                       "history": [{"kind": "play", "session": "S032",
                                                                    "started": last_started,
                                                                    **({"ended": last_ended} if last_ended else {})}]},
                                  "__getitem__": lambda s, k: s.data.get(k),
                                  "__setitem__": lambda s, k, v: s.data.__setitem__(k, v)})()
        (d / "S032.log").write_text(log_text)
        r.owns_game = lambda: owns
        r.kill_game = lambda: self.killed.append(1)
        r.event = lambda kind, **f: self.events.append(kind)
        return r

    def tearDown(self):
        if hasattr(self, "saved"):
            (G.port_open, rcode.steam_ready, rcode.list_saves, rpolicy.game_started_at, renv.log, rpolicy.game_ram_mb,
             rcode.game_mod_path, rpolicy.read_actions_since, G.party_in_combat) = self.saved

    def test_relaunches_after_a_ready_check_failure(self):
        with tempfile.TemporaryDirectory() as td:
            r = self._runner(Path(td), launched=time.time() - 600, log_text="step=ready_failed:X", save_mtime=100.0)
            self.assertIn("ready_failed", r.maybe_relaunch(False))
            self.assertEqual(self.killed, [1])
            self.assertIsNone(r.state["loaded_save"])
            self.assertIn("game_relaunch", self.events)

    def test_relaunches_an_old_process(self):
        with tempfile.TemporaryDirectory() as td:
            r = self._runner(Path(td), launched=time.time() - 5 * 3600, log_text="ok", save_mtime=100.0)
            self.assertIn("h old", r.maybe_relaunch(False))
            self.assertEqual(self.killed, [1])

    def test_young_healthy_process_kept(self):
        with tempfile.TemporaryDirectory() as td:
            r = self._runner(Path(td), launched=time.time() - 600, log_text="ok", save_mtime=100.0)
            self.assertIsNone(r.maybe_relaunch(False))
            self.assertEqual(self.killed, [])

    def test_relaunches_at_the_session_start_when_the_session_would_pass_the_age(self):
        with tempfile.TemporaryDirectory() as td:
            r = self._runner(Path(td), launched=time.time() - 1.9 * 3600, log_text="ok", save_mtime=100.0)
            self.assertIn("the session may run 45 min", r.maybe_relaunch(False, 30))
            self.assertEqual(self.killed, [1])
            self.assertTrue(any("relaunching the game: the game process is 1.9 h old" in m for m in self.logs))

    def test_relaunches_a_game_already_high_on_ram(self):
        with tempfile.TemporaryDirectory() as td:
            r = self._runner(Path(td), launched=time.time() - 600, log_text="ok", save_mtime=100.0,
                             ram=renv.RELAUNCH_RAM_MB + 1)
            self.assertIn("MB RAM+swap", r.maybe_relaunch(False, 30))
            self.assertEqual(self.killed, [1])

    def test_kept_log_names_the_ram(self):
        with tempfile.TemporaryDirectory() as td:
            r = self._runner(Path(td), launched=time.time() - 600, log_text="ok", save_mtime=100.0, ram=5432)
            self.assertIsNone(r.maybe_relaunch(False, 30))
            self.assertTrue(any("5432 MB RAM+swap" in m and "kept" in m for m in self.logs))

    def test_relaunches_a_game_loading_the_main_trees_mod(self):
        # S24 switch-over: a game launched before the snapshot (BG3LE_MOD_PATH=<repo>/mod) is relaunched once at a
        # boundary; one loading the snapshot is kept
        with tempfile.TemporaryDirectory() as td:
            r = self._runner(Path(td), launched=time.time() - 600, log_text="ok", save_mtime=100.0,
                             mod_path=str(rcode.MOD_DIR))
            why = r.maybe_relaunch(False, 30)
            self.assertIn(f"loads the mod from {rcode.MOD_DIR}, not the runner's snapshot {rcode.MOD_LIVE}", why)
            self.assertEqual(self.killed, [1])
        with tempfile.TemporaryDirectory() as td:
            r = self._runner(Path(td), launched=time.time() - 600, log_text="ok", save_mtime=100.0,
                             mod_path=str(rcode.MOD_LIVE))
            self.assertIsNone(r.maybe_relaunch(False, 30))
            self.assertEqual(self.killed, [])

    def test_reason_mod_path(self):
        now = 100_000.0
        self.assertIn("not the runner's snapshot /rt/mod-live",
                      rpolicy.relaunch_reason(now - 60, now, "", 2, mod_path="/repo/mod", mod_live="/rt/mod-live"))
        self.assertIsNone(rpolicy.relaunch_reason(now - 60, now, "", 2, mod_path="/rt/mod-live", mod_live="/rt/mod-live"))
        self.assertIsNone(rpolicy.relaunch_reason(now - 60, now, "", 2, mod_path=None, mod_live="/rt/mod-live"))  # unknown

    def test_kept_when_steam_not_offline(self):
        with tempfile.TemporaryDirectory() as td:
            r = self._runner(Path(td), launched=time.time() - 600, log_text="end_day_failed", save_mtime=100.0,
                             steam=(False, "Steam is not running"))
            self.assertIsNone(r.maybe_relaunch(False))
            self.assertEqual(self.killed, [])
            self.assertIn("game_relaunch_skipped", self.events)

    def test_kept_when_the_newest_save_predates_the_last_session(self):
        with tempfile.TemporaryDirectory() as td:
            r = self._runner(Path(td), launched=time.time() - 600, log_text="ready_failed", save_mtime=10.0,
                             last_started=50.0)
            self.assertIsNone(r.maybe_relaunch(False))
            self.assertEqual(self.killed, [])
            self.assertTrue(any("lose play" in m for m in self.logs))

    def test_kept_when_the_newest_save_predates_the_last_session_though_steam_restored_it(self):
        # Steam's local cloud restored S032-end at the launch: mtime 60 (after S032's start), its SaveTime still 10
        with tempfile.TemporaryDirectory() as td:
            r = self._runner(Path(td), launched=time.time() - 600, log_text="ready_failed", save_mtime=60.0,
                             save_saved=10.0, last_started=50.0)
            self.assertIsNone(r.maybe_relaunch(False))
            self.assertEqual(self.killed, [])
            self.assertTrue(any("lose play" in m for m in self.logs))

    # S092 (09:03:17 load_game S090-end, then a refused do_actions, the wrap-up notes and end_session): actions.jsonl
    S092_END = (
        {"t": 60.0, "tool": "use_ability", "args": {}, "result": "ok: Fireball"},
        {"t": 61.0, "tool": "load_game", "args": {"save": "Claude-1__S032-end"},
         "result": "load_game: done — loaded=S032-end\nFIGHT DEBRIEF (reloaded, 2 rounds)"},
        {"t": 62.0, "tool": "manage_inventory", "args": {"op": "use"}, "result": "FAILED: unknown target 'Elixir'"},
        {"t": 62.1, "tool": "do_actions", "args": {"n": 20}, "result": "1. FAILED: unknown target\nSTOPPED: step 1"},
        {"t": 63.0, "tool": "memory_read", "args": {}, "result": "knowledge/playbooks/session-wrapup.md"},
        {"t": 64.0, "tool": "memory_write", "args": {}, "result": "Rewrote memory/HANDOFF.md"},
        {"t": 64.5, "tool": "note", "args": {}, "result": "Noted 1 line"},
        {"t": 65.0, "tool": "end_session", "args": {}, "result": "Memory for S092 committed"},
    )

    def test_ended_on_save(self):
        save = sv("Claude-1__S032-end", 10.0)
        self.assertTrue(rpolicy.ended_on_save(self.S092_END, save))
        self.assertTrue(rpolicy.ended_on_save(self.S092_END[1:2], save))    # the load was the last call
        self.assertFalse(rpolicy.ended_on_save([], save))
        self.assertFalse(rpolicy.ended_on_save(self.S092_END[:1], save))    # no load_game
        later = {"t": 66.0, "result": "ok"}
        for extra in ({**later, "tool": "equip_best"},                # S091: gear changed after the load
                      {**later, "tool": "move_to"},
                      {**later, "tool": "some_new_tool"}):            # unknown = a change
            self.assertFalse(rpolicy.ended_on_save(self.S092_END + (extra,), save), extra)
        for extra in ({**later, "tool": "move_to", "result": "NOT READY: loading"},
                      {**later, "tool": "get_status"}, {**later, "tool": "wait_for"}):
            self.assertTrue(rpolicy.ended_on_save(self.S092_END + (extra,), save), extra)
        other = {"t": 61.5, "tool": "load_game", "args": {"save": "Claude-1__S031-end"},
                 "result": "load_game: done — loaded=S031-end"}
        self.assertFalse(rpolicy.ended_on_save(self.S092_END[:2] + (other,), save))   # the last load was another save
        failed = {"t": 61.5, "tool": "load_game", "args": {"save": "Claude-1__S032-end"},
                  "result": "FAILED: the game did not load"}
        self.assertFalse(rpolicy.ended_on_save(self.S092_END[:2] + (failed,), save))  # the last load failed
        twin = {"t": 61.5, "tool": "load_game", "args": {"save": "Claude-9__S032-end"},
                "result": "load_game: done — loaded=S032-end"}
        self.assertFalse(rpolicy.ended_on_save(self.S092_END[:2] + (twin,), save))    # same label, other folder
        by_label = {"t": 61.5, "tool": "load_game", "args": {"save": "S032-end"},
                    "result": "load_game: done — loaded=S032-end"}
        self.assertTrue(rpolicy.ended_on_save(self.S092_END[:2] + (by_label,), save))

    def test_relaunches_when_the_last_session_ended_on_the_older_save(self):
        # S092 09:05: the mod-snapshot relaunch was skipped ("S090-end is older than S092's start") although S092
        # ended on load_game S090-end with nothing changed after it
        with tempfile.TemporaryDirectory() as td:
            r = self._runner(Path(td), launched=time.time() - 600, log_text="ok", save_mtime=10.0, last_started=50.0,
                             mod_path=str(rcode.MOD_DIR), acts=self.S092_END)
            self.assertIn("loads the mod from", r.maybe_relaunch(False, 30))
            self.assertEqual(self.killed, [1])
            self.assertTrue(any("ended on load_game S032-end with nothing changed after it" in m for m in self.logs))
        with tempfile.TemporaryDirectory() as td:   # one action after the load: skipped as before
            r = self._runner(Path(td), launched=time.time() - 600, log_text="ok", save_mtime=10.0, last_started=50.0,
                             mod_path=str(rcode.MOD_DIR),
                             acts=self.S092_END + ({"t": 66.0, "tool": "equip_best", "result": "ok"},))
            self.assertIsNone(r.maybe_relaunch(False, 30))
            self.assertEqual(self.killed, [])
            self.assertTrue(any("lose play" in m for m in self.logs))

    # S093 (09:49:34): the agent didn't save after S090-end; the runner quicksaved at the session's end (QuickSave_24)
    QS = sv("Claude-4__QuickSave_24", 70.0)
    S093_END = (
        {"t": 60.0, "tool": "end_turn", "args": {}, "result": "end_turn: done"},
        {"t": 64.0, "tool": "memory_write", "args": {}, "result": "Rewrote memory/HANDOFF.md"},
        {"t": 65.0, "tool": "end_session", "args": {}, "result": "Memory for S093 committed"},
        {"t": 80.0, "tool": "get_status", "args": {}, "result": "WHERE Upper City"},   # the runner's check_all
    )

    def test_boundary_save(self):
        last = {"session": "S093", "started": 50.0}
        calm = {self.QS.folder: False}
        saves = [sv("Claude-1__S032-end", 10.0), self.QS,
                 sv("Claude-3__QuickSave_23", 40.0)]   # the previous boundary's
        runner = {self.QS.folder, "Claude-3__QuickSave_23"}
        self.assertEqual(rpolicy.boundary_save(saves, runner, calm, last, list(self.S093_END)), (self.QS, ""))
        got, why = rpolicy.boundary_save(saves, runner, {self.QS.folder: True}, last, list(self.S093_END))
        self.assertIsNone(got)
        self.assertIn("mid-fight", why)
        got, why = rpolicy.boundary_save(saves, runner, {}, last, list(self.S093_END))   # made before the flag existed
        self.assertIsNone(got)
        self.assertIn("unknown whether the party was in a fight", why)
        moved = {"t": 71.0, "tool": "move_to", "args": {}, "result": "move_to: done"}
        got, why = rpolicy.boundary_save(saves, runner, calm, last, [*self.S093_END, moved])
        self.assertIsNone(got)
        self.assertIn("move_to changed the game after it", why)
        refused = {**moved, "result": "NOT READY: loading"}
        self.assertEqual(rpolicy.boundary_save(saves, runner, calm, last, [*self.S093_END, refused])[0], self.QS)
        got, why = rpolicy.boundary_save(saves, {"Claude-3__QuickSave_23"}, calm, last, [])   # only one older than S093
        self.assertIsNone(got)
        self.assertIn("no runner quicksave since S093's start", why)
        agents = sv("Claude-5__QuickSave_25", 75.0)    # not the runner's: ignored
        self.assertEqual(rpolicy.boundary_save([*saves, agents], runner, calm, last, [])[0], self.QS)
        # the previous boundary's quicksave restored by Steam's local cloud at the launch (mtime 90, SaveTime 40): not
        # written in S093, so not the newer one
        restored = sv("Claude-3__QuickSave_23", 90.0, saved=40.0)
        got, why = rpolicy.boundary_save([saves[0], restored], runner, calm | {restored.folder: False}, last, [])
        self.assertIsNone(got)
        self.assertIn("no runner quicksave since S093's start", why)
        # S093's own quicksave restored (mtime 90): a move at 75 still came after it was written
        qs = sv(self.QS.folder, 90.0, saved=70.0)
        got, why = rpolicy.boundary_save([saves[0], qs], runner, calm, last, [{"t": 75.0, "tool": "move_to", "result": "ok"}])
        self.assertIsNone(got)
        self.assertIn("move_to changed the game after it", why)

    def _s093(self, td, **kw):
        # S093 wrote HANDOFF ("Save to load: S090-end") and ended 4 s before the quicksave: choose_save keeps S090-end
        (Path(td) / "HANDOFF.md").write_text('# Handoff S032 → S033\n\nSave to load: "S032-end"\n')
        r = self._runner(Path(td), launched=time.time() - 600, log_text="ok", save_mtime=10.0, last_started=50.0,
                         last_ended=66.0, mod_path=str(rcode.MOD_DIR), acts=self.S093_END, extra_saves=(self.QS,),
                         runner_saves=(self.QS.folder,), **kw)
        self.assertEqual(r.choose_save().label, "S032-end")
        return r

    def test_relaunches_onto_the_runners_boundary_quicksave(self):
        # S093/S094 09:50:53: "the newest save S090-end is older than S093's start: skipped" although the runner's
        # QuickSave_24 held where S093 ended; HANDOFF's older save stays choose_save's pick, the relaunch loads QS
        with tempfile.TemporaryDirectory() as td:
            r = self._s093(td, save_combat={self.QS.folder: False})
            self.assertIn("loads the mod from", r.maybe_relaunch(False, 30))
            self.assertEqual(self.killed, [1])
            self.assertEqual(r.relaunch_save, self.QS)
            self.assertEqual(r.save_choice[0], "QuickSave_24")
            self.assertTrue(any("then loading Claude-4__QuickSave_24" in m for m in self.logs))
            self.assertTrue(any("QuickSave_24" in n and "where that session left off" in n for n in r.pending_notes))

    def test_boundary_quicksave_not_used_mid_fight(self):
        for kw, why in (({"save_combat": {self.QS.folder: True}}, "made mid-fight"),
                        ({"save_combat": {self.QS.folder: False}, "combat_now": True}, "in a fight now"),
                        ({"save_combat": {self.QS.folder: False}, "combat_now": None}, "doesn't say"),
                        ({"save_combat": None}, "unknown whether")):
            self.logs = []
            with tempfile.TemporaryDirectory() as td:
                r = self._s093(td, **kw)
                self.assertIsNone(r.maybe_relaunch(False, 30), kw)
                self.assertEqual(self.killed, [], kw)
                self.assertIsNone(r.relaunch_save, kw)
                self.assertEqual(r.pending_notes, [], kw)
                self.assertTrue(any(why in m and "lose play" in m for m in self.logs), (kw, self.logs))
                self.tearDown()

    def test_boundary_quicksave_not_kept_when_steam_blocks_the_relaunch(self):
        with tempfile.TemporaryDirectory() as td:
            r = self._s093(td, save_combat={self.QS.folder: False}, steam=(False, "Steam is not running"))
            self.assertIsNone(r.maybe_relaunch(False, 30))
            self.assertEqual(self.killed, [])
            self.assertIsNone(r.relaunch_save)   # a later ensure_game (a crash) must not load it
            self.assertEqual(r.pending_notes, [])

    def test_lose_play_mark(self):
        last = {"session": "S093", "started": 50.0}
        self.assertEqual(rpolicy.lose_play_mark(last, []), (50.0, "S093's start"))
        reads = [{"t": 60.0, "tool": "get_status", "result": "WHERE"}, {"t": 61.0, "tool": "note", "result": "ok"}]
        self.assertEqual(rpolicy.lose_play_mark(last, reads)[0], 50.0)   # nothing changed the game: its start
        t, what = rpolicy.lose_play_mark(last, [*reads, {"t": 80.0, "tool": "move_to", "result": "move_to: done"},
                                          {"t": 81.0, "tool": "move_to", "result": "FAILED — no path"}])
        self.assertEqual(t, 80.0)   # the refused one changed nothing
        self.assertIn("S093's last game change (move_to at ", what)

    # A session saved partway (S032-end at 70, after its start at 50), then played on without saving (move_to at 80)
    PLAYED_ON = ({"t": 60.0, "tool": "save_game", "args": {}, "result": "save_game: done"},
                 {"t": 80.0, "tool": "move_to", "args": {}, "result": "move_to: done"},
                 {"t": 85.0, "tool": "end_session", "args": {}, "result": "Memory committed"})

    def test_kept_when_the_session_played_on_after_its_save(self):
        # before: only the session's start counted, so the relaunch loaded S032-end and lost the walk at 80
        with tempfile.TemporaryDirectory() as td:
            r = self._runner(Path(td), launched=time.time() - 600, log_text="ok", save_mtime=70.0, last_started=50.0,
                             mod_path=str(rcode.MOD_DIR), acts=self.PLAYED_ON)
            self.assertIsNone(r.maybe_relaunch(False, 30))
            self.assertEqual(self.killed, [])
            self.assertTrue(any("older than S032's last game change (move_to at" in m and "lose play" in m
                                for m in self.logs), self.logs)
        with tempfile.TemporaryDirectory() as td:   # saved after its last game change: relaunched as before
            r = self._runner(Path(td), launched=time.time() - 600, log_text="ok", save_mtime=82.0, last_started=50.0,
                             mod_path=str(rcode.MOD_DIR), acts=self.PLAYED_ON)
            self.assertIn("loads the mod from", r.maybe_relaunch(False, 30))
            self.assertEqual(self.killed, [1])

    def test_played_on_after_its_save_relaunches_onto_a_newer_boundary_quicksave(self):
        qs = sv("Claude-4__QuickSave_24", 90.0)
        for combat, relaunched in ((False, True), (True, False)):
            self.logs, self.killed = [], []
            with tempfile.TemporaryDirectory() as td:
                (Path(td) / "HANDOFF.md").write_text('# Handoff S032 → S033\n\nSave to load: "S032-end"\n')
                r = self._runner(Path(td), launched=time.time() - 600, log_text="ok", save_mtime=70.0,
                                 last_started=50.0, last_ended=88.0, mod_path=str(rcode.MOD_DIR), acts=self.PLAYED_ON,
                                 extra_saves=(qs,), runner_saves=(qs.folder,), save_combat={qs.folder: combat})
                self.assertEqual(r.choose_save().label, "S032-end")   # HANDOFF's, within the slack of S032's end
                why = r.maybe_relaunch(False, 30)
                self.assertEqual(why is not None, relaunched, self.logs)
                self.assertEqual(r.relaunch_save, qs if relaunched else None)
                if not relaunched:
                    self.assertTrue(any("made mid-fight" in m and "lose play" in m for m in self.logs), self.logs)
                self.tearDown()

    def test_ensure_game_loads_the_relaunch_save_once(self):
        saved = (G.port_open, renv.log)
        try:
            G.port_open = lambda *a, **k: False
            renv.log = lambda m: None
            r = lpt.Runner.__new__(lpt.Runner)
            r.state = {"history": [], "loaded_save": None}
            r.owns_game = lambda: True
            r.opt = rstate.Options("pt", None, 30, "opus")
            r.note_game_gone = r.check_game_oom = lambda *a: None
            r.choose_save = lambda: "chosen"
            r.load_save = lambda s: s
            r.relaunch_save = self.QS
            self.assertEqual(r.ensure_game(False), self.QS)
            self.assertIsNone(r.relaunch_save)
            self.assertEqual(r.ensure_game(False), "chosen")
        finally:
            G.port_open, renv.log = saved


class EnsureSave(unittest.TestCase):
    def test_records_whether_the_runner_quicksave_was_mid_fight(self):
        saved = (rgame.saves_since, rcode.list_saves, G.server_ready, G.available_ops, G.rpc, G.party_in_combat, renv.log)
        try:
            for combat, tail in ((False, ""), (True, " (mid-fight)"), (None, " (fight state unknown)")):
                logs, disk = [], []
                rgame.saves_since = lambda *a: []
                rcode.list_saves = lambda d=disk: list(d)
                G.server_ready = lambda: True
                G.available_ops = lambda c: {"ui.menu"}
                G.party_in_combat = lambda c=combat: c
                G.rpc = lambda *a, d=disk, **k: d.append(
                    sv("Claude-4__QuickSave_24", time.time()))
                renv.log = logs.append
                r = lpt.Runner.__new__(lpt.Runner)
                r.state = {"save_owner": "Claude", "runner_saves": [], "runner_save_combat": None}
                r.game_alive = lambda: True
                r.ensure_save(0.0)
                self.assertEqual(r.state["runner_saves"], ["Claude-4__QuickSave_24"])
                self.assertEqual(r.state["runner_save_combat"], {"Claude-4__QuickSave_24": combat})
                self.assertIn(f"quicksave written: Claude-4__QuickSave_24{tail}", logs)
        finally:
            (rgame.saves_since, rcode.list_saves, G.server_ready, G.available_ops, G.rpc, G.party_in_combat,
             renv.log) = saved


    def test_the_quicksave_is_the_new_folder_by_its_own_time_never_a_steam_restored_copy(self):
        saved = (rgame.saves_since, rcode.list_saves, G.server_ready, G.available_ops, G.rpc, G.party_in_combat, renv.log)
        try:
            logs, now = [], time.time()
            # Steam's local cloud restores an earlier session's save of another character (SaveTime 100) at the launch
            disk = [sv("Karlach-9__S004-end", now - 5, saved=100.0)]
            rgame.saves_since = lambda *a: []
            rcode.list_saves = lambda: list(disk)
            G.server_ready = lambda: True
            G.available_ops = lambda c: {"ui.menu"}
            G.party_in_combat = lambda: False
            # while the runner waits: its quicksave, and a restored copy of an old quicksave (a new folder, mtime later)
            G.rpc = lambda *a, **k: disk.extend([sv("Claude-4__QuickSave_24", now), sv("Claude-7__QuickSave_9", now + 2,
                                                                                          saved=200.0)])
            renv.log = logs.append
            r = lpt.Runner.__new__(lpt.Runner)
            r.state = {"save_owner": "Claude", "runner_saves": [], "runner_save_combat": None}
            r.game_alive = lambda: True
            r.ensure_save(now - 60)
            self.assertEqual(r.state["runner_saves"], ["Claude-4__QuickSave_24"])
            self.assertIn("the agent didn't save this session; quicksaving", logs)  # not "session saves ['Karlach-9...']"
        finally:
            (rgame.saves_since, rcode.list_saves, G.server_ready, G.available_ops, G.rpc, G.party_in_combat,
             renv.log) = saved

class Gates(unittest.TestCase):
    def setUp(self):
        self.saved = (G.rpc, G.lua_eval, renv.log)
        self.calls = []

    def test_decision(self):
        self.assertTrue(rpolicy.gates_decision(None, None, True))               # new game
        self.assertTrue(rpolicy.gates_decision("Claude", "Claude", False))
        self.assertTrue(rpolicy.gates_decision(" claude ", "Claude", False))
        self.assertFalse(rpolicy.gates_decision("pibooool", "Claude", False))   # the user's own save
        self.assertIsNone(rpolicy.gates_decision(None, "Claude", False))        # name unreadable
        self.assertIsNone(rpolicy.gates_decision("Claude", None, False))        # owner unknown

    def _runner(self, new_game=False, name="Claude"):
        r = lpt.Runner.__new__(lpt.Runner)
        r.opt = rstate.Options("pt", None, 60, "opus", save_owner="Claude")
        r.state = type("S", (), {"__getitem__": lambda s, k: None})()
        r.new_game = new_game
        self.calls = []
        renv.log = lambda m: None
        G.lua_eval = lambda *a, **k: name
        return r

    def tearDown(self):
        G.rpc, G.lua_eval, renv.log = self.saved

    def test_settings_errors_never_raise(self):
        r = self._runner()

        def boom(op, args=None, context="server", timeout=0):
            self.calls.append((op, args))
            raise RuntimeError("game not answering")
        G.rpc = boom
        r.apply_play_settings()                                            # must not raise
        self.assertEqual([c[0] for c in self.calls[:3]], ["ui.config", "ui.settings", "ui.dismiss"])
        self.assertEqual(self.calls[0][1], {"autoskip": True})
        self.assertEqual(self.calls[1][1], {"show_tutorials": False})
        self.assertEqual(rpolicy.play_settings({"BG3AI_AUTOSKIP": "0"})[0], ("ui.config", {"autoskip": False}))
        self.assertEqual(rpolicy.play_settings({})[0], ("ui.config", {"autoskip": True}))  # on unless set off

    def test_gates_follow_character(self):
        for name, new_game, expected in (("Claude", False, True), ("pibooool", False, False),
                                         ("", True, True), ("", False, None)):
            r = self._runner(new_game, name)
            G.rpc = lambda op, args=None, context="server", timeout=0: self.calls.append((op, args))
            r.apply_play_settings()
            gates = [a for op, a in self.calls if op == "ui.config" and "save_enabled" in a]
            if expected is None:
                self.assertEqual(gates, [], name)
            else:
                self.assertEqual(gates, [{"save_enabled": expected, "load_enabled": expected}], name)
            # UI-2: the MCP server follows the same decision (its _enable_save_gates used to reopen closed gates)
            self.assertEqual(r.save_env().get("BG3AI_SAVE_GATES"), {True: "1", False: "0", None: None}[expected])
            G.rpc, G.lua_eval, renv.log = self.saved

    def test_ui_strict_switch(self):
        """S17: BG3AI_UI_STRICT=1/0 sets bg3le's strict frame lock in the running game at session start; unset
        (the default) sends nothing."""
        self.assertIsNone(rpolicy.ui_strict_wanted({}))
        self.assertIsNone(rpolicy.ui_strict_wanted({"BG3AI_UI_STRICT": "yes"}))
        self.assertTrue(rpolicy.ui_strict_wanted({"BG3AI_UI_STRICT": "1"}))
        self.assertFalse(rpolicy.ui_strict_wanted({"BG3AI_UI_STRICT": " 0 "}))
        self.assertIn("f(true)", rpolicy.ui_strict_lua(True))
        self.assertIn("f(false)", rpolicy.ui_strict_lua(False))
        self.assertEqual(rpolicy.ui_strict_text("true 3 189 0"), "ON (refused 3, lock timeouts lua 189 ui 0)")
        self.assertEqual(rpolicy.ui_strict_text("false 0 33 0"), "off (refused 0, lock timeouts lua 33 ui 0)")
        self.assertIn("lacks patch 0010", rpolicy.ui_strict_text("missing"))
        for env, sent in ((None, False), ("1", True), ("0", True)):
            r = self._runner()
            evals, logs = [], []
            G.lua_eval = lambda code, ctx="client", evals=evals, **k: evals.append((code, ctx)) or (
                "true 0 5 0" if "UiStrict" in code else "Claude")
            renv.log = logs.append
            G.rpc = lambda op, args=None, context="server", timeout=0: None
            old = os.environ.pop("BG3AI_UI_STRICT", None)
            try:
                if env is not None:
                    os.environ["BG3AI_UI_STRICT"] = env
                r.apply_play_settings()
            finally:
                os.environ.pop("BG3AI_UI_STRICT", None)
                if old is not None:
                    os.environ["BG3AI_UI_STRICT"] = old
            strict = [(c, x) for c, x in evals if "UiStrict" in c]
            self.assertEqual(bool(strict), sent, env)
            if sent:
                self.assertEqual(strict[0][1], "client")
                self.assertIn(f"f({'true' if env == '1' else 'false'})", strict[0][0])
                self.assertIn("ui strict: ON (refused 0, lock timeouts lua 5 ui 0)", logs)
            G.rpc, G.lua_eval, renv.log = self.saved


class SaveIdentity(unittest.TestCase):
    """RUN-5 / I1: the MCP server gets BG3AI_SAVE_OWNER and a BG3AI_SAVE_PREFIX derived from --save-owner alone (the
    README quick start sets no prefix; an empty one meant "No saves." and no reload after a wipe)."""

    def test_pure(self):
        self.assertEqual(rpolicy.save_identity("Claude", ""), ("Claude", "Claude-"))
        self.assertEqual(rpolicy.save_identity("Claude", "Claude-"), ("Claude", "Claude-"))
        self.assertEqual(rpolicy.save_identity("Claude", "pt1-"), ("Claude", "pt1-"))   # the operator's own prefix wins
        self.assertEqual(rpolicy.save_identity(None, ""), ("", ""))                     # new game, no name yet
        self.assertEqual(rpolicy.gates_env(None), {})

    def test_run_claude_env_from_save_owner_alone(self):
        tmp = Path(tempfile.mkdtemp(prefix="bg3ai-env-"))
        self.addCleanup(__import__("shutil").rmtree, tmp, True)
        envs = []

        def popen(cmd, **kw):
            envs.append(kw["env"])
            raise OSError("stop here")
        with mock.patch.multiple(renv, RUNTIME=tmp, log=lambda m: None), mock.patch.object(rcode, "list_saves", list), \
                mock.patch.object(rgame, "SAVE_PREFIX", ""), \
                mock.patch.object(rcode.subprocess, "Popen", popen), \
                mock.patch.dict(renv.os.environ, {"BG3AI_SAVE_GATES": "1", "BG3AI_SAVE_OWNER": "Stale"}):
            r = lpt.Runner(rstate.Options("env-test", None, 60, "opus", save_owner="Claude"))
            r.gates = False
            with self.assertRaises(OSError):
                r.run_claude("S001", "go", 60, "c")
            renv.os.environ.pop("BG3AI_SAVE_OWNER")
            r2 = lpt.Runner(rstate.Options("env-new", None, 60, "opus"))
            with self.assertRaises(OSError):
                r2.run_claude("S001", "go", 60, "c")
        self.assertEqual((envs[0]["BG3AI_SAVE_OWNER"], envs[0]["BG3AI_SAVE_PREFIX"], envs[0]["BG3AI_SAVE_GATES"]),
                         ("Claude", "Claude-", "0"))
        self.assertEqual((envs[1]["BG3AI_SAVE_OWNER"], envs[1]["BG3AI_SAVE_PREFIX"]), ("", ""))
        self.assertNotIn("BG3AI_SAVE_GATES", envs[1])                     # not decided: no stale value passes


class ClaudeSaves(unittest.TestCase):
    def test_claude_owner(self):
        f = sv
        saves = [f("Claude-11__S001-start", 10), f("Claude-12__S002-end", 20), f("pibooool-9__S002-end", 30),
                 f("Claude-13__QuickSave_4", 40)]
        best = rgame.pick_save(saves, "S002-end", "Claude", set(), "")
        self.assertEqual(best.folder, "Claude-12__S002-end")
        self.assertEqual(saves[0].owner, "Claude")


if __name__ == "__main__":
    unittest.main()

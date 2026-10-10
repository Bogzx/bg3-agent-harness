"""feed_gale; press_key ESCAPE: refused on the plain game view (S021/S026 it opened the game menu), menu closed again if it opens."""
from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_turn_macros import Base  # noqa: E402,F401  (sets BG3AI_RUNTIME before importing server)
from bg3ai import server  # noqa: E402
from bg3ai.game import Game, GameError, JobResult, NotReady  # noqa: E402
from bg3ai.luadebug import LuaDebugError  # noqa: E402


class Fake:
    def __init__(self, screens):
        self.screens, self.keys = list(screens), []

    def rpc(self, op, args=None, context="server"):
        if op == "ui.state":
            scr = self.screens.pop(0) if len(self.screens) > 1 else self.screens[0]
            return scr if isinstance(scr, dict) else {"screen": scr}
        if op == "ui.key":
            self.keys.append(args["key"])
        if op == "ui.press":  # the game menu's Resume button
            self.keys.append("RESUME:" + args["button"])
            if getattr(self, "resume_closes", True):
                self.screens = ["game"]
        return {}

    try_rpc = rpc

    def run_job(self, op, args, context="server", wait_s=25.0, **kw):
        self.jobs = getattr(self, "jobs", []) + [(op, args, context)]
        if op == "ui.donate":
            return JobResult(status="done", result={"donated": args["item"]})
        self.keys.append(args["key"])
        return JobResult(status="done", result={"key": args["key"]})

    def drain_events(self, ctx):
        return []


class PressEscape(unittest.TestCase):
    def use(self, screens):
        fake = Fake(screens)
        orig = server.game
        server.game = fake
        self.addCleanup(setattr, server, "game", orig)
        return fake

    def test_plain_game_view_refused(self):
        fake = self.use(["game"])
        out = server.press_key.fn("escape") if hasattr(server.press_key, "fn") else server.press_key("escape")
        self.assertTrue(out.startswith("REFUSED"), out)
        self.assertEqual(fake.keys, [])

    def test_open_panel_closes(self):
        fake = self.use([{"screen": "game", "panels_open": ["Inventory"]}, "game"])
        out = getattr(server.press_key, "fn", server.press_key)("ESCAPE")
        self.assertNotIn("REFUSED", out)
        self.assertEqual(fake.keys, ["ESCAPE"])

    def test_menu_that_opens_is_closed_again(self):
        fake = self.use(["trade", "game_menu"])
        out = getattr(server.press_key, "fn", server.press_key)("ESCAPE")
        self.assertEqual(fake.keys, ["ESCAPE", "RESUME:Resume"])
        self.assertIn("closed it again", out)

    def test_menu_that_stays_open_is_reported(self):
        """Live 12:1x (Karlach's menu): a second ESCAPE key left the game menu open while the result said
        'closed it again'. Resume first, then the key; if it is still up, say so, and that ESCAPE doesn't end a talk."""
        fake = self.use(["dialogue", "game_menu"])
        fake.resume_closes = False
        server.time.sleep, real = (lambda s: None), server.time.sleep
        self.addCleanup(setattr, server.time, "sleep", real)
        out = getattr(server.press_key, "fn", server.press_key)("ESCAPE")
        self.assertEqual(fake.keys, ["ESCAPE", "RESUME:Resume", "ESCAPE"])
        self.assertIn("STILL OPEN", out)
        self.assertNotIn("closed it again", out)
        self.assertIn("choose_dialogue", out)

    def test_escape_on_game_menu_closes_it_once(self):
        fake = self.use(["game_menu", "game"])
        getattr(server.press_key, "fn", server.press_key)("ESCAPE")
        self.assertEqual(fake.keys, ["ESCAPE"])

    def test_unknown_screen_presses(self):
        fake = self.use([None])
        fake.rpc = lambda op, args=None, context="server": (fake.keys.append(args["key"]) if op == "ui.key" else None)
        fake.try_rpc = fake.rpc
        getattr(server.press_key, "fn", server.press_key)("ESCAPE")
        self.assertEqual(fake.keys[0], "ESCAPE")

    def test_other_keys_untouched(self):
        fake = self.use(["game"])
        getattr(server.press_key, "fn", server.press_key)("space")
        self.assertEqual(fake.keys, ["SPACE"])



class FeedGale(unittest.TestCase):
    def test_item_by_handle_goes_to_ui_donate(self):
        fake = Fake(["donate"])
        orig = server.game
        server.game = fake
        self.addCleanup(setattr, server, "game", orig)
        u = "cccccccc-0000-0000-0000-000000000001"
        h = server.H.get(u, "Komira's Locket")
        out = getattr(server.feed_gale, "fn", server.feed_gale)(h)
        self.assertEqual(fake.jobs[0][:2], ("ui.donate", {"item": u}))
        self.assertEqual(fake.jobs[0][2], "client")
        self.assertIn("feed_gale: done", out)

    def test_donate_screen_hint(self):
        self.assertIn("feed_gale", server._input_screen_text("donate"))



class NightFake:
    """Night camp: travel_block says night_camp until ui.long_rest(sleep_only) ends it (or fails)."""

    def __init__(self, ends=True):
        self.night, self.ends, self.jobs = True, ends, []

    def rpc(self, op, args=None, context="server"):
        if op == "ui.travel_block":
            return {"night_camp": True, "error": "Unavailable"} if self.night else {"hud": True}
        if op == "ui.fast_travel":
            return {"can": True}
        if op == "state.status":
            return {"party": []}
        return {}

    try_rpc = rpc

    def run_job(self, op, args, context="server", wait_s=25.0, **kw):
        self.jobs.append((op, args))
        if op == "ui.long_rest":
            if self.ends:
                self.night = False
                return JobResult(status="done", result={"night_ended": True, "rest": "night_finished"})
            return JobResult(status="failed", result={"step": "sleep"}, error="the night at camp didn't end")
        return JobResult(status="done", result={})

    def drain_events(self, ctx):
        return []


class NightCamp(unittest.TestCase):
    def use(self, fake):
        orig = server.game
        server.game = fake
        self.addCleanup(setattr, server, "game", orig)
        orig_wait = server._wait_running
        server._wait_running = lambda max_s=60.0: True
        self.addCleanup(setattr, server, "_wait_running", orig_wait)
        return fake

    def test_fast_travel_ends_the_night_first(self):
        fake = self.use(NightFake())
        out = getattr(server.fast_travel, "fn", server.fast_travel)("Last Light Inn")
        ops = [o for o, _ in fake.jobs]
        self.assertEqual(ops[0], "ui.long_rest")
        self.assertEqual(fake.jobs[0][1], {"sleep_only": True})
        self.assertIn("ui.fast_travel", ops)
        self.assertIn("ended the night at camp first", out)

    def test_night_that_wont_end_stops_travel(self):
        fake = self.use(NightFake(ends=False))
        out = getattr(server.fast_travel, "fn", server.fast_travel)("Last Light Inn")
        self.assertNotIn("ui.fast_travel", [o for o, _ in fake.jobs])
        self.assertIn("night at camp hasn't ended", out)

    def test_camp_leave_ends_the_night_first(self):
        fake = self.use(NightFake())
        out = getattr(server.camp, "fn", server.camp)("leave")
        self.assertEqual([o for o, _ in fake.jobs][:2], ["ui.long_rest", "act.leave_camp"])
        self.assertIn("ended the night", out)

    def test_no_night_no_rest(self):
        fake = NightFake()
        fake.night = False
        self.use(fake)
        getattr(server.camp, "fn", server.camp)("leave")
        self.assertEqual([o for o, _ in fake.jobs], ["act.leave_camp"])


    # S030 16:46: camp() inside the Gauntlet of Shar failed "cannot travel right now" with no reason.
    def test_camp_go_failure_says_why_and_the_way_out(self):
        fake = NightFake()
        fake.night = False
        base = fake.rpc

        def rpc(op, args=None, context="server"):
            if op == "ui.travel_block":
                return {"hud": True, "error": fake.err}
            return base(op, args, context)

        fake.rpc = fake.try_rpc = rpc
        fake.run_job = lambda op, args, context="server", wait_s=25.0, **kw: JobResult(
            status="failed", result=None, error="cannot travel right now")
        self.use(fake)
        go = getattr(server.camp, "fn", server.camp)
        fake.err = "DangerZone"
        self.assertIn("(DangerZone): this is a danger zone", go())
        fake.err = "Unavailable"
        out = go()
        self.assertIn("(Unavailable): this area does not let the party go to camp", out)
        self.assertIn("walk back out the way you came", out)
        fake.err = None
        self.assertIn("The game gave no reason", go())

    def test_waits_for_the_party_to_wake_up(self):
        # Acceptance 12:16: long_rest returned with the party Sleeping (TravelError 'Condition', no night_camp);
        # fast_travel failed at once. Now it waits for the HUD + no error, then travels.
        fake = NightFake()
        fake.night = False
        polls = {"n": 0}
        base = fake.rpc

        def rpc(op, args=None, context="server"):
            if op == "ui.travel_block":
                polls["n"] += 1
                return {"in_camp": True, "error": "Condition", "hud": True} if polls["n"] <= 3 else {"hud": True}
            return base(op, args, context)

        fake.rpc = fake.try_rpc = rpc
        self.use(fake)
        orig = server.time.sleep
        server.time.sleep = lambda s: None
        self.addCleanup(setattr, server.time, "sleep", orig)
        out = getattr(server.fast_travel, "fn", server.fast_travel)("Last Light Inn")
        self.assertIn("ui.fast_travel", [o for o, _ in fake.jobs])
        self.assertIn("for the party to wake up", out)
        self.assertGreater(polls["n"], 3)


class BootErrors(unittest.TestCase):
    """Pass 4: raw Lua errors from a game still loading reached the agent; now NotReady."""

    def eval_raising(self, msg):
        g = Game()

        class C:
            def evaluate(self, code, context="client", timeout=12.0):
                raise LuaDebugError(msg)

        g._acquire = lambda: C()
        g._release = lambda c, ok: None
        return lambda: g.eval("return 1")

    def test_booting_errors_are_not_ready(self):
        for msg in ('[string "x"]:1: attempt to index a nil value (field \'BG3AI\')',
                    "server context runs once a save is loaded (at the main menu use :client)"):
            with self.assertRaises(NotReady, msg=msg) as cm:
                self.eval_raising(msg)()
            self.assertIn("still loading", str(cm.exception))

    def test_other_lua_errors_stay_game_errors(self):
        with self.assertRaises(GameError) as cm:
            self.eval_raising("attempt to index a nil value (field 'foo')")()
        self.assertNotIsInstance(cm.exception, NotReady)


class LoadCrash(unittest.TestCase):
    def test_process_gone_fails_at_once(self):
        alive = iter([True] + [False] * 50)
        orig_pt = server.PLAYTHROUGH
        server.PLAYTHROUGH = "test"
        self.addCleanup(setattr, server, "PLAYTHROUGH", orig_pt)
        orig = (server.game, server._game_process_alive, server.SAVE_DIR, server.SAVE_PREFIX, server.time.sleep)
        self.addCleanup(lambda: (setattr(server, "game", orig[0]), setattr(server, "_game_process_alive", orig[1]),
                                 setattr(server, "SAVE_DIR", orig[2]), setattr(server, "SAVE_PREFIX", orig[3]),
                                 setattr(server.time, "sleep", orig[4])))
        import tempfile
        d = Path(tempfile.mkdtemp())
        (d / "Claude-1__LIVETEST-se").mkdir()
        server.SAVE_DIR, server.SAVE_PREFIX = d, "Claude-"
        server._game_process_alive = lambda: next(alive)
        server.time.sleep = lambda s: None

        class G(Fake):
            def game_state(self):
                return "Loading"

            def server_ready(self):
                return False

            def run_job(self, op, args, context="server", wait_s=25.0, **kw):
                return JobResult(status="done", result={"loading": True})

        server.game = G(["game"])
        t0 = time.monotonic()
        out = getattr(server.load_game, "fn", server.load_game)("LIVETEST-se")
        self.assertLess(time.monotonic() - t0, 5)
        self.assertIn("CRASHED", out)

    def test_load_that_never_starts_fails_and_closes_the_page(self):
        orig = (server.game, server.SAVE_DIR, server.SAVE_PREFIX, server.PLAYTHROUGH, server.LOAD_START_S)
        self.addCleanup(lambda: (setattr(server, "game", orig[0]), setattr(server, "SAVE_DIR", orig[1]),
                                 setattr(server, "SAVE_PREFIX", orig[2]), setattr(server, "PLAYTHROUGH", orig[3]),
                                 setattr(server, "LOAD_START_S", orig[4])))
        import tempfile
        d = Path(tempfile.mkdtemp())
        (d / "Claude-1__LIVETEST-se").mkdir()
        server.SAVE_DIR, server.SAVE_PREFIX, server.PLAYTHROUGH, server.LOAD_START_S = d, "Claude-", "t", 0.5

        class G(Fake):
            def game_state(self):
                return "Running"

            def server_ready(self):
                return True

            def run_job(self, op, args, context="server", wait_s=25.0, **kw):
                self.presses = getattr(self, "presses", 0) + 1
                return JobResult(status="done", result={"pressed": "Load"})

        g = G(["save_load"])
        server.game = g
        out = getattr(server.load_game, "fn", server.load_game)("LIVETEST-se")
        self.assertIn("didn't start a load", out)
        self.assertEqual(g.presses, 1 + server.LOAD_RETRIES)  # pressed again before giving up
        self.assertEqual(g.keys, ["ESCAPE", "ESCAPE"])  # the page is closed only at the end



class LoadVerified(unittest.TestCase):
    """load_game is done only when a load really happened: a new server Lua state and no Load page."""

    def run_load(self, boots, screens, states=None):
        orig = (server.game, server.SAVE_DIR, server.SAVE_PREFIX, server.PLAYTHROUGH, server.time.sleep,
                server.LOAD_START_S)
        self.addCleanup(lambda: (setattr(server, "game", orig[0]), setattr(server, "SAVE_DIR", orig[1]),
                                 setattr(server, "SAVE_PREFIX", orig[2]), setattr(server, "PLAYTHROUGH", orig[3]),
                                 setattr(server.time, "sleep", orig[4]), setattr(server, "LOAD_START_S", orig[5])))
        server.LOAD_START_S = 0.3
        import tempfile
        d = Path(tempfile.mkdtemp())
        (d / "Claude-1__LIVETEST-se").mkdir()
        server.SAVE_DIR, server.SAVE_PREFIX, server.PLAYTHROUGH = d, "Claude-", "t"
        server.time.sleep = lambda s: None
        boots = list(boots)
        import itertools
        states = itertools.chain(states or ["LoadSession"], itertools.repeat("Running"))

        class G(Fake):
            def rpc(self, op, args=None, context="server", timeout=12.0):
                if op == "sys.ping":
                    return {"boot": boots.pop(0) if len(boots) > 1 else boots[0]}
                return Fake.rpc(self, op, args, context)

            try_rpc = rpc

            def game_state(self):
                return next(states)

            def server_ready(self):
                return True

            def run_job(self, op, args, context="server", wait_s=25.0, **kw):
                self.presses = getattr(self, "presses", 0) + 1
                return JobResult(status="done", result={"pressed": "Load"})

        self.g = G(screens)
        server.game = self.g
        return getattr(server.load_game, "fn", server.load_game)("LIVETEST-se")

    def test_new_boot_is_done(self):
        self.assertIn("load_game: done", self.run_load(["a", "b"], ["game"]))

    def test_same_boot_fails(self):
        out = self.run_load(["a", "a"], ["game"])
        self.assertIn("nothing was loaded", out)

    def test_load_page_still_up_fails(self):
        out = self.run_load(["a", "b"], ["save_load"])
        self.assertIn("nothing was loaded", out)  # page still up, then a 2nd press that didn't start either
        self.assertEqual(self.g.presses, 1 + server.LOAD_RETRIES)
        self.assertEqual(self.g.keys, ["ESCAPE", "ESCAPE"])

    def test_press_that_did_nothing_is_pressed_again(self):
        # Acceptance 12:13: the first press left the Load page up (same Lua state); the second press loaded.
        # ui.state reads: 1st attempt 10x save_load (gives up), then game; boots: before, same, new.
        out = self.run_load(["a", "b"], ["save_load"] * 10 + ["game"],
                            states=["LoadSession", "Running", "LoadSession"])
        self.assertIn("load_game: done", out)
        self.assertEqual(self.g.presses, 2)
        self.assertEqual(self.g.keys, [])


if __name__ == "__main__":
    unittest.main()

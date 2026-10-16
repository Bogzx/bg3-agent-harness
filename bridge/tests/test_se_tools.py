"""feed_gale; press_key ESCAPE: refused on the plain game view (S021/S026 it opened the game menu), menu closed again if it opens."""
from __future__ import annotations

import sys
import time
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_turn_macros import P1, Base  # noqa: E402,F401  (sets BG3AI_RUNTIME before importing server)
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


class SaveLoadHatches(unittest.TestCase):
    """Audit UI-3: ui_action / press_key don't save or load around save_game / load_game."""

    def use(self):
        fake = Fake(["game"])
        orig = server.game
        server.game = fake
        self.addCleanup(setattr, server, "game", orig)
        return fake

    def test_ui_action_quickload_refused_before_the_game(self):
        fake = self.use()
        for cmd in ("QuickLoadCommand", "QuickSaveCommand", "OpenLoadGameDialog", "ContinueGameCommand",
                    "DeleteSavegameCommand", "Overwrite"):
            out = getattr(server.ui_action, "fn", server.ui_action)(cmd, "*")
            self.assertTrue(out.startswith("REFUSED"), (cmd, out))
        self.assertEqual(fake.keys, [])

    def test_ui_action_other_commands_pass(self):
        self.assertIsNone(server._saveload_word("UserTriggeredNextPhaseCommand"))
        self.assertIsNone(server._saveload_word("SelectAnswerCommand"))
        self.assertIsNone(server._saveload_word("TakeAllCommand"))

    def test_f8_refused(self):
        fake = self.use()
        out = getattr(server.press_key, "fn", server.press_key)("f8")
        self.assertTrue(out.startswith("REFUSED"), out)
        self.assertEqual(fake.keys, [])

    def test_f5_is_save_game(self):
        fake = self.use()
        calls = []
        with unittest.mock.patch.object(server, "save_game", lambda label="": calls.append(label) or "save_game: queued"):
            out = getattr(server.press_key, "fn", server.press_key)("F5")
        self.assertEqual(calls, [""])
        self.assertIn("save_game: queued", out)
        self.assertEqual(fake.keys, [])



class ChooseBelowTheFold(unittest.TestCase):
    """Audit UI-4: the mod refuses a rows-only option outside the rendered rows (needs_scroll); the bridge wheels the
    row into view, finds it by its text and chooses that row with the text for the mod to check."""

    class G:
        def __init__(self):
            self.jobs = []

        def rpc(self, op, args=None, context="server"):
            if op == "ui.state":
                return {"screen": "dialogue", "dialogue": {"text": "line", "options": [
                    {"i": k, "text": f"answer {k}"} for k in range(1, 7)], "more_below": 4}}
            return {}

        try_rpc = rpc

        def run_job(self, op, args, context="server", wait_s=25.0, **kw):
            self.jobs.append(dict(args))
            if "text" not in args:
                raise GameError("option 8 is not among the visible rows 1-6 of 10 (needs_scroll)")
            return JobResult(status="done", result={"screen": "game"})

    def test_scrolls_and_chooses_by_text(self):
        g = self.G()
        full = {"dialogue": {"options": [{"i": k, "text": f"answer {k}"} for k in range(1, 11)]}}
        from bg3ai import clicker
        with unittest.mock.patch.object(server, "game", g), \
                unittest.mock.patch.object(server, "_complete_answers", lambda ui: full), \
                unittest.mock.patch.object(server, "_inp", lambda: object()), \
                unittest.mock.patch.object(clicker, "scroll_to_answer", lambda game, inp, read, text, up=False: (8, None)):
            r = server._choose_answer(8)
        self.assertEqual(r.status, "done")
        self.assertEqual(g.jobs, [{"i": 8}, {"i": 8, "text": "answer 8"}])

    def test_other_errors_pass_through(self):
        g = self.G()
        g.run_job = lambda *a, **k: (_ for _ in ()).throw(GameError("option 3 is disabled"))
        with unittest.mock.patch.object(server, "game", g):
            with self.assertRaises(GameError):
                server._choose_answer(3)


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

    def test_camp_area_block_answers_before_pressing(self):
        # S035 19:27:30 / 19:38:58: camp() in the Gauntlet of Shar took 12 s each to fail with (Location).
        fake = NightFake()
        fake.night = False
        base = fake.rpc

        def rpc(op, args=None, context="server"):
            if op == "ui.travel_block":
                return {"hud": True, "error": "Location"}
            return base(op, args, context)

        fake.rpc = fake.try_rpc = rpc
        self.use(fake)
        out = getattr(server.camp, "fn", server.camp)()
        self.assertIn("camp: FAILED — cannot travel right now", out)
        self.assertIn("(Location): this area does not let the party go to camp", out)
        self.assertNotIn("act.go_to_camp", [o for o, _ in fake.jobs])

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


def _game_alive(test):
    """The game process "runs" for this test: load_game's retry stops when no bg3 process is left, and the real
    check reads this machine's /proc (red whenever the game happened to be down, 2026-10-10 21:00)."""
    orig = server._game_process_alive
    test.addCleanup(setattr, server, "_game_process_alive", orig)
    server._game_process_alive = lambda: True


class WorldUuid(unittest.TestCase):
    """S035 19:37: click_on("Umbral Gem") was "ambiguous: e55, e257", both gems in party bags."""

    def use(self, inv, nearby):
        test = self

        class G:
            def try_rpc(self, op, args=None, context="server"):
                return {"state.inventory": {"items": inv}, "state.nearby": nearby}.get(op)
        orig = (server.game, server.H)
        server.game, server.H = G(), server.Handles()
        self.addCleanup(lambda: (setattr(server, "game", orig[0]), setattr(server, "H", orig[1])))
        return test

    def test_carried_namesakes_skipped(self):
        self.use([{"uuid": "g1", "name": "Umbral Gem", "owner": P1}, {"uuid": "g2", "name": "Umbral Gem", "owner": P1}],
                 {"items": [{"uuid": "g3", "name": "Umbral Gem"}]})
        server.H.get("g1", "Umbral Gem"), server.H.get("g2", "Umbral Gem"), server.H.get("g3", "Umbral Gem")
        self.assertEqual(server._world_uuid("Umbral Gem"), "g3")

    def test_only_carried_says_who_holds_it(self):
        self.use([{"uuid": "g1", "name": "Umbral Gem", "owner": P1}], {"items": []})
        server.H.get("g1", "Umbral Gem")
        with self.assertRaises(ValueError) as cm:
            server._world_uuid("Umbral Gem")
        self.assertIn("the party carries it", str(cm.exception))


class ConfirmReward(unittest.TestCase):
    """S042 22:50:08: confirm("Accept") on the Reward panel failed "no confirmation box is open"."""

    def use(self, screen):
        calls = []

        class G:
            def try_rpc(self, op, args=None, context="server"):
                calls.append(op)
                return {"screen": screen} if op == "ui.state" else None

            def rpc(self, op, args=None, context="server"):
                calls.append(op)
                return {"closed": [{"page": "RewardPanel", "how": "GetRewardsAndClose"}]}

            def drain_events(self, ctx):
                return []
        orig = server.game
        server.game = G()
        self.addCleanup(setattr, server, "game", orig)
        return calls

    def test_accept_takes_the_reward(self):
        calls = self.use("reward")
        out = server.confirm("Accept")
        self.assertIn("ui.dismiss", calls)
        self.assertIn("confirm: done — the Reward panel", out)

    def test_no_leaves_it(self):
        calls = self.use("reward")
        orig = server._box_act
        server._box_act = lambda *a, **k: "FAILED: no confirmation box is open"
        self.addCleanup(setattr, server, "_box_act", orig)
        server.confirm("no")
        self.assertNotIn("ui.dismiss", calls)


class FastTravelReadyCheck(unittest.TestCase):
    """S032 17:23:43 / 17:24:57: `ready_failed:WaypointTravel_RegionSwap` came at once, fast_travel waited 66 s."""

    def run_with(self, fail: bool):
        import threading
        cancelled, keys = threading.Event(), []

        class G:
            event_seq = {"server": 5, "client": 0}
            port = 0

            def rpc(self, op, args=None, context="server", timeout=3.0):
                if op == "events.since":
                    evs = [{"seq": 6, "kind": "rest_flow", "data": {"step": "requested"}}]
                    if fail:
                        evs.append({"seq": 7, "kind": "rest_flow",
                                    "data": {"step": "ready_failed:WaypointTravel_RegionSwap"}})
                    return {"latest": evs[-1]["seq"], "events": [e for e in evs if e["seq"] > args["seq"]]}
                if op == "job.cancel":
                    assert (args["id"], context) == (7, "client")
                    cancelled.set()
                return {}

            def try_rpc(self, op, args=None, context="server"):
                if op == "ui.key":
                    keys.append(args["key"])
                return {"screen": "map"} if op == "ui.state" else {}

            def run_job(self, op, args, context="server", wait_s=25.0, on_start=None, **kw):
                on_start(context, 7)
                if cancelled.wait(3.0 if fail else 0.6):
                    return JobResult("interrupted", None, "cancelled")
                return JobResult("done", {"arrived": "Moonrise Towers"})

            def drain_events(self, ctx):
                return []
        g = G()
        saved = (server.game, server._watch_game, server._leave_camp_first, server._travel_block)
        self.addCleanup(lambda: (setattr(server, "game", saved[0]), setattr(server, "_watch_game", saved[1]),
                                 setattr(server, "_leave_camp_first", saved[2]), setattr(server, "_travel_block", saved[3])))
        server.game, server._watch_game = g, g
        server._leave_camp_first = lambda: ""
        server._travel_block = lambda *a, **k: {}
        t0 = time.monotonic()
        out = server.fast_travel("Waukeen's Rest")
        return out, time.monotonic() - t0, keys

    def test_ready_check_failure_ends_the_wait(self):
        out, took, keys = self.run_with(fail=True)
        self.assertIn("FAILED — the game's party ready check for the trip failed (travel to another region)", out)
        self.assertNotIn("WaypointTravel_RegionSwap", out)
        self.assertLess(took, 2.5)
        self.assertEqual(keys, ["ESCAPE"])  # the map the job opened is closed

    def test_success_untouched(self):
        out, _, keys = self.run_with(fail=False)
        self.assertIn("fast_travel: done", out)
        self.assertEqual(keys, [])

    def test_check_ignores_other_steps(self):
        w = server._ReadyCheckWatch()
        w._seq = 0
        self.assertFalse(w.check({"events": [{"seq": 1, "kind": "rest_flow", "data": {"step": "ready_ok:X"}}]}))
        self.assertTrue(w.check({"events": [{"seq": 2, "kind": "rest_flow", "data": {"step": "ready_failed:Y"}}]}))
        self.assertEqual((w.failed, w._seq), ("Y", 2))


class GameProcessAlive(unittest.TestCase):
    """load_game's crash fast path: the Steam runtime's ~60 MB `bg3` parent alone is no running game."""

    def _proc(self, procs):
        import tempfile
        d = Path(tempfile.mkdtemp())
        for pid, comm, mb in procs:
            (d / str(pid)).mkdir()
            (d / str(pid) / "comm").write_text(comm + "\n")
            (d / str(pid) / "status").write_text(f"Name:\t{comm}\n" + (f"VmRSS:\t{mb * 1024} kB\n" if mb is not None else ""))
        (d / "self").mkdir()
        return d

    def test_parent_and_game(self):
        self.assertTrue(server._game_process_alive(self._proc([(10, "bg3", 60), (11, "bg3", 4800), (12, "bash", 5)])))

    def test_lingering_parent_only(self):
        self.assertFalse(server._game_process_alive(self._proc([(10, "bg3", 60), (12, "bash", 5)])))

    def test_zombie_game_child(self):
        self.assertFalse(server._game_process_alive(self._proc([(10, "bg3", 60), (11, "bg3", None)])))

    def test_no_proc(self):
        self.assertIsNone(server._game_process_alive(Path("/nonexistent-proc")))


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
        _game_alive(self)

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
        _game_alive(self)
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


class InsertFake:
    """ui.insert: no panel until act.interact opens it; lists one book; inserting reports Success."""
    BOOK = "cccccccc-0000-0000-0000-0000000000b5"
    ME = "cccccccc-0000-0000-0000-0000000000a1"

    def __init__(self, open_=False, state="Success"):
        self.open, self.state, self.jobs = open_, state, []

    def rpc(self, op, args=None, context="server"):
        if op == "state.inventory":
            return {"items": [{"uuid": self.BOOK, "name": "Teachings of Loss: The Nightsinger", "owner": self.ME}]}
        if op == "state.status":
            return {"active": self.ME}
        return {}

    try_rpc = rpc

    def run_job(self, op, args, context="server", wait_s=25.0, **kw):
        self.jobs.append((op, dict(args), context))
        if op == "act.interact":
            self.open = True
            return JobResult(status="done", result={})
        if op == "ui.insert":
            if not self.open:
                return JobResult(status="failed", error="no Insert Item panel is open: interact with the pedestal/socket opens it")
            if not args.get("item"):
                return JobResult(status="done", result={"panel": "Riddle of the Night", "state": "Empty", "slots": [False],
                                                         "items": [{"uuid": self.BOOK, "name": "Teachings of Loss: The Nightsinger"}]})
            return JobResult(status="done", result={"item": "Teachings of Loss: The Nightsinger", "state": self.state,
                                                     "fail_reason": "Invalid" if self.state == "Fail" else None})
        return JobResult(status="done", result={})

    def drain_events(self, ctx):
        return []


class InsertItem(unittest.TestCase):
    def use(self, fake):
        orig = server.game
        server.game = fake
        self.addCleanup(setattr, server, "game", orig)
        server.H.get(InsertFake.ME, "Claude")
        self.ped = server.H.get("cccccccc-0000-0000-0000-0000000000c1", "Riddle of the Night")
        self.book = server.H.get(InsertFake.BOOK, "Teachings of Loss: The Nightsinger")
        return fake

    def call(self, **kw):
        return getattr(server.insert_item, "fn", server.insert_item)(**kw)

    def test_opens_the_panel_by_the_carrier_and_inserts(self):
        fake = self.use(InsertFake())
        out = self.call(item=self.book, into=self.ped)
        ops = [(o, a) for o, a, _ in fake.jobs]
        self.assertEqual(ops[0], ("ui.insert", {}))
        self.assertEqual(ops[1][0], "act.interact")
        self.assertEqual(ops[1][1]["char"], InsertFake.ME)
        self.assertIn(("ui.insert", {"item": InsertFake.BOOK}), ops)
        self.assertTrue(all(c == "client" for o, _, c in fake.jobs if o == "ui.insert"))
        self.assertIn("insert_item: done — inserted Teachings of Loss: The Nightsinger into Riddle of the Night", out)

    def test_no_item_lists_what_the_panel_takes(self):
        fake = self.use(InsertFake(open_=True))
        out = self.call()
        self.assertIn("INSERT panel Riddle of the Night: slot(s) empty", out)
        self.assertIn(f"{self.book} Teachings of Loss: The Nightsinger", out)
        self.assertEqual([o for o, _, _ in fake.jobs], ["ui.insert"])

    def test_no_panel_and_no_target_says_how(self):
        self.use(InsertFake())
        out = self.call(item=self.book)
        self.assertIn("pass into=", out)

    def test_refused_insert_is_a_failure_with_the_reason(self):
        self.use(InsertFake(open_=True, state="Fail"))
        out = self.call(item=self.book)
        self.assertIn("FAILED — the panel refused Teachings of Loss: The Nightsinger (Invalid)", out)

    def test_insert_by_another_member_takes_control_first(self):
        # S037 20:15-20:17: who=Shadowheart while Claude was controlled: the interact "used" the pedestal, no panel
        # (the client shows it only for the controlled character); select + click opened it.
        sh = "cccccccc-0000-0000-0000-0000000000a3"

        class Fake(InsertFake):
            active = InsertFake.ME

            def rpc(self, op, args=None, context="server"):
                if op == "state.inventory":
                    return {"items": [{"uuid": self.BOOK, "name": "Teachings of Loss: The Nightsinger", "owner": sh}]}
                if op == "state.status":
                    return {"active": self.active}
                return {}

            try_rpc = rpc

            def run_job(self, op, args, context="server", wait_s=25.0, **kw):
                if op == "act.select":
                    self.jobs.append((op, dict(args), context))
                    self.active = args["char"]
                    return JobResult(status="done", result={})
                if op == "act.interact" and args["char"] != self.active:
                    self.jobs.append((op, dict(args), context))
                    return JobResult(status="done", result={})   # used, but the panel shows on nobody's screen
                return super().run_job(op, args, context, wait_s, **kw)

        h = server.H.get(sh, "Shadowheart")
        fake = self.use(Fake())
        out = self.call(item=self.book, into=self.ped, who=h)
        ops = [o for o, _, _ in fake.jobs]
        self.assertLess(ops.index("act.select"), ops.index("act.interact"))
        self.assertIn("insert_item: done — inserted", out)
        self.assertIn("took control of Shadowheart", out)

    def test_insert_into_an_item_named_before_any_read(self):
        # S037 20:15: into="Riddle of the Night" right after a travel: unknown until a get_nearby. One nearby read
        # (characters + items in sight) learns it.
        ped = "cccccccc-0000-0000-0000-0000000000c9"

        class Fake(InsertFake):
            def rpc(self, op, args=None, context="server"):
                if op == "state.nearby":
                    self.nearby_kinds = (args or {}).get("kinds")
                    return {"items": [{"uuid": ped, "name": "Riddle of the Tide"}]}
                return super().rpc(op, args, context)

            try_rpc = rpc

        fake = self.use(Fake())
        self.assertEqual(server._uuid("Riddle of the Tide"), ped)
        self.assertIn("items", fake.nearby_kinds)

    class AltarFake(InsertFake):
        """S037 20:18-20:20: the Pedestal of Reckoning (PED) talks; the slot is the Ancient Altar (ALTAR) at its spot."""
        PED = "cccccccc-0000-0000-0000-0000000000d1"
        ALTAR = "cccccccc-0000-0000-0000-0000000000d2"

        def rpc(self, op, args=None, context="server"):
            if op == "state.nearby":
                return {"items": [{"uuid": self.PED, "name": "Pedestal of Reckoning", "kind": "usable", "pos": [-717.7, 3.6, -777.3]},
                                  {"uuid": self.ALTAR, "name": "Ancient Altar", "kind": "usable", "pos": [-717.5, 3.6, -777.0]},
                                  {"uuid": "cccccccc-0000-0000-0000-0000000000d3", "name": "Traversal Gem", "kind": "usable", "pos": [-710.0, 3.6, -770.0]}]}
            return super().rpc(op, args, context)

        try_rpc = rpc

        def run_job(self, op, args, context="server", wait_s=25.0, **kw):
            if op == "act.interact" and args["target"] == self.PED:
                self.jobs.append((op, dict(args), context))
                return JobResult(status="done", result={})
            return super().run_job(op, args, context, wait_s, **kw)

    def test_insert_tries_the_object_at_the_same_spot(self):
        fake = self.use(self.AltarFake())
        ped = server.H.get(self.AltarFake.PED, "Pedestal of Reckoning")
        out = self.call(item=self.book, into=ped)
        targets = [a.get("target") for o, a, _ in fake.jobs if o == "act.interact"]
        self.assertEqual(targets, [self.AltarFake.PED, self.AltarFake.ALTAR])
        self.assertIn("insert_item: done — inserted", out)
        self.assertIn(f"the slot is on {server.H.get(self.AltarFake.ALTAR)} Ancient Altar", out)

    def test_insert_that_opens_a_conversation_says_so_and_names_the_neighbour(self):
        fake = self.use(self.AltarFake())
        ped = server.H.get(self.AltarFake.PED, "Pedestal of Reckoning")
        orig = server._act

        def act(name, op, args, **kw):
            if op == "act.interact" and args.get("target") == self.AltarFake.PED:
                fake.jobs.append((op, dict(args), "server"))
                return "interact: done — used\nDIALOGUE Shadowheart: We have all the gems\n  1. Examine the altar."
            return orig(name, op, args, **kw)

        server._act = act
        self.addCleanup(setattr, server, "_act", orig)
        t = time.monotonic()
        out = self.call(item=self.book, into=ped)
        self.assertLess(time.monotonic() - t, 2.0)   # no 4 s poll for a panel once a conversation is up
        self.assertIn("opened a conversation instead", out)
        self.assertIn("1. Examine the altar.", out)
        self.assertIn(f"insert_item(into={server.H.get(self.AltarFake.ALTAR)})", out)
        self.assertEqual([a.get("target") for o, a, _ in fake.jobs if o == "act.interact"], [self.AltarFake.PED])

    def test_insert_that_closes_the_panel_says_closed(self):
        class Fake(InsertFake):
            def run_job(self, op, args, context="server", wait_s=25.0, **kw):
                r = super().run_job(op, args, context, wait_s, **kw)
                if op == "ui.insert" and args.get("item"):
                    self.open = False   # the story pedestal took it and vanished
                return r

        self.use(Fake(open_=True))
        out = self.call(item=self.book)
        self.assertIn("(panel closed)", out)
        self.assertNotIn("ESCAPE", out)

    def test_partial_fill_of_a_multi_slot_panel_is_done(self):
        # S037 20:23: the Verge altar's 3 gem slots: gems 1 and 2 sat in their slots but read "FAILED — didn't make
        # the panel ready" with the whole bag listed; the mod now answers placed/filled/slots_total.
        class Fake(InsertFake):
            def run_job(self, op, args, context="server", wait_s=25.0, **kw):
                if op == "ui.insert" and args.get("item"):
                    self.jobs.append((op, dict(args), context))
                    return JobResult(status="done", result={"placed": "Teachings of Loss: The Nightsinger", "state": "Preparing",
                                                            "filled": 1, "slots_total": 3, "panel_open": True})
                return super().run_job(op, args, context, wait_s, **kw)

        self.use(Fake(open_=True))
        out = self.call(item=self.book)
        self.assertIn("insert_item: done — put Teachings of Loss: The Nightsinger in the slot (1 of 3 filled", out)
        self.assertIn("2 more item(s)", out)
        self.assertNotIn("FAILED", out)
        self.assertNotIn("ESCAPE", out)

    def test_insert_screen_hint(self):
        self.assertIn("insert_item", server._input_screen_text("insert"))


class WorldTarget(unittest.TestCase):
    """S035 19:37: click_on("Umbral Gem") for the gem on the trial floor failed 'ambiguous: e55, e257' (both carried)."""
    SH, ME = "11111111-0000-0000-0000-00000000aa01", "11111111-0000-0000-0000-00000000aa02"
    G1, G2, G3 = ("11111111-0000-0000-0000-00000000ab01", "11111111-0000-0000-0000-00000000ab02",
                  "11111111-0000-0000-0000-00000000ab03")

    class G:
        def __init__(self, items, nearby=()):
            self.items, self.nearby, self.ops = items, list(nearby), []

        def rpc(self, op, args=None, context="server"):
            self.ops.append(op)
            if op == "state.inventory":
                return {"items": self.items}
            if op == "state.nearby":
                return {"items": self.nearby}
            return {}

        try_rpc = rpc

        def drain_events(self, ctx="server"):
            return []

    def use(self, g):
        saved, server.game = server.game, g
        self.addCleanup(setattr, server, "game", saved)
        return g

    def setUp(self):
        self.name = f"Umbral Gem {self._testMethodName}"   # H is process-wide: a name of our own
        server.H.get(self.SH, "Holder Shadowheart")   # unique names: H is shared, "Shadowheart" is taken elsewhere
        server.H.get(self.ME, "Holder Claude")
        self.carried = [{"uuid": self.G1, "name": self.name, "owner": self.SH},
                        {"uuid": self.G2, "name": self.name, "owner": self.ME}]
        server.H.get(self.G1, self.name)
        server.H.get(self.G2, self.name)

    def test_carried_namesakes_skipped_for_the_one_on_the_floor(self):
        server.H.get(self.G3, self.name)
        self.use(self.G(self.carried))
        self.assertEqual(server._world_uuid(self.name), self.G3)

    def test_only_carried_known_reads_nearby_once(self):
        g = self.use(self.G(self.carried, nearby=[{"uuid": self.G3, "name": self.name}]))
        self.assertEqual(server._world_uuid(self.name), self.G3)
        self.assertEqual(g.ops.count("state.nearby"), 1)

    def test_none_in_sight_says_who_carries_it(self):
        self.use(self.G(self.carried))
        with self.assertRaises(ValueError) as cm:
            server._world_uuid(self.name)
        msg = str(cm.exception)
        self.assertIn("party carries it", msg)
        self.assertIn("by Holder Shadowheart", msg)
        self.assertIn("by Holder Claude", msg)
        self.assertNotIn("ambiguous", msg)

    def test_handle_is_taken_as_given(self):
        g = self.use(self.G(self.carried))
        h = server.H.get(self.G2)
        self.assertEqual(server._world_uuid(h), self.G2)
        self.assertEqual(g.ops, [])

    def test_interact_keeps_a_unique_carried_item(self):
        g = self.use(self.G(self.carried))
        u = "11111111-0000-0000-0000-00000000ab09"
        server.H.get(u, f"Book {self._testMethodName}")
        self.assertEqual(server._interact_uuid(f"Book {self._testMethodName}"), u)
        self.assertEqual(g.ops, [])

    def test_click_on_resolves_to_the_world_gem(self):
        server.H.get(self.G3, self.name)
        self.use(self.G(self.carried))
        seen = {}
        saved_inp, server._inp = server._inp, lambda: object()
        self.addCleanup(setattr, server, "_inp", saved_inp)
        import bg3ai.clicker as clicker

        class CR:
            ok, reason, screen = True, "", "game"

            def as_dict(self):
                return {}

        saved_ce = clicker.click_entity
        clicker.click_entity = lambda game, inp, u, b: seen.setdefault("u", u) and CR()
        self.addCleanup(setattr, clicker, "click_entity", saved_ce)
        out = server.click_on(self.name)
        self.assertEqual(seen["u"], self.G3, out)


class LeftTrade(Base):
    """S037 20:25:57: walk_toward right after trade(confirm) → "character is in a conversation"; the trade window was
    still up. The action closes it (ESCAPE, as walking off does for a player) and runs."""

    def setUp(self):
        super().setUp()
        self.g.ui = {"screen": "trade"}
        orig = self.g.rpc

        def rpc(op, args=None, context="server", timeout=12.0):
            if op == "ui.key" and (args or {}).get("key") == "ESCAPE":
                self.g.ui = {"screen": "game"}
            return orig(op, args, context, timeout)

        self.g.rpc = rpc
        self.g.refuse_if = lambda op, args: ("character is in a conversation (dialog 8187 res=e489)"
                                             if op == "act.move_to" and self.g.ui.get("screen") == "trade" else None)

    def test_action_refused_by_a_left_trade_closes_it_and_runs(self):
        out = server._act("move_to", "act.move_to", {"char": P1, "pos": [5, 0, 0]})
        self.assertIn("(closed the trade window first)", out)
        self.assertNotIn("FAILED", out)
        self.assertEqual(len(self.g.ops("act.move_to")), 2)

    def test_a_real_conversation_is_left_alone(self):
        self.g.ui = {"screen": "dialogue"}
        self.g.refuse_if = lambda op, args: "character is in a conversation (InDialog)" if op == "act.move_to" else None
        out = server._act("move_to", "act.move_to", {"char": P1, "pos": [5, 0, 0]})
        self.assertIn("in a conversation", out)
        self.assertEqual([c for c in self.g.calls if c[0] == "ui.key"], [])


class CampAfterClosingTalk(Base):
    """S057 07:40:25: camp() right after a trade closed failed 'cannot do that during a conversation' ×2 (the trader's
    goodbye line). _act's conversation retry (CONVO_WAIT_S, no dialogue on screen) covers camp's refusal text too."""

    def setUp(self) -> None:
        super().setUp()
        saved = (server._travel_block, server.CONVO_WAIT_S)
        server._travel_block = lambda *a, **k: {}
        self.addCleanup(lambda: (setattr(server, "_travel_block", saved[0]), setattr(server, "CONVO_WAIT_S", saved[1])))
        self.jobs, self.fails, self.screen = [], [2], "game"
        orig = self.g.try_rpc

        def try_rpc(op, args=None, context="server", **kw):
            if op == "ui.state":
                return {"screen": self.screen}
            return orig(op, args, context=context, **kw)
        self.g.try_rpc = try_rpc

        def run_job(op, args, context="server", wait_s=25.0, **kw):
            self.jobs.append(op)
            if op == "act.go_to_camp" and self.fails[0] > 0:
                self.fails[0] -= 1
                return JobResult(status="failed", error="cannot do that during a conversation")
            return JobResult(status="done", result={"at_camp": True})
        self.g.run_job = run_job

    def test_waits_for_the_goodbye_line_then_goes(self):
        with unittest.mock.patch.object(server.time, "sleep"):
            out = server.camp()
        self.assertNotIn("FAILED", out)
        self.assertEqual(self.jobs.count("act.go_to_camp"), 3)

    def test_open_dialogue_window_fails_at_once(self):
        self.screen = "dialogue"
        out = server.camp()
        self.assertIn("during a conversation", out)
        self.assertEqual(self.jobs.count("act.go_to_camp"), 1)


class ScreenshotMap(Base):
    """screenshot(map=true): open the map, capture, close it (S055/S056: M + screenshot + ESCAPE by hand)."""

    def setUp(self) -> None:
        super().setUp()
        from PIL import Image as PILImage
        order = self.order = []

        class Cap:
            def capture(self, max_width):
                order.append("capture")
                return b"jpg", PILImage.new("RGB", (8, 8))
        saved = (server._screen, server.MAP_SHOT_SETTLE_S)
        server._screen, server.MAP_SHOT_SETTLE_S = Cap(), 0.0
        self.addCleanup(lambda: (setattr(server, "_screen", saved[0]), setattr(server, "MAP_SHOT_SETTLE_S", saved[1])))
        self.opened = True
        orig = self.g.try_rpc

        def try_rpc(op, args=None, context="server", **kw):
            if op == "ui.map_close":
                order.append("close")
                return {"closed": True}
            return orig(op, args, context=context, **kw)
        self.g.try_rpc = try_rpc
        self.status = "done"

        def run_job(op, args, context="server", wait_s=25.0, **kw):
            order.append(op)
            if self.status != "done":
                return JobResult(status="failed", error="the map didn't open (pressed M,M over 3.0s)")
            return JobResult(status="done", result={"opened": self.opened, "map_presses": 1})
        self.g.run_job = run_job

    def fn(self, **kw):
        f = getattr(server.screenshot, "fn", server.screenshot)
        return f(**kw)

    def test_opens_captures_closes(self):
        out = self.fn(map=True)
        self.assertNotIsInstance(out, str)
        self.assertEqual(self.order, ["ui.map_show", "capture", "close"])

    def test_map_already_open_is_left_open(self):
        self.opened = False
        self.fn(map=True)
        self.assertEqual(self.order, ["ui.map_show", "capture"])

    def test_map_that_does_not_open_says_so(self):
        self.status = "failed"
        out = self.fn(map=True)
        self.assertIn("the map didn't open", out)
        self.assertNotIn("capture", self.order)

    def test_plain_screenshot_unchanged(self):
        self.fn()
        self.assertEqual(self.order, ["capture"])


class DoorOpenState(unittest.TestCase):
    """S054 05:54:51: 'Control Centre <door> 4.5m' gave no open/closed state; interact closed the open door."""

    def test_door_shows_open_or_closed(self):
        from bg3ai import render
        H = server.H
        n = {"items": [{"uuid": "d00r0000-0000-0000-0000-000000000001", "name": "Control Centre", "kind": "door",
                        "dist": 4.5, "open": True},
                       {"uuid": "d00r0000-0000-0000-0000-000000000002", "name": "Cell Door", "kind": "door",
                        "dist": 6.0, "closed": True, "locked": True}]}
        out = render.render_nearby(n, H)
        self.assertRegex(out, r"Control Centre <door> 4\.5m open")
        self.assertRegex(out, r"Cell Door <door> 6m locked closed")

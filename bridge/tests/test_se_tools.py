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
from bg3ai.render import Handles
from bg3ai.tools import core, reporting, plumbing, observe, world_tools, travel, dialogue, inventory, uitools, batch, queueing, macro_tools, saving  # noqa: E402,F401  (F401: test_se_tools_world.py imports from here)
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
        orig = core.game
        core.game = fake
        self.addCleanup(setattr, core, "game", orig)
        return fake

    def test_plain_game_view_refused(self):
        fake = self.use(["game"])
        out = uitools.press_key.fn("escape") if hasattr(uitools.press_key, "fn") else uitools.press_key("escape")
        self.assertTrue(out.startswith("REFUSED"), out)
        self.assertEqual(fake.keys, [])

    def test_open_panel_closes(self):
        fake = self.use([{"screen": "game", "panels_open": ["Inventory"]}, "game"])
        out = getattr(uitools.press_key, "fn", uitools.press_key)("ESCAPE")
        self.assertNotIn("REFUSED", out)
        self.assertEqual(fake.keys, ["ESCAPE"])

    def test_menu_that_opens_is_closed_again(self):
        fake = self.use(["trade", "game_menu"])
        out = getattr(uitools.press_key, "fn", uitools.press_key)("ESCAPE")
        self.assertEqual(fake.keys, ["ESCAPE", "RESUME:Resume"])
        self.assertIn("closed it again", out)

    def test_menu_that_stays_open_is_reported(self):
        """Live 12:1x (Karlach's menu): a second ESCAPE key left the game menu open while the result said
        'closed it again'. Resume first, then the key; if it is still up, say so, and that ESCAPE doesn't end a talk."""
        fake = self.use(["dialogue", "game_menu"])
        fake.resume_closes = False
        time.sleep, real = (lambda s: None), time.sleep
        self.addCleanup(setattr, time, "sleep", real)
        out = getattr(uitools.press_key, "fn", uitools.press_key)("ESCAPE")
        self.assertEqual(fake.keys, ["ESCAPE", "RESUME:Resume", "ESCAPE"])
        self.assertIn("STILL OPEN", out)
        self.assertNotIn("closed it again", out)
        self.assertIn("choose_dialogue", out)

    def test_escape_on_game_menu_closes_it_once(self):
        fake = self.use(["game_menu", "game"])
        getattr(uitools.press_key, "fn", uitools.press_key)("ESCAPE")
        self.assertEqual(fake.keys, ["ESCAPE"])

    def test_unknown_screen_presses(self):
        fake = self.use([None])
        fake.rpc = lambda op, args=None, context="server": (fake.keys.append(args["key"]) if op == "ui.key" else None)
        fake.try_rpc = fake.rpc
        getattr(uitools.press_key, "fn", uitools.press_key)("ESCAPE")
        self.assertEqual(fake.keys[0], "ESCAPE")

    def test_other_keys_untouched(self):
        fake = self.use(["game"])
        getattr(uitools.press_key, "fn", uitools.press_key)("space")
        self.assertEqual(fake.keys, ["SPACE"])


class SaveLoadHatches(unittest.TestCase):
    """Audit UI-3: ui_action / press_key don't save or load around save_game / load_game."""

    def use(self):
        fake = Fake(["game"])
        orig = core.game
        core.game = fake
        self.addCleanup(setattr, core, "game", orig)
        return fake

    def test_ui_action_quickload_refused_before_the_game(self):
        fake = self.use()
        for cmd in ("QuickLoadCommand", "QuickSaveCommand", "OpenLoadGameDialog", "ContinueGameCommand",
                    "DeleteSavegameCommand", "Overwrite"):
            out = getattr(uitools.ui_action, "fn", uitools.ui_action)(cmd, "*")
            self.assertTrue(out.startswith("REFUSED"), (cmd, out))
        self.assertEqual(fake.keys, [])

    def test_ui_action_other_commands_pass(self):
        self.assertIsNone(uitools._saveload_word("UserTriggeredNextPhaseCommand"))
        self.assertIsNone(uitools._saveload_word("SelectAnswerCommand"))
        self.assertIsNone(uitools._saveload_word("TakeAllCommand"))

    def test_f8_refused(self):
        fake = self.use()
        out = getattr(uitools.press_key, "fn", uitools.press_key)("f8")
        self.assertTrue(out.startswith("REFUSED"), out)
        self.assertEqual(fake.keys, [])

    def test_f5_is_save_game(self):
        fake = self.use()
        calls = []
        with unittest.mock.patch.object(saving, "save_game", lambda label="": calls.append(label) or "save_game: queued"):
            out = getattr(uitools.press_key, "fn", uitools.press_key)("F5")
        self.assertEqual(calls, ["quicksave"])
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
        with unittest.mock.patch.object(core, "game", g), \
                unittest.mock.patch.object(reporting, "_complete_answers", lambda ui: full), \
                unittest.mock.patch.object(world_tools, "_inp", lambda: object()), \
                unittest.mock.patch.object(clicker, "scroll_to_answer", lambda game, inp, read, text, up=False: (8, None)):
            r = dialogue._choose_answer(8)
        self.assertEqual(r.status, "done")
        self.assertEqual(g.jobs, [{"i": 8}, {"i": 8, "text": "answer 8"}])

    def test_other_errors_pass_through(self):
        g = self.G()
        g.run_job = lambda *a, **k: (_ for _ in ()).throw(GameError("option 3 is disabled"))
        with unittest.mock.patch.object(core, "game", g):
            with self.assertRaises(GameError):
                dialogue._choose_answer(3)


class FeedGale(unittest.TestCase):
    def test_item_by_handle_goes_to_ui_donate(self):
        fake = Fake(["donate"])
        orig = core.game
        core.game = fake
        self.addCleanup(setattr, core, "game", orig)
        u = "cccccccc-0000-0000-0000-000000000001"
        h = core.H.get(u, "Komira's Locket")
        out = getattr(inventory.feed_gale, "fn", inventory.feed_gale)(h)
        self.assertEqual(fake.jobs[0][:2], ("ui.donate", {"item": u}))
        self.assertEqual(fake.jobs[0][2], "client")
        self.assertIn("feed_gale: done", out)

    def test_donate_screen_hint(self):
        self.assertIn("feed_gale", batch._input_screen_text("donate"))



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
        orig = core.game
        core.game = fake
        self.addCleanup(setattr, core, "game", orig)
        orig_wait = travel._wait_running
        travel._wait_running = lambda max_s=60.0: True
        self.addCleanup(setattr, travel, "_wait_running", orig_wait)
        return fake

    def test_fast_travel_ends_the_night_first(self):
        fake = self.use(NightFake())
        out = getattr(travel.fast_travel, "fn", travel.fast_travel)("Last Light Inn")
        ops = [o for o, _ in fake.jobs]
        self.assertEqual(ops[0], "ui.long_rest")
        self.assertEqual(fake.jobs[0][1], {"sleep_only": True})
        self.assertIn("ui.fast_travel", ops)
        self.assertIn("ended the night at camp first", out)

    def test_night_that_wont_end_stops_travel(self):
        fake = self.use(NightFake(ends=False))
        out = getattr(travel.fast_travel, "fn", travel.fast_travel)("Last Light Inn")
        self.assertNotIn("ui.fast_travel", [o for o, _ in fake.jobs])
        self.assertIn("night at camp hasn't ended", out)

    def test_camp_leave_ends_the_night_first(self):
        fake = self.use(NightFake())
        out = getattr(travel.camp, "fn", travel.camp)("leave")
        self.assertEqual([o for o, _ in fake.jobs][:2], ["ui.long_rest", "act.leave_camp"])
        self.assertIn("ended the night", out)

    def test_no_night_no_rest(self):
        fake = NightFake()
        fake.night = False
        self.use(fake)
        getattr(travel.camp, "fn", travel.camp)("leave")
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
        go = getattr(travel.camp, "fn", travel.camp)
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
        out = getattr(travel.camp, "fn", travel.camp)()
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
        orig = time.sleep
        time.sleep = lambda s: None
        self.addCleanup(setattr, time, "sleep", orig)
        out = getattr(travel.fast_travel, "fn", travel.fast_travel)("Last Light Inn")
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
    orig = saving._game_process_alive
    test.addCleanup(setattr, saving, "_game_process_alive", orig)
    saving._game_process_alive = lambda: True


class WorldUuid(unittest.TestCase):
    """S035 19:37: click_on("Umbral Gem") was "ambiguous: e55, e257", both gems in party bags."""

    def use(self, inv, nearby):
        test = self

        class G:
            def try_rpc(self, op, args=None, context="server"):
                return {"state.inventory": {"items": inv}, "state.nearby": nearby}.get(op)
        orig = (core.game, core.H)
        core.game, core.H = G(), Handles()
        self.addCleanup(lambda: (setattr(core, "game", orig[0]), setattr(core, "H", orig[1])))
        return test

    def test_carried_namesakes_skipped(self):
        self.use([{"uuid": "g1", "name": "Umbral Gem", "owner": P1}, {"uuid": "g2", "name": "Umbral Gem", "owner": P1}],
                 {"items": [{"uuid": "g3", "name": "Umbral Gem"}]})
        core.H.get("g1", "Umbral Gem"), core.H.get("g2", "Umbral Gem"), core.H.get("g3", "Umbral Gem")
        self.assertEqual(plumbing._world_uuid("Umbral Gem"), "g3")

    def test_only_carried_says_who_holds_it(self):
        self.use([{"uuid": "g1", "name": "Umbral Gem", "owner": P1}], {"items": []})
        core.H.get("g1", "Umbral Gem")
        with self.assertRaises(ValueError) as cm:
            plumbing._world_uuid("Umbral Gem")
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
        orig = core.game
        core.game = G()
        self.addCleanup(setattr, core, "game", orig)
        return calls

    def test_accept_takes_the_reward(self):
        calls = self.use("reward")
        out = uitools.confirm("Accept")
        self.assertIn("ui.dismiss", calls)
        self.assertIn("confirm: done — the Reward panel", out)

    def test_no_leaves_it(self):
        calls = self.use("reward")
        orig = world_tools._box_act
        world_tools._box_act = lambda *a, **k: "FAILED: no confirmation box is open"
        self.addCleanup(setattr, world_tools, "_box_act", orig)
        uitools.confirm("no")
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
        saved = (core.game, queueing._watch_game, travel._leave_camp_first, travel._travel_block)
        self.addCleanup(lambda: (setattr(core, "game", saved[0]), setattr(queueing, "_watch_game", saved[1]),
                                 setattr(travel, "_leave_camp_first", saved[2]), setattr(travel, "_travel_block", saved[3])))
        core.game, queueing._watch_game = g, g
        travel._leave_camp_first = lambda: ""
        travel._travel_block = lambda *a, **k: {}
        t0 = time.monotonic()
        out = travel.fast_travel("Waukeen's Rest")
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
        w = travel._ReadyCheckWatch()
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
        self.assertTrue(saving._game_process_alive(self._proc([(10, "bg3", 60), (11, "bg3", 4800), (12, "bash", 5)])))

    def test_lingering_parent_only(self):
        self.assertFalse(saving._game_process_alive(self._proc([(10, "bg3", 60), (12, "bash", 5)])))

    def test_zombie_game_child(self):
        self.assertFalse(saving._game_process_alive(self._proc([(10, "bg3", 60), (11, "bg3", None)])))

    def test_no_proc(self):
        self.assertIsNone(saving._game_process_alive(Path("/nonexistent-proc")))


class LoadCrash(unittest.TestCase):
    def test_process_gone_fails_at_once(self):
        alive = iter([True] + [False] * 50)
        orig_pt = saving.PLAYTHROUGH
        saving.PLAYTHROUGH = "test"
        self.addCleanup(setattr, saving, "PLAYTHROUGH", orig_pt)
        orig = (core.game, saving._game_process_alive, core.SAVE_DIR, saving.SAVE_PREFIX, time.sleep)
        self.addCleanup(lambda: (setattr(core, "game", orig[0]), setattr(saving, "_game_process_alive", orig[1]),
                                 setattr(core, "SAVE_DIR", orig[2]), setattr(saving, "SAVE_PREFIX", orig[3]),
                                 setattr(time, "sleep", orig[4])))
        import tempfile
        d = Path(tempfile.mkdtemp())
        (d / "Claude-1__LIVETEST-se").mkdir()
        core.SAVE_DIR, saving.SAVE_PREFIX = d, "Claude-"
        saving._game_process_alive = lambda: next(alive)
        time.sleep = lambda s: None

        class G(Fake):
            def game_state(self):
                return "Loading"

            def server_ready(self):
                return False

            def run_job(self, op, args, context="server", wait_s=25.0, **kw):
                return JobResult(status="done", result={"loading": True})

        core.game = G(["game"])
        t0 = time.monotonic()
        out = getattr(saving.load_game, "fn", saving.load_game)("LIVETEST-se")
        self.assertLess(time.monotonic() - t0, 5)
        self.assertIn("CRASHED", out)

    def test_load_that_never_starts_fails_and_closes_the_page(self):
        orig = (core.game, core.SAVE_DIR, saving.SAVE_PREFIX, saving.PLAYTHROUGH, saving.LOAD_START_S)
        self.addCleanup(lambda: (setattr(core, "game", orig[0]), setattr(core, "SAVE_DIR", orig[1]),
                                 setattr(saving, "SAVE_PREFIX", orig[2]), setattr(saving, "PLAYTHROUGH", orig[3]),
                                 setattr(saving, "LOAD_START_S", orig[4])))
        import tempfile
        d = Path(tempfile.mkdtemp())
        (d / "Claude-1__LIVETEST-se").mkdir()
        core.SAVE_DIR, saving.SAVE_PREFIX, saving.PLAYTHROUGH, saving.LOAD_START_S = d, "Claude-", "t", 0.5
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
        core.game = g
        out = getattr(saving.load_game, "fn", saving.load_game)("LIVETEST-se")
        self.assertIn("didn't start a load", out)
        self.assertEqual(g.presses, 1 + saving.LOAD_RETRIES)  # pressed again before giving up
        self.assertEqual(g.keys, ["ESCAPE", "ESCAPE"])  # the page is closed only at the end



class LoadVerified(unittest.TestCase):
    """load_game is done only when a load really happened: a new server Lua state and no Load page."""

    def run_load(self, boots, screens, states=None):
        orig = (core.game, core.SAVE_DIR, saving.SAVE_PREFIX, saving.PLAYTHROUGH, time.sleep,
                saving.LOAD_START_S)
        self.addCleanup(lambda: (setattr(core, "game", orig[0]), setattr(core, "SAVE_DIR", orig[1]),
                                 setattr(saving, "SAVE_PREFIX", orig[2]), setattr(saving, "PLAYTHROUGH", orig[3]),
                                 setattr(time, "sleep", orig[4]), setattr(saving, "LOAD_START_S", orig[5])))
        saving.LOAD_START_S = 0.3
        _game_alive(self)
        import tempfile
        d = Path(tempfile.mkdtemp())
        (d / "Claude-1__LIVETEST-se").mkdir()
        core.SAVE_DIR, saving.SAVE_PREFIX, saving.PLAYTHROUGH = d, "Claude-", "t"
        time.sleep = lambda s: None
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
        core.game = self.g
        return getattr(saving.load_game, "fn", saving.load_game)("LIVETEST-se")

    def test_new_boot_is_done(self):
        self.assertIn("load_game: done", self.run_load(["a", "b"], ["game"]))

    def test_same_boot_fails(self):
        out = self.run_load(["a", "a"], ["game"])
        self.assertIn("nothing was loaded", out)

    def test_load_page_still_up_fails(self):
        out = self.run_load(["a", "b"], ["save_load"])
        self.assertIn("nothing was loaded", out)  # page still up, then a 2nd press that didn't start either
        self.assertEqual(self.g.presses, 1 + saving.LOAD_RETRIES)
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

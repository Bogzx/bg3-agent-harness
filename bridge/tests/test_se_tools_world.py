"""Offline tests: insert_item, world targets (click_on), the trade window left open, camp after a closing talk, the
screenshot map and door open states. Fixtures: test_se_tools.py."""

from __future__ import annotations

import time
import unittest
import unittest.mock
from pathlib import Path

from test_se_tools import (  # sets BG3AI_RUNTIME before importing bg3ai
    P1,
    Base,
    JobResult,
    batch,
    core,
    inventory,
    macro_tools,
    observe,
    plumbing,
    travel,
    world_tools,
)


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
        if op == "ui.selected":  # the HUD has the same member (A16: HUD-dependent selects read the client)
            return {"uuid": self.ME}
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
        orig = core.game
        core.game = fake
        self.addCleanup(setattr, core, "game", orig)
        core.H.get(InsertFake.ME, "Claude")
        self.ped = core.H.get("cccccccc-0000-0000-0000-0000000000c1", "Riddle of the Night")
        self.book = core.H.get(InsertFake.BOOK, "Teachings of Loss: The Nightsinger")
        return fake

    def call(self, **kw):
        return getattr(inventory.insert_item, "fn", inventory.insert_item)(**kw)

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

        h = core.H.get(sh, "Shadowheart")
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
        self.assertEqual(plumbing._uuid("Riddle of the Tide"), ped)
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
        ped = core.H.get(self.AltarFake.PED, "Pedestal of Reckoning")
        out = self.call(item=self.book, into=ped)
        targets = [a.get("target") for o, a, _ in fake.jobs if o == "act.interact"]
        self.assertEqual(targets, [self.AltarFake.PED, self.AltarFake.ALTAR])
        self.assertIn("insert_item: done — inserted", out)
        self.assertIn(f"the slot is on {core.H.get(self.AltarFake.ALTAR)} Ancient Altar", out)

    def test_insert_that_opens_a_conversation_says_so_and_names_the_neighbour(self):
        fake = self.use(self.AltarFake())
        ped = core.H.get(self.AltarFake.PED, "Pedestal of Reckoning")
        orig = plumbing._act

        def act(name, op, args, **kw):
            if op == "act.interact" and args.get("target") == self.AltarFake.PED:
                fake.jobs.append((op, dict(args), "server"))
                return "interact: done — used\nDIALOGUE Shadowheart: We have all the gems\n  1. Examine the altar."
            return orig(name, op, args, **kw)

        plumbing._act = act
        self.addCleanup(setattr, plumbing, "_act", orig)
        t = time.monotonic()
        out = self.call(item=self.book, into=ped)
        self.assertLess(time.monotonic() - t, 2.0)   # no 4 s poll for a panel once a conversation is up
        self.assertIn("opened a conversation instead", out)
        self.assertIn("1. Examine the altar.", out)
        self.assertIn(f"insert_item(into={core.H.get(self.AltarFake.ALTAR)})", out)
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
        self.assertIn("insert_item", batch._input_screen_text("insert"))


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
        saved, core.game = core.game, g
        self.addCleanup(setattr, core, "game", saved)
        return g

    def setUp(self):
        self.name = f"Umbral Gem {self._testMethodName}"   # H is process-wide: a name of our own
        core.H.get(self.SH, "Holder Shadowheart")   # unique names: H is shared, "Shadowheart" is taken elsewhere
        core.H.get(self.ME, "Holder Claude")
        self.carried = [{"uuid": self.G1, "name": self.name, "owner": self.SH},
                        {"uuid": self.G2, "name": self.name, "owner": self.ME}]
        core.H.get(self.G1, self.name)
        core.H.get(self.G2, self.name)

    def test_carried_namesakes_skipped_for_the_one_on_the_floor(self):
        core.H.get(self.G3, self.name)
        self.use(self.G(self.carried))
        self.assertEqual(plumbing._world_uuid(self.name), self.G3)

    def test_only_carried_known_reads_nearby_once(self):
        g = self.use(self.G(self.carried, nearby=[{"uuid": self.G3, "name": self.name}]))
        self.assertEqual(plumbing._world_uuid(self.name), self.G3)
        self.assertEqual(g.ops.count("state.nearby"), 1)

    def test_none_in_sight_says_who_carries_it(self):
        self.use(self.G(self.carried))
        with self.assertRaises(ValueError) as cm:
            plumbing._world_uuid(self.name)
        msg = str(cm.exception)
        self.assertIn("party carries it", msg)
        self.assertIn("by Holder Shadowheart", msg)
        self.assertIn("by Holder Claude", msg)
        self.assertNotIn("ambiguous", msg)

    def test_handle_is_taken_as_given(self):
        g = self.use(self.G(self.carried))
        h = core.H.get(self.G2)
        self.assertEqual(plumbing._world_uuid(h), self.G2)
        self.assertEqual(g.ops, [])

    def test_interact_keeps_a_unique_carried_item(self):
        g = self.use(self.G(self.carried))
        u = "11111111-0000-0000-0000-00000000ab09"
        core.H.get(u, f"Book {self._testMethodName}")
        self.assertEqual(plumbing._interact_uuid(f"Book {self._testMethodName}"), u)
        self.assertEqual(g.ops, [])

    def test_click_on_resolves_to_the_world_gem(self):
        core.H.get(self.G3, self.name)
        self.use(self.G(self.carried))
        seen = {}
        saved_inp, world_tools._inp = world_tools._inp, lambda: object()
        self.addCleanup(setattr, world_tools, "_inp", saved_inp)
        import bg3ai.clicker as clicker

        class CR:
            ok, reason, screen = True, "", "game"

            def as_dict(self):
                return {}

        saved_ce = clicker.click_entity
        clicker.click_entity = lambda game, inp, u, b: seen.setdefault("u", u) and CR()
        self.addCleanup(setattr, clicker, "click_entity", saved_ce)
        out = world_tools.click_on(self.name)
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
        out = plumbing._act("move_to", "act.move_to", {"char": P1, "pos": [5, 0, 0]})
        self.assertIn("(closed the trade window first)", out)
        self.assertNotIn("FAILED", out)
        self.assertEqual(len(self.g.ops("act.move_to")), 2)

    def test_a_real_conversation_is_left_alone(self):
        self.g.ui = {"screen": "dialogue"}
        self.g.refuse_if = lambda op, args: "character is in a conversation (InDialog)" if op == "act.move_to" else None
        out = plumbing._act("move_to", "act.move_to", {"char": P1, "pos": [5, 0, 0]})
        self.assertIn("in a conversation", out)
        self.assertEqual([c for c in self.g.calls if c[0] == "ui.key"], [])


class CampAfterClosingTalk(Base):
    """S057 07:40:25: camp() right after a trade closed failed 'cannot do that during a conversation' ×2 (the trader's
    goodbye line). _act's conversation retry (CONVO_WAIT_S, no dialogue on screen) covers camp's refusal text too."""

    def setUp(self) -> None:
        super().setUp()
        saved = (travel._travel_block, macro_tools.CONVO_WAIT_S)
        travel._travel_block = lambda *a, **k: {}
        self.addCleanup(lambda: (setattr(travel, "_travel_block", saved[0]), setattr(macro_tools, "CONVO_WAIT_S", saved[1])))
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
        with unittest.mock.patch.object(time, "sleep"):
            out = travel.camp()
        self.assertNotIn("FAILED", out)
        self.assertEqual(self.jobs.count("act.go_to_camp"), 3)

    def test_open_dialogue_window_fails_at_once(self):
        self.screen = "dialogue"
        out = travel.camp()
        self.assertIn("during a conversation", out)
        self.assertEqual(self.jobs.count("act.go_to_camp"), 1)

    def test_goodbye_line_on_a_dialogue_screen_is_waited_out(self):
        """Live window 2026-10-16 18:40: trade → ESCAPE → camp FAILED in 0.1 s: the goodbye line ("Take care now.")
        is a dialogue screen with no answers. Only answers on screen stop the retry."""
        self.screen = "dialogue"
        orig = self.g.try_rpc

        def try_rpc(op, args=None, context="server", **kw):
            if op == "ui.state":
                return {"screen": "dialogue", "dialogue": {"speaker": "Quartermaster Talli", "text": "Take care now.",
                                                           "listening": True}}
            return orig(op, args, context=context, **kw)
        self.g.try_rpc = try_rpc
        with unittest.mock.patch.object(time, "sleep"):
            out = travel.camp()
        self.assertNotIn("FAILED", out)
        self.assertEqual(self.jobs.count("act.go_to_camp"), 3)

    def test_answers_on_screen_still_fail_at_once(self):
        orig = self.g.try_rpc
        self.g.try_rpc = lambda op, args=None, context="server", **kw: (
            {"screen": "dialogue", "dialogue": {"options": [{"i": 1, "text": "Leave."}]}} if op == "ui.state"
            else orig(op, args, context=context, **kw))
        out = travel.camp()
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
        saved = (core._screen, observe.MAP_SHOT_SETTLE_S)
        core._screen, observe.MAP_SHOT_SETTLE_S = Cap(), 0.0
        self.addCleanup(lambda: (setattr(core, "_screen", saved[0]), setattr(observe, "MAP_SHOT_SETTLE_S", saved[1])))
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
        f = getattr(observe.screenshot, "fn", observe.screenshot)
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

    def test_captures_where_the_game_writes(self):
        # live 20261017-145354: acceptance.py's bridge ran on runtime/acceptance/rt and waited for the PNG in
        # rt/shots while the game wrote into runtime/shots: "game did not present a frame in time"
        made = []
        core._screen = None
        with unittest.mock.patch.dict("os.environ", {"BG3AI_SHOTS_DIR": "/game/runtime/shots"}), \
                unittest.mock.patch("bg3ai.screen.ScreenCapture", side_effect=lambda d: made.append(d)):
            try:
                self.fn()
            except Exception:  # noqa: BLE001  (the fake capture object is None)
                pass
        self.assertEqual(made, [Path("/game/runtime/shots")])
        with unittest.mock.patch.dict("os.environ", {"BG3AI_SHOTS_DIR": ""}):
            self.assertEqual(observe._shots_dir(), core.RUNTIME / "shots")


class DoorOpenState(unittest.TestCase):
    """S054 05:54:51: 'Control Centre <door> 4.5m' gave no open/closed state; interact closed the open door."""

    def test_door_shows_open_or_closed(self):
        from bg3ai import render
        H = core.H
        n = {"items": [{"uuid": "d00r0000-0000-0000-0000-000000000001", "name": "Control Centre", "kind": "door",
                        "dist": 4.5, "open": True},
                       {"uuid": "d00r0000-0000-0000-0000-000000000002", "name": "Cell Door", "kind": "door",
                        "dist": 6.0, "closed": True, "locked": True}]}
        out = render.render_nearby(n, H)
        self.assertRegex(out, r"Control Centre <door> 4\.5m open")
        self.assertRegex(out, r"Cell Door <door> 6m locked closed")


if __name__ == "__main__":
    unittest.main()

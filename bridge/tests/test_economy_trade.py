"""Offline tests: pickpocket and trade (open, handover, confirm closes, wares fill, sell opens, no gold).
Fixtures: test_economy.py."""

from __future__ import annotations

import unittest

from test_economy import (  # sets BG3AI_RUNTIME before importing bg3ai
    FakeGame,
    GameError,
    Handles,
    JobResult,
    _u,
    core,
    economy,
    plumbing,
    progression,
    reporting,
    s026_inventory,
    travel,
    uitools,
    world_tools,
)


class Pickpocket(unittest.TestCase):
    """pickpocket lists the pockets with the game's shown odds (Services.lua ui.pickpocket) and takes one."""

    POCKETS = {"items": [{"name": "Gold", "count": 12, "chance": 81, "dc": 8},
                         {"uuid": _u(31), "name": "Dagger", "price": 2, "chance": 54, "dc": 12},
                         {"uuid": _u(32), "name": "Ring", "price": 90, "chance": 20, "dc": 18}]}

    def setUp(self):
        saved = (core.H, core.game, world_tools._click_act)
        core.H = Handles()
        self.addCleanup(lambda: (setattr(core, "H", saved[0]), setattr(core, "game", saved[1]),
                                 setattr(world_tools, "_click_act", saved[2])))
        self.jobs: list = []
        self.window = False
        test = self

        class G:
            def run_job(self, op, args, context="server", wait_s=25.0):
                test.jobs.append((op, dict(args)))
                if op == "act.select":
                    return JobResult("done", {})
                if not test.window:
                    return JobResult("failed", error="no pickpocket window is open: pickpocket(npc) opens it")
                if args.get("take"):
                    return JobResult("done", {"item": test.POCKETS["items"][1], "success": True, "rolled": 15,
                                              "dc": 12, "chance": 54})
                return JobResult("done", test.POCKETS)

            def try_rpc(self, op, *a, **k):
                if op == "ui.selected":  # the HUD has Claude too (A16: the pickpocket select reads the client)
                    return {"uuid": _u(1)}
                return self.rpc(op) if op == "state.status" else None

            def drain_events(self, ctx):
                return []

            def rpc(self, op, *a, **k):
                return {"active": _u(1), "party": [{"uuid": _u(1), "name": "Claude"}, {"uuid": _u(2), "name": "Shadowheart"}]}

        core.game = G()

        def click_act(tool, op, args, wait_s=40.0):
            test.window = True
            reporting._batch["last"] = "done"
            return "pickpocket: done — opened pickpocket"
        world_tools._click_act = click_act
        self.npc = core.H.get(_u(30), "Refugee")

    def test_text_sorted_by_odds(self):
        t = economy.pockets_text(self.POCKETS, lambda it: "")
        self.assertTrue(t.startswith("POCKETS"))
        self.assertLess(t.index("Gold x12 81% DC8"), t.index("Dagger 2g 54% DC12"))
        self.assertLess(t.index("Dagger"), t.index("Ring 90g 20% DC18"))

    def test_unread_odds_are_not_zero(self):
        t = economy.pockets_text({"items": [{"name": "Club", "chance": 0, "dc": 0}, {"name": "Bow", "chance": 45, "dc": 12},
                                            {"name": "Mail", "chance": 0, "dc": 26}]}, lambda it: "")
        self.assertIn("Club odds ?", t)
        self.assertIn("Mail 0% DC26", t)
        self.assertLess(t.index("Bow 45%"), t.index("Club odds ?"))

    def test_opens_then_lists(self):
        out = world_tools.pickpocket(self.npc)
        self.assertIn("opened pickpocket", out)
        self.assertIn("Ring 90g 20% DC18", out)
        self.assertEqual([op for op, _ in self.jobs], ["ui.pickpocket", "ui.pickpocket"])
        self.assertEqual(self.jobs[0][1]["npc"], _u(30))

    def test_other_member_takes_control_first(self):
        """Live 12:3x: who=Shadowheart while Claude was controlled: the window never showed (client UI follows the
        controlled character)."""
        core.H.get(_u(2), "Shadowheart")
        out = world_tools.pickpocket(self.npc, who="Shadowheart")
        self.assertEqual([op for op, _ in self.jobs], ["ui.pickpocket", "act.select", "ui.pickpocket"])
        self.assertEqual(self.jobs[1][1], {"char": _u(2)})
        self.assertIn("took control of Shadowheart", out)
        self.jobs.clear()
        self.window = False
        world_tools.pickpocket(self.npc, who="Claude")                  # already controlled: no select
        self.assertNotIn("act.select", [op for op, _ in self.jobs])

    def test_take_by_handle_in_one_call(self):
        self.window = True
        h = core.H.get(_u(31), "Dagger")
        out = world_tools.pickpocket(self.npc, take=h, min_chance=40)
        self.assertIn("stole", out)
        self.assertIn("rolled 15 vs DC 12", out)
        take = next(a for _, a in self.jobs if a.get("take"))   # the pockets are read again after it
        self.assertEqual((take["take"], take["min_chance"]), (_u(31), world_tools.SELECT_ONLY))  # 40: checked here
        self.assertEqual(self.jobs[-1][1].get("take"), None)

    def test_window_that_never_opens(self):
        # live 12:3x: pickpocket on an Ox: the open step said done but no window came; the 2nd read raised
        test = self

        def click_act(tool, op, args, wait_s=40.0):
            reporting._batch["last"] = "done"
            return "pickpocket: done — opened=pickpocket via=engine"
        world_tools._click_act = click_act

        class Raising:
            def run_job(self, op, args, context="server", wait_s=25.0):
                test.jobs.append((op, dict(args)))
                raise GameError("no pickpocket window is open: pickpocket(npc) opens it")

            def drain_events(self, ctx):
                return []

            def try_rpc(self, *a, **k):
                return None

            def rpc(self, op, *a, **k):
                return {"active": _u(1), "party": [{"uuid": _u(1), "name": "Claude"}]}
        core.game = Raising()
        out = world_tools.pickpocket(self.npc)
        self.assertIn("no pickpocket window", out)
        self.assertGreaterEqual(len(self.jobs), 2)   # read, open, then polls ~3 s for the widget

    def test_failed_steal_says_crime(self):
        t = economy.steal_text({"item": {"name": "Ring"}, "success": False, "rolled": 3, "dc": 18, "chance": 20}, lambda it: "")
        self.assertIn("CAUGHT?", t)
        self.assertIn("rolled 3 vs DC 18", t)


class TradeOpen(unittest.TestCase):
    """S020/S025/S026: trade(npc) FAILED 'no trade screen is open' every time; the agent then talked and picked
    'Let me see your wares.' by hand. trade(npc) now does that itself."""

    DAMMON = [{"i": 1, "text": "Would you be able to take a look at Karlach's heart?"},
              {"i": 2, "text": "Let me see your wares."}, {"i": 3, "text": "Leave."}]

    def test_trade_answer(self):
        self.assertEqual(economy.trade_answer(self.DAMMON)["i"], 2)
        self.assertEqual(economy.trade_answer([{"i": 1, "text": "What do you have for sale?"}])["i"], 1)
        self.assertEqual(economy.trade_answer([{"i": 4, "text": "[TRADE] Show me."}])["i"], 4)
        self.assertIsNone(economy.trade_answer([{"i": 1, "text": "Who are you?"}, {"i": 2, "text": "Leave."}]))
        self.assertIsNone(economy.trade_answer([{"i": 1, "text": "Let me see your wares.", "off": True}]))

    def run_trade(self, ui_seq: list, dialogue_opts=None, opens=True):
        calls = []
        state = {"ui": ui_seq[0]}

        class G:
            def try_rpc(self, op, args=None, context="server", default=None):
                return state["ui"] if op == "ui.state" else default

            def run_job(self, op, args=None, context="server", wait_s=0, **kw):
                calls.append((op, args))
                if op == "ui.dialogue_choose" and opens:
                    state["ui"] = {"screen": "trade"}
                return JobResult(status="done", result={})

        def fake_talk(npc, who=None, wait=True):
            calls.append(("talk_to", npc))
            state["ui"] = {"screen": "dialogue", "dialogue": {"options": dialogue_opts or []}}
            return "talk_to: done — dialogue"

        saved = (core.game, travel.talk_to, plumbing._act, core.H)
        core.H = Handles()
        core.H.get("11111111-2222-3333-4444-5555555555aa", "Dammon")
        core.game, travel.talk_to = G(), fake_talk
        plumbing._act = lambda tool, op, args, **kw: calls.append((op, args)) or "trade: done — wares=[…]"
        try:
            return progression.trade(npc="e1"), calls
        finally:
            core.game, travel.talk_to, plumbing._act, core.H = saved

    def test_trade_npc_talks_and_picks_the_wares_answer(self):
        out, calls = self.run_trade([{"screen": "game"}], self.DAMMON)
        self.assertEqual([c[0] for c in calls], ["talk_to", "ui.dialogue_choose", "ui.trade"])
        self.assertEqual(calls[1][1], {"i": 2})
        self.assertIn("picked 'Let me see your wares.'", out)
        self.assertIn("trade: done", out)

    def test_trade_npc_already_open_lists_at_once(self):
        out, calls = self.run_trade([{"screen": "trade"}])
        self.assertEqual([c[0] for c in calls], ["ui.trade"])
        self.assertTrue(out.startswith("trade: done"), out)

    def test_trade_npc_without_a_trade_answer_stops_at_the_conversation(self):
        out, calls = self.run_trade([{"screen": "game"}], [{"i": 1, "text": "Who are you?"}, {"i": 2, "text": "Leave."}])
        self.assertEqual([c[0] for c in calls], ["talk_to"])
        self.assertIn("no trade answer", out)
        self.assertIn("1. Who are you?", out)

    def test_trade_npc_answer_that_opens_nothing_fails_clearly(self):
        out, calls = self.run_trade([{"screen": "game"}], self.DAMMON, opens=False)
        self.assertIn("no trade screen opened", out)
        self.assertNotIn("ui.trade", [c[0] for c in calls])


class TradeHandover(unittest.TestCase):
    """S028 15:48: Wulbren's 'I have the tools you need - take them.' opened a trade screen with no wares and no
    gold; trade(offer, confirm) refused the donation, and the agent screenshotted, clicked Barter and confirmed the
    'Donate these items?' box by hand (5 calls). An equipped hammer failed 'some items couldn't be moved' silently."""

    MACE = "11111111-2222-3333-4444-5555555555ab"

    def test_handover_screen(self):
        self.assertTrue(economy.handover({"wares": [], "trader_gold": 0}))
        self.assertFalse(economy.handover({"wares": [{"uuid": "x", "price": 3}], "trader_gold": 0}))
        self.assertFalse(economy.handover({"wares": [], "trader_gold": 40}))
        self.assertFalse(economy.handover({"wares": [], "trader_gold": 0, "wares_more": True}))
        self.assertFalse(economy.handover(None))

    def test_problems_rendered(self):
        out = economy.trade_text({"problems": ["item Hammer isn't on this side of the trade screen"]},
                                 lambda it: "e9", listing=False)
        self.assertIn("NOT MOVED: item Hammer isn't on this side", out)

    def test_equipped_offer_named(self):
        inv = {"items": [{"uuid": "u1", "name": "Ceremonial Warhammer", "equipped_slot": "Melee Main Weapon",
                          "owner": "c1"}, {"uuid": "u2", "name": "Mace"}]}
        out = economy.equipped_offers(inv, ["u1", "u2"], lambda u: "Claude")
        self.assertEqual(len(out), 1)
        self.assertIn("Ceremonial Warhammer is equipped by Claude", out[0])

    def _trade(self, screen: dict, job_result: dict, box: dict | None, **kw):
        calls = []
        ui = {"screen": "trade"}

        class G:
            def try_rpc(self, op, args=None, context="server", default=None):
                calls.append((op, dict(args or {})))
                if op == "ui.trade":
                    return screen
                if op == "ui.confirm":
                    return box
                if op == "ui.state":
                    return ui
                return default

        def fake_act(tool, op, args, **k):
            calls.append((op, dict(args)))
            reporting._batch["result"] = job_result
            return "trade: done"

        def fake_box(tool, op, args, wait_s=8, rounds=3):
            calls.append(("box:" + op, dict(args)))
            ui["screen"] = "trade"
            return "confirm: done — pressed=Yes"

        def fake_esc():
            calls.append(("escape", {}))
            ui.update(screen="dialogue", dialogue={"speaker": "Wulbren Bongle", "options": []})
            return "press_key: done"

        saved = (core.game, plumbing._act, world_tools._box_act, uitools._press_escape, reporting._render_ui, core.H)
        core.H = Handles()
        core.H.get(self.MACE, "Crude Mace")
        core.game, plumbing._act, world_tools._box_act, uitools._press_escape = G(), fake_act, fake_box, fake_esc
        reporting._render_ui = lambda u, brief=False: [f"SCREEN {u.get('screen')}"]
        try:
            return progression.trade(**kw), calls
        finally:
            (core.game, plumbing._act, world_tools._box_act, uitools._press_escape, reporting._render_ui, core.H) = saved

    def test_handover_donates_answers_the_box_and_closes(self):
        moved = [{"uuid": self.MACE, "name": "Crude Mace", "side": "you"}]
        out, calls = self._trade({"wares": [], "trader_gold": 0}, {"donate_box": True, "moved": moved},
                                 {"title": "Donate these items?", "buttons": ["Yes", "No"]},
                                 offer="e1", confirm=True)
        job = next(a for op, a in calls if op == "ui.trade" and a.get("confirm"))
        self.assertTrue(job.get("donate"))
        self.assertIn(("box:ui.confirm", {"answer": "Yes"}), calls)
        self.assertIn("escape", [c[0] for c in calls])
        self.assertIn("gave e1 Crude Mace (donated", out)
        self.assertIn("closed the hand-over screen", out)
        self.assertIn("SCREEN dialogue", out)

    def test_shop_with_wares_does_not_donate(self):
        out, calls = self._trade({"wares": [{"uuid": "w", "price": 5}], "trader_gold": 0},
                                 {"offer": {}, "wares": []}, None, offer="e1", confirm=True)
        job = next(a for op, a in calls if op == "ui.trade" and a.get("confirm"))
        self.assertNotIn("donate", job)
        self.assertNotIn("box:ui.confirm", [c[0] for c in calls])

    def test_box_that_is_not_a_donation_is_left_to_the_agent(self):
        out, calls = self._trade({"wares": [], "trader_gold": 0}, {"donate_box": True, "moved": []},
                                 {"title": "Steal these items?", "buttons": ["Yes", "No"]}, offer="e1", confirm=True)
        self.assertIn("STOPPED", out)
        self.assertNotIn("box:ui.confirm", [c[0] for c in calls])


class TradeConfirmCloses(unittest.TestCase):
    """S067 #125 / S068 #168: trade(request=…, confirm=true) left the window up and the agent pressed ESCAPE after
    it each time. A confirmed deal closes the window and checks the game view is back."""

    WARES = tuple(f"11111111-2222-3333-4444-5555555557{n:02d}" for n in range(4))  # Supply Pack, Pork, Chicken, Potion

    def _trade(self, job_result: dict, closes_after: int | None = 2, **kw):
        calls, ui, reads = [], {"screen": "trade"}, [0]

        class G:
            def try_rpc(self, op, args=None, context="server", default=None):
                calls.append((op, dict(args or {})))
                if op == "ui.key":
                    ui.update(screen="dialogue", dialogue={"speaker": "Arron", "options": []})  # the goodbye line
                if op == "ui.state":
                    if ui.get("screen") == "dialogue" and closes_after is not None:
                        reads[0] += 1
                        if reads[0] > closes_after:
                            ui.clear()  # the line ended: no screen = the game view
                    return dict(ui)
                return default

        def fake_act(tool, op, args, **k):
            calls.append((op, dict(args)))
            reporting._batch["result"] = job_result
            return "trade: done"

        saved = (core.game, plumbing._act, reporting._render_ui, core.H, progression.TRADE_CLOSE_S)
        core.H = Handles()
        handles = ",".join(core.H.get(u, f"ware {i}") for i, u in enumerate(self.WARES))
        core.game, plumbing._act = G(), fake_act
        reporting._render_ui = lambda u, brief=False: [f"SCREEN {u.get('screen')}"]
        progression.TRADE_CLOSE_S = 0.5
        try:
            return progression.trade(request=handles, **kw), calls
        finally:
            (core.game, plumbing._act, reporting._render_ui, core.H, progression.TRADE_CLOSE_S) = saved

    def test_s068_confirmed_deal_closes_the_window(self):
        out, calls = self._trade({"confirmed": True, "moved": []}, confirm=True)
        self.assertEqual([a for op, a in calls if op == "ui.key"], [{"key": "ESCAPE"}])
        self.assertIn("closed the trade window: back in the game", out)

    def test_no_confirm_keeps_it_open(self):
        out, calls = self._trade({"moved": []})
        self.assertNotIn("ui.key", [c[0] for c in calls])
        self.assertNotIn("closed the trade window", out)

    def test_deal_not_confirmed_keeps_it_open(self):
        _, calls = self._trade({"moved": [], "problems": ["x"]}, confirm=True)
        self.assertNotIn("ui.key", [c[0] for c in calls])

    def test_window_still_up_is_said(self):
        out, _ = self._trade({"confirmed": True, "moved": []}, closes_after=None, confirm=True)
        self.assertIn("(closing the trade window: screen=dialogue after ESCAPE)", out)
        self.assertIn("SCREEN dialogue", out)


_HALF = {"unmatched": 1, "wares": [{"price": 1040}, {"price": 832}]}
_NAMED = {"wares": [{"uuid": "11111111-2222-3333-4444-555555555701", "name": "Supply Pack", "price": 156,
                     "supply": 40}]}


class TradeWaresFill(unittest.TestCase):
    """L7 22:08: trade() 0.2 s after the screen opened listed 24 wares "(no id) ?" (the trader's side half filled);
    the listing is read again until the wares are named."""

    def _listing(self, results: list[dict], **kw):
        calls = []

        def fake_act(tool, op, args, **k):
            calls.append((op, dict(args)))
            reporting._batch["result"] = results[min(len(calls), len(results)) - 1]
            return "trade: done" + (" (no id)" if progression._wares_unnamed(reporting._batch["result"]) else " named")

        saved = (plumbing._act, progression.TRADE_FILL_S)
        plumbing._act, progression.TRADE_FILL_S = fake_act, 0.0
        try:
            return progression.trade(**kw), calls
        finally:
            plumbing._act, progression.TRADE_FILL_S = saved


    def test_half_filled_wares_are_read_again(self):
        out, calls = self._listing([_HALF, _HALF, _NAMED])
        self.assertEqual(len(calls), 3)
        self.assertIn("named", out)

    def test_named_listing_is_read_once(self):
        _, calls = self._listing([_NAMED])
        self.assertEqual(len(calls), 1)

    def test_never_named_stops_after_the_tries(self):
        out, calls = self._listing([_HALF])
        self.assertEqual(len(calls), 1 + progression.TRADE_FILL_TRIES)
        self.assertIn("(no id)", out)

    def test_a_request_is_not_repeated(self):
        saved = core.H
        core.H = Handles()
        try:
            h = core.H.get("11111111-2222-3333-4444-555555555701", "Supply Pack")
            _, calls = self._listing([_HALF], request=h)
        finally:
            core.H = saved
        self.assertEqual(len(calls), 1)


class TradeSellOpens(unittest.TestCase):
    def test_npc_with_sell_junk_opens_first(self):
        calls = []
        saved = (progression._open_trade, progression._sell_junk)
        progression._open_trade = lambda npc: calls.append(("open", npc)) or "(opened: talked to e5, picked 'Let's see your wares.')"
        progression._sell_junk = lambda keep="": calls.append(("sell", keep)) or "trade: done"
        try:
            out = progression.trade(npc="e5", sell="junk")
        finally:
            progression._open_trade, progression._sell_junk = saved
        self.assertEqual([c[0] for c in calls], ["open", "sell"])
        self.assertIn("(opened: talked to e5", out)
        self.assertIn("trade: done", out)


class TradeNoGold(unittest.TestCase):
    """S028 (vis review): a trader at 0 gold and sell=junk with no trade screen open."""

    def test_broke_trader_and_handover_said(self):
        h = lambda it: "e9"
        t = economy.trade_text({"trader_gold": 0, "wares": [{"uuid": "w", "name": "Rope", "price": 3}]}, h)
        self.assertIn("TRADER HAS NO GOLD", t)
        t = economy.trade_text({"trader_gold": 0, "wares": []}, h)
        self.assertIn("HAND-OVER", t)
        self.assertNotIn("NO GOLD", economy.trade_text({"trader_gold": 50, "wares": []}, h))

    def _sell(self, screen):
        calls = []

        class G:
            def try_rpc(self, op, args=None, context="server", default=None):
                return {"screen": screen} if op == "ui.state" else default

        saved = (core.game, progression._open_trade, progression._sell_junk)
        core.game = G()
        progression._open_trade = lambda npc: calls.append(("open", npc)) or "(opened: picked 'Let me see your wares.')"
        progression._sell_junk = lambda keep="": calls.append(("sell", keep)) or "trade: done"
        try:
            return progression.trade(sell="junk"), calls
        finally:
            core.game, progression._open_trade, progression._sell_junk = saved

    def test_sell_junk_without_a_screen_refuses_clearly(self):
        out, calls = self._sell("game")
        self.assertIn('trade(npc=<trader id>, sell="junk")', out)
        self.assertEqual(calls, [])

    def test_sell_junk_in_a_trader_conversation_opens_it(self):
        out, calls = self._sell("dialogue")
        self.assertEqual([c[0] for c in calls], ["open", "sell"])
        self.assertEqual(calls[0][1], "")

    def test_sell_junk_broke_trader_message(self):
        g = FakeGame(s026_inventory(), {"trader_gold": 0, "your_items": [{"uuid": _u(6), "price": 30}]})
        orig = core.game
        core.game = g
        try:
            out = progression.trade(sell="junk")
        finally:
            core.game = orig
        self.assertIn("the trader has no gold left", out)


if __name__ == "__main__":
    unittest.main()

"""Offline tests for the 2026-10-13 audit's nav findings on the bridge side: NAV-2 (a killed level's partial WAYS
line), NAV-6 (a walk's arrival reads only warm ways, never a cold frontier).

Run: cd bridge && uv run python -m unittest discover -s tests -p 'test_nav_audit.py'
"""

from __future__ import annotations

import unittest
from unittest import mock

from test_turn_macros import CHEST, P1, P2, Base  # noqa: F401  (sets BG3AI_RUNTIME before importing server)

from bg3ai import delta, render, server  # noqa: E402


class ArrivalLeads(Base):
    def test_arrival_asks_for_cached_ways_only(self):
        seen = []

        def frontier(op, args=None, context="server", default=None):
            seen.append((op, args))
            return {"leads": [{"uuid": CHEST, "name": "Crate", "kind": "container", "dist": 3.0, "dir": "N",
                               "unvisited": True}], "ways": [{"dir": "N", "open_m": 20}]}
        self.g.try_rpc = frontier
        out = server._arrival_leads()
        self.assertEqual(seen, [("nav.frontier", {"ways_cached": True})])
        self.assertIn("LEADS (unvisited", out)
        self.assertIn("WAYS walkable: N 20m+", out)


class KilledLevelWays(unittest.TestCase):
    def test_partial_ways_line(self):
        out = render.render_frontier({"leads": [], "ways": [{"dir": "N", "open_m": 20}], "ways_off": "slow"},
                                     server.H)
        self.assertIn("WAYS walkable: N 20m+", out)
        self.assertIn("WAYS partial: path searches are throttled on this level", out)

    def test_no_line_on_a_healthy_level(self):
        out = render.render_frontier({"leads": [], "ways": [{"dir": "N", "open_m": 20}]}, server.H)
        self.assertNotIn("WAYS partial", out)


class DoorTrapChanges(unittest.TestCase):
    """NAV-4: a door opening/closing and a trap disarmed show in CHANGES."""

    def rec(self, **kw):
        return delta._item_rec(dict({"name": "Door", "kind": "door"}, **kw))

    def test_door_opens_and_closes(self):
        self.assertEqual(delta._flag_changes(self.rec(closed=True)["flags"], self.rec(open=True)["flags"]),
                         ["now open"])
        self.assertEqual(delta._flag_changes(self.rec(open=True)["flags"], self.rec(closed=True)["flags"]),
                         ["now closed"])

    def test_trap_disarmed_and_gone(self):
        armed, off = self.rec(trap={"armed": True}), self.rec(trap={"armed": False})
        self.assertIn("armed", armed["flags"])
        self.assertEqual(delta._flag_changes(armed["flags"], off["flags"]), ["trap disarmed"])
        self.assertEqual(delta._flag_changes(armed["flags"], self.rec()["flags"]), ["trap gone"])

    def test_old_words_kept(self):
        self.assertEqual(delta._flag_changes(("locked",), ("opened",)), ["opened", "unlocked"])


class ClickOffScreen(Base):
    """BACKLOG naut-r2 S001 16:54: click_on FAILED off_screen x2 with the camera on the selected Lae'zel, Corran next to
    the nerve; set_party select Corran + click_on worked. Now click_on selects the member beside it and retries."""

    def setUp(self):
        super().setUp()
        import bg3ai.clicker as clicker
        self.clicks = []
        tries = iter([False, True])

        class CR:
            def __init__(self, ok):
                self.ok, self.reason, self.screen = ok, None if ok else "off_screen", "game"

            def as_dict(self):
                return {"ok": self.ok}
        self.CR = CR

        saved_ce, saved_inp = clicker.click_entity, server._inp
        clicker.click_entity = lambda game, inp, u, b: self.clicks.append(u) or CR(next(tries))
        server._inp = lambda: object()
        self.addCleanup(setattr, clicker, "click_entity", saved_ce)
        self.addCleanup(setattr, server, "_inp", saved_inp)
        self.g.watch["party"] = [{"uuid": P1, "pos": [0, 0, 0]}, {"uuid": P2, "pos": [11, 0, 0]}]
        real = self.g.rpc

        def rpc(op, args=None, context="server", timeout=12.0):
            if op == "state.entity":
                return {"uuid": CHEST, "pos": [12, 0, 0], "visible": True}
            return real(op, args, context, timeout)
        self.g.rpc = rpc

    def test_selects_the_member_beside_it_and_retries(self):
        server.H.get(CHEST, "Touch")
        out = server.click_on(server.H.get(CHEST))
        self.assertEqual(len(self.clicks), 2, out)
        self.assertIn(("act.select", {"char": P2}), [(op, a) for op, a in self.g.calls if op == "act.select"])
        self.assertIn("selected", out)
        self.assertIn("clicked", out)

    def test_selects_before_the_first_look_when_off_view(self):
        # se click_on latency: the target behind the camera -> the select comes first, one click_entity, no 7 s pass
        real = self.g.rpc

        def rpc(op, args=None, context="server", timeout=12.0):
            if op == "ui.screen_pos":
                return {"on_screen": False, "behind": True, "side": "left"}
            return real(op, args, context, timeout)
        self.g.rpc = rpc
        self.tries = iter([True])
        import bg3ai.clicker as clicker
        clicker.click_entity = lambda game, inp, u, b: self.clicks.append(u) or self.CR(next(self.tries))
        server.H.get(CHEST, "Touch")
        out = server.click_on(server.H.get(CHEST))
        self.assertEqual(len(self.clicks), 1, out)
        self.assertIn(("act.select", {"char": P2}), [(op, a) for op, a in self.g.calls if op == "act.select"])
        self.assertTrue(out.startswith("selected"), out)

    def test_no_early_select_when_in_view_or_switch_off(self):
        import bg3ai.clicker as clicker
        real = self.g.rpc
        sp = {"on_screen": True, "ndc": [0.1, 0.2]}
        self.g.rpc = lambda op, args=None, context="server", timeout=12.0: \
            sp if op == "ui.screen_pos" else real(op, args, context, timeout)
        self.assertFalse(server._click_off_view(CHEST))
        sp = {"on_screen": False, "behind": True}
        self.assertTrue(server._click_off_view(CHEST))
        with mock.patch.object(clicker, "HOVER_V2", False):
            server.H.get(CHEST, "Touch")
            out = server.click_on(server.H.get(CHEST))
        self.assertEqual(len(self.clicks), 2, out)   # v1: off_screen first, then select + retry

    def test_no_select_when_the_selected_one_is_nearest(self):
        self.g.watch["party"] = [{"uuid": P1, "pos": [11, 0, 0]}, {"uuid": P2, "pos": [0, 0, 0]}]
        self.assertIsNone(server._click_selects(CHEST))


if __name__ == "__main__":
    unittest.main()

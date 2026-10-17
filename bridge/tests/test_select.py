"""Offline tests: act.select is done once the HUD (the client's ClientControl) shows the character (A16).

Run: cd bridge && uv run python -m unittest tests.test_select
"""

from __future__ import annotations

import unittest

from bg3ai import game as gamemod

P1, P2, P3 = ("aaaaaaaa-0000-0000-0000-000000000001", "bbbbbbbb-0000-0000-0000-000000000002",
              "cccccccc-0000-0000-0000-000000000003")


class FakeGame(gamemod.Game):
    """A scripted mod: act.select settles at once on the server (its current character); the HUD follows a real
    change of it, never a no-op order (S074: the server already had Claude, the HUD another member)."""

    SELECT_FOLLOW_S = 0.05

    def __init__(self, server: str, hud: str, stuck: bool = False, client_ok: bool = True):
        super().__init__(port=1)
        self.server, self.hud, self.stuck, self.client_ok = server, hud, stuck, client_ok
        self.selects: list[str] = []
        self.party = [{"uuid": P1}, {"uuid": P2}, {"uuid": P3}]

    def rpc(self, op, args=None, context="server", timeout=12.0):
        if op == "act.select":
            c = args["char"]
            self.selects.append(c)
            if c != self.server:
                self.server = c
                if not self.stuck:
                    self.hud = c
            return {"job": len(self.selects), "status": "done", "result": {"selected": True}}
        if op == "ui.selected":
            if not self.client_ok:
                raise gamemod.NotReady("BG3AI mod is not loaded in the client context")
            return {"uuid": self.hud}
        if op == "state.status":
            return {"active": self.server, "party": self.party}
        raise AssertionError(op)


class Select(unittest.TestCase):
    def test_done_when_the_hud_follows(self):
        g = FakeGame(server=P2, hud=P2)
        r = g.run_job("act.select", {"char": P1})
        self.assertEqual(r.status, "done")
        self.assertEqual(g.selects, [P1])

    def test_server_noop_hands_over_via_another_member(self):
        # S074 23:06:52 (bg3le 25512.4): done in 1 ms on the server while the HUD kept P2; ui.reactions refused
        g = FakeGame(server=P1, hud=P2)
        r = g.run_job("act.select", {"char": P1})
        self.assertEqual(r.status, "done", r.error)
        self.assertEqual(g.selects, [P1, P2, P1])
        self.assertEqual(g.hud, P1)
        self.assertEqual(r.result["via"], P2)

    def test_dead_or_downed_members_are_not_the_hand_over(self):
        g = FakeGame(server=P1, hud=P3)
        g.party = [{"uuid": P1}, {"uuid": P2, "dead": True}, {"uuid": P3}]
        r = g.run_job("act.select", {"char": P1})
        self.assertEqual(r.status, "done", r.error)
        self.assertEqual(g.selects, [P1, P3, P1])

    def test_a_hud_that_never_follows_is_a_failure_naming_the_holder(self):
        g = FakeGame(server=P1, hud=P2, stuck=True)
        r = g.run_job("act.select", {"char": P1})
        self.assertEqual(r.status, "failed")
        self.assertIn("the HUD kept another member selected", r.error)
        self.assertIn("selecting another member and back didn't move it either", r.error)
        self.assertEqual(r.result["holder"], P2)

    def test_an_unreadable_client_takes_the_servers_word(self):
        g = FakeGame(server=P1, hud=P2, client_ok=False)
        r = g.run_job("act.select", {"char": P1})
        self.assertEqual(r.status, "done")
        self.assertEqual(g.selects, [P1])


if __name__ == "__main__":
    unittest.main()

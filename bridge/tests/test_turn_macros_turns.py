"""Offline tests: single tools and end_turn in a fight (a stale conversation flag, an invented attack id, the HUD's
End Turn when the game ignores the order) and who acts in a turn group. Fixtures: test_turn_macros.py."""

from __future__ import annotations

import threading
import time
import unittest
from unittest import mock

from test_turn_macros import (  # sets BG3AI_RUNTIME before importing bg3ai
    GOB,
    NOT_JOBS,
    P1,
    P2,
    CombatTurnBase,
    JobResult,
    combat,
    combat_tools,
    core,
    macro_tools,
    overview,
    plumbing,
    turn,
    uitools,
)


class CombatTools(CombatTurnBase):
    def test_stale_conversation_flag_retried_for_a_single_tool(self):
        # S055 06:44: pickpocket ×3 "character is in a conversation" right after the conversation ended
        saved, macro_tools.CONVO_WAIT_S = macro_tools.CONVO_WAIT_S, 2.0
        self.addCleanup(setattr, macro_tools, "CONVO_WAIT_S", saved)
        n = {"left": 2}

        def refuse(op, a):
            if op == "act.pickpocket" and n["left"] > 0:
                n["left"] -= 1
                return "character is in a conversation (dialog 1995725312 res=e411)"
            return None
        self.g.refuse_if = refuse
        self.g.ui = {"screen": "game"}
        out = plumbing._act("pickpocket", "act.pickpocket", {"char": P1, "npc": GOB})
        self.assertIn("pickpocket: done", out)
        self.assertEqual(len(self.g.ops("act.pickpocket")), 3)

    def test_invented_ranged_attack_id_is_aliased(self):
        # S051 ×2: "Projectile_RangedAttack" → "unknown action", a failed alternative each time
        self.assertEqual(overview._ability_id(P1, "Projectile_RangedAttack"), "Projectile_MainHandAttack")
        self.assertEqual(overview._ability_id(P1, "Target_MeleeAttack"), "Target_MainHandAttack")

    def test_hud_end_is_not_interleaved_with_input_from_another_thread(self):
        """Review M7: the background end's HUD fallback (select A, press End Turn) ran while the MCP thread clicked /
        selected for B: the press could land on B. Both take plumbing._INPUT_LOCK now: B's press_key waits."""
        self.g.combat["current"] = P1
        self.g.active = P2                       # someone else is selected: the fallback selects P1 first
        self.g.dur["act.select"] = 0.3
        real = self.g.rpc
        order = []

        def rpc(op, args=None, context="server", timeout=12.0):
            if op in ("ui.command", "ui.key") or op == "act.select":
                order.append(op if op != "act.select" else f"select {args['char'][-1]}")
            if op == "ui.command":
                self.g.combat["current"] = GOB
                return {"done": True}
            if op == "ui.key":  # a finished job
                self.g.jobs[999] = {"status": "done", "end": 0, "op": op, "args": args, "result": {"key": "I"}}
                return {"job": 999}
            return real(op, args, context, timeout)
        self.g.rpc = rpc
        self.g.ui = {"screen": "inventory", "panels_open": ["Inventory"]}
        t = threading.Thread(target=combat_tools._end_turn_by_hud,
                             args=(P1, JobResult("failed", None, combat_tools.END_IGNORED)))
        t.start()
        deadline = time.monotonic() + 2
        while not order and time.monotonic() < deadline:
            time.sleep(0.01)
        plumbing._act("press_key", "ui.key", {"key": "I"}, context="client", wait_s=2)
        t.join(5)
        self.assertEqual(order[:3], [f"select {P1[-1]}", "ui.command", "ui.key"], order)

    def test_ignored_end_turn_presses_the_hud_end_turn(self):
        # S044/S045: Osiris EndTurn ignored for our Spiritual Weapon / during Ketheric's lines: 10 s TIMEOUT ×9
        self.g.refuse_if = lambda op, a: combat_tools.END_IGNORED if op == "act.end_turn" else None
        self.g.on_start = None   # (the class's hook hands the turn on at the order: here the game ignores it)
        real = self.g.rpc
        pressed = []

        def rpc(op, args=None, context="server", timeout=12.0):
            if op == "ui.command":
                pressed.append(args)
                self.g.combat["current"] = GOB
                return {"done": True}
            return real(op, args, context, timeout)
        self.g.rpc = rpc
        out = combat_tools.end_turn(who=core.H.get(P1), wait=False)
        self.assertEqual(pressed, [{"widget": "*", "command": "EndTurn"}])
        self.assertIn("the HUD's End Turn", out)
        self.assertNotIn("FAILED", out)
        # the HUD doesn't end it either: says so
        self.g.combat["current"] = P1
        saved, combat_tools.END_HUD_WAIT_S = combat_tools.END_HUD_WAIT_S, 0.3
        self.addCleanup(setattr, combat_tools, "END_HUD_WAIT_S", saved)
        def stuck(op, args=None, context="server", timeout=12.0):
            if op == "ui.command":
                return {"done": True}
            if op == "state.combat":
                self.g.combat["current"] = P1   # the turn never moves on
            return real(op, args, context, timeout)
        self.g.rpc = stuck
        out = combat_tools.end_turn(who=core.H.get(P1), wait=False)
        self.assertIn("didn't end it either", out, (out, self.g.combat))

    def test_a_slow_end_after_the_hud_press_is_done_not_failed(self):
        # S066 19:21 #68 / S067 19:46 #65: a shared group's last end came 7-10.4 s after the order, past the Lua 3 s
        # + the old 3 s HUD watch: "didn't end it either", the next action refused, end_turn found nothing
        clock = [0.0]
        reads = []

        def rpc(op, args=None, context="server", timeout=12.0):
            if op == "state.combat":
                reads.append(clock[0])
                return combat(current=GOB) if len(reads) > 1 and clock[0] >= 7.0 else combat()
            return {"done": True}

        def sleep(s):
            clock[0] += s
        g = type("G", (), {"rpc": staticmethod(rpc), "try_rpc": staticmethod(rpc)})()
        with mock.patch.object(core, "game", g), mock.patch.object(uitools, "_hud_has", lambda u: u == P1), \
                mock.patch.object(time, "monotonic", lambda: clock[0]), \
                mock.patch.object(time, "sleep", sleep):
            r = combat_tools._end_turn_by_hud(P1, JobResult("failed", None, combat_tools.END_IGNORED))
        self.assertEqual(r.status, "done", r.error)
        self.assertIn("the game, slowly (10 s after the order", r.result["via"])

    # S018: end_turn on downed Claude / Shadowheart -> "FAILED: character is downed" (x2): the mod's refusal is the fact
    def test_downed_or_dead_member_end_is_skipped_on_the_mods_refusal(self):
        p1 = next(o for o in self.g.combat["order"] if o["uuid"] == P1)
        p1.update(hp_pct=0, downed=True)
        self.g.on_start = None
        self.g.refuse_if = lambda op, a: "character is downed" if op == "act.end_turn" else None  # Guard.actor
        out = combat_tools.end_turn(who=core.H.get(P1), wait=False)
        self.assertIn("skipped", out)
        self.assertIn("is downed: nothing to play, its turn passes by itself", out)
        # live smoke L5: only a thrown potion heals another, and this server can't throw one: no potion named
        self.assertIn("Healing Word or Help (adjacent).", out)
        self.assertNotIn("potion", out)
        self.assertNotIn("FAILED", out)
        self.assertEqual([o for o, _ in self.g.ops("act.") if o not in NOT_JOBS], ["act.end_turn"])
        self.g.refuse_if = lambda op, a: "character is dead" if op == "act.end_turn" else None
        out = combat_tools.end_turn(who=core.H.get(P1), wait=False)
        self.assertIn("is dead: it has no turn", out)
        self.assertNotIn("FAILED", out)

    # Live 10 m1 a2 (D297): Lae'zel at 0 HP without the DOWNED status (the mod's row reads downed by its HP; the
    # game rolled no death saving throw and kept her turn open). end_turn said "downed ... its turn passes by itself",
    # sent no end and waited 80 s on that open turn. The mod ends it (it refuses only DOWNED or dead).
    def test_zero_hp_member_without_downed_status_gets_its_turn_ended(self):
        p1 = next(o for o in self.g.combat["order"] if o["uuid"] == P1)
        p1.update(hp_pct=0, downed=True)

        def advance(op, args):  # the end goes through: Lae'zel's turn next
            if op == "act.end_turn":
                self.g.combat["current"] = P2
        self.g.on_start = advance
        t = time.monotonic()
        out = combat_tools.end_turn(who=core.H.get(P1))
        self.assertLess(time.monotonic() - t, 5.0)
        self.assertEqual([o for o, _ in self.g.ops("act.") if o not in NOT_JOBS], ["act.end_turn"])
        self.assertNotIn("passes by itself", out)
        self.assertNotIn("skipped", out)
        self.assertIn("is at 0 HP without the Downed status (no death saving throws): the game kept its turn open", out)
        self.assertIn(f"now {core.H.get(P2)}'s turn", out)

    def test_a_busy_answer_after_the_hud_press_checks_the_turn(self):
        """act2-fights-analysis fix 3 (launch-v1 deepseek windmill r3, balthazar r1, act3_gold_max r1): the press
        answered "bg3le: UI busy ... met it after a UI call ran" and end_turn failed at once, though the press may have
        run. Now the turn is checked as after any press; refused at the gate (nothing pressed), it fails at once."""
        from bg3ai.game import GameError

        after = ("bg3le: UI busy: the game's UI is busy (a rest/travel/load transition); met it after a UI call ran; "
                 "retry in a moment")
        before = "bg3le: UI busy: the game's UI is busy; refused before its first UI call; retry in a moment"
        for answer, moved, want in ((after, True, "done"), (after, False, "failed"), (before, True, "failed")):
            reads = []

            def rpc(op, args=None, context="server", timeout=12.0, answer=answer, moved=moved, reads=reads):
                if op == "state.combat":
                    reads.append(1)
                    return combat(current=GOB) if moved and len(reads) > 1 else combat()
                raise GameError(answer)
            g = type("G", (), {"rpc": staticmethod(rpc), "try_rpc": staticmethod(rpc)})()
            saved, combat_tools.END_HUD_WAIT_S = combat_tools.END_HUD_WAIT_S, 0.3
            try:
                with mock.patch.object(core, "game", g), mock.patch.object(uitools, "_hud_has", lambda u: u == P1):
                    r = combat_tools._end_turn_by_hud(P1, JobResult("failed", None, combat_tools.END_IGNORED))
            finally:
                combat_tools.END_HUD_WAIT_S = saved
            self.assertEqual(r.status, want, (answer, moved, r.error))
            if want == "failed":
                self.assertIn("the HUD's End Turn failed too: bg3le: UI busy", r.error)
            if answer == before:
                self.assertEqual(len(reads), 1, "nothing pressed: no turn check")

    def test_downed_member_is_not_who_acts(self):
        p1 = next(o for o in self.g.combat["order"] if o["uuid"] == P1)
        p1.update(hp_pct=0, downed=True)
        self.g.combat["turn_group"] = [P1, P2]
        self.assertEqual(turn.party_acting(self.g.combat, {P1, P2}), [P2])

if __name__ == "__main__":
    unittest.main()

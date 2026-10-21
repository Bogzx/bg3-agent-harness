"""Offline tests: combat_turn, whose turn it is (sent ahead, own summons first, downed or 1-HP members, the
turn taken away) and the HP-drop hold. Fixtures: test_turn_macros.py."""

from __future__ import annotations

import json
import threading
import time
import unittest
from unittest import mock

from test_turn_macros import (  # sets BG3AI_RUNTIME before importing bg3ai
    GOB,
    IMP,
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
    turn_plan,
    turn_tool,
    uitools,
)


class CombatTurn(CombatTurnBase):
    # S018 04:06: three combat_turns sent ahead waited 40+30+30 s while our own Spiritual Weapon had its turn.
    def test_sent_ahead_returns_at_once_when_another_party_member_must_act_first(self):
        self.g.combat["current"] = P2
        t = time.monotonic()
        out = turn_tool.combat_turn(self.plan(action="Shout_Dodge"), who=core.H.get(P1), wait_turn=5)
        self.assertLess(time.monotonic() - t, 1.5)
        self.assertIn("FAILED: it is not", out)
        self.assertIn("(yours) must act first", out)
        self.assertIn(f"end_turn who={core.H.get(P2)}", out)
        self.assertEqual(self.g.ops("act.cast"), [])

    # S035: three chains stopped 'not e6's turn (now e510 Spiritual Weapon)': up to 21.7 s waited + a re-send each.
    def test_own_summon_first_is_played_then_the_plan(self):
        p2 = next(o for o in self.g.combat["order"] if o["uuid"] == P2)
        p2.update(name="Spiritual Weapon", summon=True)
        self.g.combat["current"] = P2

        def advance(op, args):  # the summon's end hands the turn to Claude, Claude's to the goblin
            if op == "act.end_turn":
                self.g.combat["current"] = P1 if self.g.combat["current"] == P2 else GOB
        self.g.on_start = advance
        out = turn_tool.combat_turn(self.plan(action={"ability": "Target_MainHandAttack", "target": self.gob}),
                                 who=core.H.get(P1), wait_turn=5)
        self.assertIn("(your summon) had to act first", out)
        self.assertNotIn("FAILED: it is not", out)
        casts = [(a.get("spell"), a.get("caster") or a.get("who")) for _, a in self.g.ops("act.cast")]
        self.assertEqual(len(casts), 2, casts)               # the summon's attack on the goblin, then Claude's
        self.assertEqual(len(self.g.ops("act.end_turn")), 2)
        self.assertEqual(self.g.ops("act.cast")[0][1].get("target"), GOB)
        # in a list plan too; a heal on an ally is never the summon's target
        self.g.combat["current"] = P2
        n = len(self.g.ops("act.cast"))
        out = turn_tool.combat_turn(json.dumps([{"who": core.H.get(P1), "plan": {
            "action": {"ability": "Target_CureWounds", "target": core.H.get(P1)}}}]))
        # no hostile named, none with a position: the summon's turn just ends; the one new cast is Claude's heal
        self.assertEqual([a.get("spell") for _, a in self.g.ops("act.cast")[n:]], ["Target_CureWounds"])
        self.assertIn("(your summon) had to act first", out)
        self.assertNotIn("CHAIN STOPPED", out)

    def test_other_member_first_still_hands_back(self):
        self.g.combat["current"] = P2   # Lae'zel, not a summon: the player plans her turn
        out = turn_tool.combat_turn(self.plan(action="Shout_Dodge"), who=core.H.get(P1), wait_turn=5)
        self.assertIn("(yours) must act first", out)
        self.assertNotIn("your summon", out)

    # S018: end_turn on downed Claude / Shadowheart -> "FAILED: character is downed" (x2); combat_turn too.
    def test_downed_character_turn_is_skipped(self):
        p1 = next(o for o in self.g.combat["order"] if o["uuid"] == P1)
        p1.update(hp_pct=0, downed=True)
        out = turn_tool.combat_turn(self.plan(action="Shout_Dodge"), who=core.H.get(P1))
        self.assertIn("is downed: nothing to play", out)
        out = combat_tools.end_turn(who=core.H.get(P1), wait=False)
        self.assertIn("skipped", out)
        self.assertNotIn("FAILED", out)
        self.assertEqual([o for o, _ in self.g.ops("act.") if o not in NOT_JOBS], [])

    # S076 00:29-00:32 (Cazador try 6 r6): Karlach at 1/218 HP read hp_pct 0 (State.lua rounded it) and end_turn /
    # combat_turn refused her as downed while set_party showed hp=1/218 action=1. Not downed: she plays.
    def test_one_hp_member_rounded_to_zero_pct_is_not_downed(self):
        p1 = next(o for o in self.g.combat["order"] if o["uuid"] == P1)
        p1["hp_pct"] = 0                       # round(100 * 1 / 218) from an older mod; no downed flag
        self.assertEqual(turn.downed(self.g.combat), set())
        out = turn_tool.combat_turn(self.plan(action="Shout_Dodge"), who=core.H.get(P1))
        self.assertNotIn("is downed", out)
        self.assertIn("Shout_Dodge", self.casts())
        self.assertIn("end_turn: done", out)

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

    def test_mid_fight_lines_are_waited_out(self):
        # S045 Ketheric: 6 steps "character is in a conversation (InDialog)" while his lines played
        n = {"left": 2}

        def refuse(op, a):
            if op == "act.cast" and n["left"] > 0:
                n["left"] -= 1
                return "character is in a conversation (InDialog)"
            return None
        self.g.refuse_if = refuse
        self.g.ui = {"screen": "dialogue", "dialogue": {"text": "Ketheric: ...", "listening": True}}
        out = turn_tool.combat_turn(self.plan(action="Shout_Dodge"))
        self.assertIn("Shout_Dodge: done", out)
        self.assertIn("for the mid-fight lines)", out)
        # an answer on screen: handed back at once
        n["left"] = 99
        self.g.combat = combat()
        self.g.ui = {"screen": "dialogue", "dialogue": {"text": "?", "options": [{"i": 1, "text": "Never."}]}}
        out = turn_tool.combat_turn(self.plan(action="Shout_Dodge"))
        self.assertIn("needs your answer", out)

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
        # + the old 3 s HUD watch: "didn't end it either", the next combat_turn refused, end_turn found nothing
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

    def test_downed_member_is_not_who_acts(self):
        p1 = next(o for o in self.g.combat["order"] if o["uuid"] == P1)
        p1.update(hp_pct=0, downed=True)
        self.g.combat["turn_group"] = [P1, P2]
        self.assertEqual(turn.party_acting(self.g.combat, {P1, P2}), [P2])

    # S018 04:12: Wyll's and Shadowheart's turns (the heal for Claude) stopped on "e4 Claude is downed",
    # though Claude was down before the turn started.
    def test_ally_already_downed_does_not_stop_the_turn(self):
        self.g.watch["party"][1]["downed"] = True
        out = turn_tool.combat_turn(self.plan(action={"ability": "Target_MainHandAttack", "target": self.gob}))
        self.assertNotIn("STOPPED", out)
        self.assertIn("end_turn: done", out)

    # S033 18:41: Wyll's AoO mid-turn dropped him below the HP threshold and the watcher cancelled his cast.
    def test_hp_drop_mid_turn_does_not_cancel_the_queued_cast(self):
        def aoo(op, a):
            if op == "act.cast":
                self.g.watch["party"][0]["hp"] = 4   # 20 % < 35 %
        self.g.on_start = aoo
        self.g.dur["act.cast"] = 0.2
        hit = {"ability": "Target_MainHandAttack", "target": self.gob}
        out = turn_tool.combat_turn(self.plan(action=hit, extra=[[dict(hit)]]))
        self.assertIn("action: Target_MainHandAttack e1: done", out)   # the running cast finished
        self.assertNotIn("INTERRUPTED", out)
        self.assertIn("STOPPED: e4 Claude HP 4/20 (HP dropped mid-turn", out)
        self.assertIn("turn NOT ended", out)
        self.assertEqual(len(self.g.ops("act.cast")), 1)                # the extra attack waits for the player
        self.assertEqual(self.g.ops("act.end_turn"), [])

    # review M6: the hold was checked at slot boundaries only; a slot's next alternative still spent after a drop
    def test_hp_drop_during_an_alternative_stops_before_the_next_one(self):
        def aoo(op, a):
            if op == "act.cast":
                self.g.watch["party"][0]["hp"] = 4   # 20 % < 35 %
        self.g.on_start = aoo
        self.g.dur["act.cast"] = 0.4
        view = self.g._view

        def failing(jid):  # the first cast runs (the AoO lands meanwhile), then fails late
            j = self.g.jobs[jid]
            if (j["status"] == "running" and j["args"].get("spell") == "Target_MainHandAttack"
                    and time.monotonic() >= j["end"]):
                j["status"], j["error"] = "failed", "the attack was cancelled"
            return view(jid)
        self.g._view = failing
        out = turn_tool.combat_turn(self.plan(action=[{"ability": "Target_MainHandAttack", "target": self.gob},
                                                   {"ability": "Projectile_FireBolt", "target": self.gob}]))
        self.assertEqual(self.casts(), ["Target_MainHandAttack"], out)
        self.assertIn("action: Target_MainHandAttack e1: FAILED", out)
        self.assertIn("STOPPED: e4 Claude HP 4/20 (HP dropped mid-turn", out)
        self.assertEqual(self.g.ops("act.end_turn"), [])

    def test_hp_drop_during_the_action_skips_then_move(self):
        def aoo(op, a):
            if op == "act.cast":
                self.g.watch["party"][0]["hp"] = 4
        self.g.on_start = aoo
        self.g.dur["act.cast"] = 0.2
        out = turn_tool.combat_turn(self.plan(move=self.gob, action=[{"ability": "Shout_Dash", "then_move": True}]))
        self.assertIn("STOPPED: e4 Claude HP 4/20 (HP dropped mid-turn", out)
        self.assertEqual(len(self.g.ops("act.move_to")), 1, out)   # the move before the Dash only
        self.assertEqual(self.g.ops("act.end_turn"), [])

    def test_hp_below_condition_still_decides_inside_the_turn(self):
        self.g.watch["party"][0]["hp"] = 4
        self.g.actions = [{"spell": "Shout_SecondWind"}, {"spell": "Target_MainHandAttack"}]
        out = turn_tool.combat_turn(self.plan(action={"ability": "Target_MainHandAttack", "target": self.gob},
                                           bonus={"ability": "Shout_SecondWind", "if": "hp_below:50"}))
        self.assertIn("Shout_SecondWind", self.casts())

    def test_ally_going_down_mid_turn_still_stops_it(self):
        def down(op, a):
            if op == "act.cast":
                self.g.watch["party"][1]["downed"] = True
        self.g.on_start = down
        self.g.dur["act.cast"] = 0.2
        out = turn_tool.combat_turn(self.plan(action={"ability": "Target_MainHandAttack", "target": self.gob}))
        self.assertIn("is downed", out)
        self.assertIn("turn NOT ended", out)

    # S002: the planned imp died before Shadowheart's cast -> "skipped (already dead)", action unused.
    def test_dead_target_retargets(self):
        self.g.dead.add(GOB)
        self.g.actions = [{"spell": "Target_SacredFlame", "range": 18}]
        out = turn_tool.combat_turn(self.plan(action={"ability": "Target_SacredFlame", "target": self.gob}))
        self.assertEqual([a["target"] for _, a in self.g.ops("act.cast")], [IMP])
        self.assertIn("already dead → aimed at", out)  # re-aimed at once (no skip, no fallback pass)

    # review L4: the re-aim dropped every other pick (a 3-dart Magic Missile became one dart on the new target)
    def test_dead_first_pick_is_replaced_in_targets_only(self):
        self.g.dead.add(GOB)
        self.g.actions = [{"spell": "Projectile_MagicMissile", "range": 18}]
        turn_tool.combat_turn(self.plan(action={"ability": "Projectile_MagicMissile", "target": self.gob,
                                             "targets": [self.gob, self.imp, self.gob]}))
        # target + targets = 4 picks; the hotbar route is off, so the 2-target cast takes target + the 1st pick
        # (before: targets popped, one dart). A9: that pair is the imp twice, UseSpell(imp, imp), which the game never
        # started (S066 #59): the cast goes at the imp alone, which takes every dart
        self.assertEqual([(a.get("target"), a.get("targets")) for _, a in self.g.ops("act.cast")], [(IMP, None)])

    def test_dead_picks_are_dropped_from_targets(self):
        self.g.dead.add(IMP)
        self.g.actions = [{"spell": "Projectile_MagicMissile", "range": 18}]
        out = turn_tool.combat_turn(self.plan(action={"ability": "Projectile_MagicMissile",
                                                   "targets": [self.gob, self.imp]}))
        self.assertEqual([a.get("targets") for _, a in self.g.ops("act.cast")], [[GOB]])
        self.assertIn("dropped dead e2 Imp from the picks", out)

    def test_retarget_respects_reach_sight_and_the_off_switch(self):
        self.g.dead.add(GOB)
        self.g.actions = [{"spell": "Target_MainHandAttack", "range": 1.5}]
        plan = {"action": {"ability": "Target_MainHandAttack", "target": self.gob}}
        turn_tool.combat_turn(self.plan(**plan))  # the imp is 12 m away: out of melee reach
        self.assertEqual(self.g.ops("act.cast"), [])
        imp = next(o for o in self.g.combat["order"] if o["uuid"] == IMP)
        imp["from"][P1] = [2.2, 0]  # in reach (edge to edge) but out of sight
        self.g.combat["current"] = P1
        turn_tool.combat_turn(self.plan(**plan))
        self.assertEqual(self.g.ops("act.cast"), [])
        imp["from"][P1] = [2.2, 1]
        self.g.combat["current"] = P1
        turn_tool.combat_turn(self.plan(**plan, retarget=False))
        self.assertEqual(self.g.ops("act.cast"), [])
        self.g.combat["current"] = P1
        turn_tool.combat_turn(self.plan(**plan))
        self.assertEqual([a["target"] for _, a in self.g.ops("act.cast")], [IMP])

    # S033 18:39-18:41: Umbral Tremors left the fight; act.target_state said only visible=false, 4 turns of
    # skipped attacks. Now "is gone (…)" and the attack is re-aimed at once (the Dodge after it is not used).
    def test_gone_target_says_gone_and_reaims_at_once(self):
        turn_plan._turn_sides[GOB] = "hostile"  # on a turn order read earlier: the party saw it on the bar
        self.g.visible.discard(GOB)
        self.g.gone[GOB] = "left_fight"
        next(o for o in self.g.combat["order"] if o["uuid"] == IMP)["from"][P1] = [1.9, 1]
        self.g.actions = [{"spell": "Target_MainHandAttack", "range": 1.5}, {"spell": "Shout_Dodge"}]
        hit = {"ability": "Target_MainHandAttack", "target": self.gob}
        out = turn_tool.combat_turn(self.plan(action=[hit, "Shout_Dodge"], extra=[[dict(hit)]]))
        self.assertIn(f"{self.gob} Goblin is gone (it is no longer in the turn order) → aimed at "
                      f"{self.imp} Imp", out)
        self.assertNotIn("not visible", out)
        self.assertEqual([(a.get("spell"), a.get("target")) for _, a in self.g.ops("act.cast")],
                         [("Target_MainHandAttack", IMP), ("Target_MainHandAttack", IMP)])

    def test_gone_target_nobody_in_reach_skips_and_says_gone(self):
        turn_plan._turn_sides[GOB] = "hostile"  # on a turn order read earlier: the party saw it on the bar
        self.g.visible.discard(GOB)
        self.g.gone[GOB] = "left_fight"
        self.g.actions = [{"spell": "Target_MainHandAttack", "range": 1.5}, {"spell": "Shout_Dodge"}]
        out = turn_tool.combat_turn(self.plan(action=[{"ability": "Target_MainHandAttack", "target": self.gob},
                                                   "Shout_Dodge"]))
        self.assertIn("is gone (it is no longer in the turn order)", out)  # imp 12 m: out of melee reach
        self.assertEqual(self.casts(), ["Shout_Dodge"])

    def test_hidden_target_is_not_reaimed_at_once(self):
        # only out of sight (still in the fight): the old skip + auto fallback, not "gone"
        self.g.visible.discard(GOB)
        next(o for o in self.g.combat["order"] if o["uuid"] == IMP)["from"][P1] = [1.9, 1]
        self.g.actions = [{"spell": "Target_MainHandAttack", "range": 1.5}]
        out = turn_tool.combat_turn(self.plan(action={"ability": "Target_MainHandAttack", "target": self.gob}))
        self.assertIn("not visible", out)
        self.assertNotIn("is gone", out)
        self.assertIn("(auto fallback)", out)

    def test_gone_move_target_says_gone(self):
        turn_plan._turn_sides[GOB] = "hostile"  # on a turn order read earlier: the party saw it on the bar
        self.g.visible.discard(GOB)
        self.g.gone[GOB] = "left_fight"
        out = turn_tool.combat_turn(self.plan(move=self.gob,
                                           action={"ability": "Target_MainHandAttack", "target": self.gob}))
        self.assertIn(f"target: {self.gob} Goblin is gone (it is no longer in the turn order) → the plan now aims at "
                      f"the nearest visible hostile {self.imp} Imp", out)

    def test_target_never_seen_on_the_turn_order_is_only_out_of_sight(self):
        # audit C9: a character the party never saw on the turn bar: no "is gone" from the engine, only out of sight
        turn_plan._turn_sides.pop(GOB, None)
        self.g.combat["order"] = [o for o in self.g.combat["order"] if o["uuid"] != GOB]
        self.g.visible.discard(GOB)
        self.g.gone[GOB] = "left_fight"
        self.g.actions = [{"spell": "Target_MainHandAttack", "range": 1.5}]
        out = turn_tool.combat_turn(self.plan(action={"ability": "Target_MainHandAttack", "target": self.gob}))
        self.assertNotIn("is gone", out)
        self.assertIn("not visible", out)
        self.assertEqual([a.get("on_bar") for op, a in self.g.ops("act.target_state")][:1], [False])

    def test_gone_target_retarget_false_keeps_the_skip(self):
        turn_plan._turn_sides[GOB] = "hostile"  # on a turn order read earlier: the party saw it on the bar
        self.g.visible.discard(GOB)
        self.g.gone[GOB] = "left_fight"
        next(o for o in self.g.combat["order"] if o["uuid"] == IMP)["from"][P1] = [1.9, 1]
        self.g.actions = [{"spell": "Target_MainHandAttack", "range": 1.5}]
        out = turn_tool.combat_turn(self.plan(action={"ability": "Target_MainHandAttack", "target": self.gob},
                                           retarget=False))
        self.assertIn("is gone", out)
        self.assertEqual(self.g.ops("act.cast"), [])

    def test_extra_attack_retargets_after_the_first_kill(self):
        def kill(op, a):
            if op == "act.cast" and a.get("target") == GOB:
                self.g.dead.add(GOB)
            if op == "act.end_turn":
                self.g.combat["current"] = GOB
        self.g.on_start = kill
        self.g.actions = [{"spell": "Target_MainHandAttack", "range": 1.5}]
        next(o for o in self.g.combat["order"] if o["uuid"] == IMP)["from"][P1] = [1.9, 1]
        hit = {"ability": "Target_MainHandAttack", "target": self.gob}
        out = turn_tool.combat_turn(self.plan(action=hit, extra=[[hit]]))
        self.assertEqual([a["target"] for _, a in self.g.ops("act.cast")], [GOB, IMP])
        self.assertIn("extra 1: e1 Goblin is already dead → aimed at e2 Imp", out)

    def test_unplanned_bonus_heals_a_downed_ally(self):
        lae = next(o for o in self.g.combat["order"] if o["uuid"] == P2)
        lae.update(hp_pct=0, downed=True, **{"from": {P1: [7.0, 1]}})
        self.g.visible.add(P2)  # G.visible: party members always
        self.g.actions = [{"spell": "Target_HealingWord", "range": 18}, {"spell": "Shout_Dodge"}]
        out = turn_tool.combat_turn(self.plan(action="Shout_Dodge"))
        self.assertEqual(self.casts(), ["Shout_Dodge", "Target_HealingWord"])
        self.assertEqual(self.g.ops("act.cast")[1][1]["target"], P2)
        self.assertIn("bonus (auto: ally downed): Target_HealingWord", out)
        self.assertTrue(out.index("bonus (auto") < out.index("end_turn:"))

    def test_no_auto_bonus_without_a_downed_ally_or_when_planned_or_off(self):
        self.g.actions = [{"spell": "Target_HealingWord", "range": 18}]
        self.g.visible.add(P2)
        turn_tool.combat_turn(self.plan(action="Shout_Dodge"))
        lae = next(o for o in self.g.combat["order"] if o["uuid"] == P2)
        lae.update(hp_pct=0, downed=True, **{"from": {P1: [7.0, 1]}})
        for kw in ({"auto_bonus": False}, {"bonus": "Shout_SecondWind"}):
            self.g.combat["current"] = P1
            turn_tool.combat_turn(self.plan(action="Shout_Dodge", **kw))
        self.assertNotIn("Target_HealingWord", self.casts())
        lae["from"][P1] = [25.0, 1]  # out of range
        self.g.combat["current"] = P1
        turn_tool.combat_turn(self.plan(action="Shout_Dodge"))
        self.assertNotIn("Target_HealingWord", self.casts())

    def test_not_in_combat(self):
        self.g.combat = {"active": False}
        self.assertIn("nothing to play — not in combat", turn_tool.combat_turn(self.plan(action="Shout_Dodge")))

    def test_hp_drop_stops_the_turn_without_ending_it(self):
        def hurt(op, args):
            if op == "act.move_to":
                self.g.watch["party"][0]["hp"] = 4
        self.g.on_start = hurt
        out = turn_tool.combat_turn(self.plan(move=self.gob, action={"ability": "Target_MainHandAttack",
                                                                  "target": self.gob}))
        self.assertIn("STOPPED: ", out)
        self.assertIn("HP 4/20", out)
        self.assertIn("turn NOT ended", out)
        self.assertEqual(self.g.ops("act.cast"), [])
        self.assertEqual(self.g.ops("act.end_turn"), [])

    def test_turn_taken_away_mid_plan(self):
        def lose(op, args):
            if op == "act.cast":
                self.g.combat["current"] = GOB
        self.g.on_start = lose
        out = turn_tool.combat_turn(self.plan(action="Shout_Dash", bonus="Shout_SecondWind"))
        self.assertEqual(self.casts(), ["Shout_Dash"])
        self.assertIn("no longer", out)
        self.assertEqual(self.g.ops("act.end_turn"), [])


if __name__ == "__main__":
    unittest.main()

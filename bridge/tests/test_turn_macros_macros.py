"""Offline tests: the macros (loot_area, dialogue_advance, auto dialogue, travel_to, regroup, read_all,
rest_if_needed), combat_turn's background end, namesakes and openers, and the clock's stuck signal. Fixtures:
test_turn_macros.py."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from test_turn_macros import (  # sets BG3AI_RUNTIME before importing bg3ai
    BOOK,
    CHEST,
    GOB,
    LETTER,
    LOCKED,
    OWNED,
    P1,
    P2,
    POTION,
    Base,
    batch,
    combat,
    combat_tools,
    core,
    dialogue,
    macro_tools,
    macros,
    observe,
    overview,
    plumbing,
    queueing,
    reporting,
    travel,
    travel_macro,
    turn,
    turn_tool,
    world_tools,
)


class NearestNamesake(Base):
    """ACT-7: disarm/lockpick/loot_corpse by a name two things share take the one in sight nearest the actor."""

    T1, T2 = "71717171-2222-3333-4444-555555555555", "72727272-2222-3333-4444-555555555555"

    def setUp(self) -> None:
        super().setUp()
        core.H.get(self.T1, "Fire Trap")
        core.H.get(self.T2, "Fire Trap")
        self.g.nearby = {"characters": [], "items": [
            {"uuid": self.T1, "name": "Fire Trap", "pos": [9, 0, 0], "dist": 9.0},
            {"uuid": self.T2, "name": "Fire Trap", "pos": [3, 0, 0], "dist": 3.0}]}
        rpc = self.g.rpc

        def with_pos(op, args=None, context="server"):  # state.status party rows carry pos (State.lua member rows)
            r = rpc(op, args, context)
            if op == "state.status":
                for row in r["party"]:
                    row["pos"] = next(m["pos"] for m in self.g.watch["party"] if m["uuid"] == row["uuid"])
            return r
        self.g.rpc = with_pos

    def test_the_nearest_in_sight_is_meant(self):
        self.assertEqual(plumbing._nearest_uuid("Fire Trap", P1), self.T2)
        # from Lae'zel's side the other one is nearer: distances are the actor's, not the active member's
        self.g.watch["party"][1]["pos"] = [10, 0, 0]
        self.assertEqual(plumbing._nearest_uuid("Fire Trap", P2), self.T1)

    def test_a_tie_or_none_in_sight_stays_ambiguous(self):
        self.g.nearby["items"][0]["pos"] = [-3.2, 0, 0]
        with self.assertRaisesRegex(ValueError, "is ambiguous"):
            plumbing._nearest_uuid("Fire Trap", P1)
        self.g.nearby = {"characters": [], "items": []}
        with self.assertRaisesRegex(ValueError, "is ambiguous"):
            plumbing._nearest_uuid("Fire Trap", P1)

    def test_an_id_is_taken_as_is(self):
        self.assertEqual(plumbing._nearest_uuid(core.H.get(self.T1), P1), self.T1)


class OpenerCombat(Base):
    """acceptance 13:3x: a Ranged Attack on a dormant Shadow did damage, no combat began, nothing said so."""

    def setUp(self) -> None:
        super().setUp()
        saved, combat_tools.OPENER_WAIT_S = combat_tools.OPENER_WAIT_S, 0.05
        self.addCleanup(setattr, combat_tools, "OPENER_WAIT_S", saved)
        self.g.result_for = lambda op, a: ({"total_damage": 3, "target": GOB} if op == "act.cast" else {})

    def test_damage_without_a_fight_says_so(self):
        out = combat_tools.use_ability("Projectile_MainHandAttack", target=self.gob)
        self.assertIn(f"combat: NOT started — {self.gob} Goblin took 3 damage but no fight began", out)

    def test_fight_started_says_so(self):
        def start(op, a):
            if op == "act.cast":
                self.g.combat = combat()
        self.g.on_start = start
        out = combat_tools.use_ability("Projectile_MainHandAttack", target=self.gob)
        self.assertIn("COMBAT: the attack started a fight (round 2)", out)

    def test_items_party_and_in_combat_add_nothing(self):
        out = combat_tools.use_ability("Projectile_MainHandAttack", target=self.chest)
        self.assertNotIn("combat:", out.lower())
        self.g.combat = combat()
        out = combat_tools.use_ability("Projectile_MainHandAttack", target=self.gob)
        self.assertNotIn("combat: NOT", out)
        self.assertNotIn("started a fight", out)


class NextUp(unittest.TestCase):
    def test_wraps_round_and_skips_the_ended_character(self):
        cs = combat(current=P1)
        text, mine = turn.next_up(cs, {P1, P2}, lambda u: u[:2], ended=P1)
        self.assertEqual(mine, P2)
        self.assertEqual(text, "NEXT 99, 88, then 22 (yours)")
        cs["order"] = cs["order"][:3]  # only Claude and enemies: back to Claude after them
        text, mine = turn.next_up(cs, {P1}, lambda u: u[:2], ended=P1)
        self.assertEqual((text, mine), ("NEXT 99, 88, then 11 (yours)", P1))

    def test_movement_left(self):
        self.assertEqual(turn.movement_left("destination is beyond remaining movement (path 14.2 m, 5.5 m left)"), 5.5)
        self.assertTrue(turn.is_fatal("FAILED — it is not this character's turn"))


# ---------------------------------------------------------------------------
# macros
# ---------------------------------------------------------------------------

class LootArea(Base):
    def setUp(self) -> None:
        super().setUp()
        self.g.dead.add(GOB)
        self.g.nearby = {
            "characters": [{"uuid": GOB, "name": "Goblin", "relation": "hostile", "dead": True, "pos": [8, 0, 0],
                            "dist": 8.0}],
            "items": [{"uuid": CHEST, "name": "Chest", "kind": "container", "pos": [2, 0, 0], "dist": 2.0},
                      {"uuid": LOCKED, "name": "Iron Chest", "kind": "container", "locked": True, "pos": [3, 0, 0],
                       "dist": 3.0},
                      {"uuid": OWNED, "name": "Merchant's Crate", "kind": "container", "owned": True,
                       "pos": [4, 0, 0], "dist": 4.0}]}
        self.corpses: list[str] = []

        def fake_loot_corpse(corpse: str, who: str | None = None) -> str:
            self.corpses.append(corpse)
            reporting._batch["last"] = "done"
            return "loot_corpse: done"
        orig = world_tools.loot_corpse
        world_tools.loot_corpse = fake_loot_corpse
        self.addCleanup(setattr, world_tools, "loot_corpse", orig)

    def test_loots_visible_in_walking_order_and_skips_the_rest(self):
        out = macro_tools.loot_area()
        self.assertEqual(self.g.ops("act.interact")[0][1]["target"], CHEST)
        self.assertEqual(self.corpses, [self.gob])
        self.assertEqual(len(self.g.ops("ui.loot")), 2)
        self.assertLess(out.index(self.chest), out.index(f"{self.gob} Goblin:"))  # nearest first
        self.assertIn("Iron Chest (locked)", out)
        self.assertIn("Merchant's Crate (owned: taking is stealing)", out)
        self.assertIn("nothing to loot", macro_tools.loot_area())  # remembered

    def test_stops_at_the_first_interrupt(self):
        self.g.on_start = lambda op, a: op == "ui.loot" and self.g.push("combat_started", {})
        out = macro_tools.loot_area()
        self.assertIn("STOPPED: combat started; not reached: ", out)
        self.assertEqual(self.corpses, [])

    def test_an_open_loot_window_is_taken_before_stopping(self):
        # S042 22:51:18: a damage stop right after opening a corpse left its loot window unlooted
        def opened(op, a):
            if op == "act.interact":
                self.g.watch["party"] = [{"uuid": P1, "hp": 50, "max_hp": 104, "pos": [0, 0, 0]}]
        self.g.watch["party"] = [{"uuid": P1, "hp": 104, "max_hp": 104, "pos": [0, 0, 0]}]
        self.g.dur["act.interact"] = 0.0   # opened at once; the damage shows at the check after the step
        self.g.on_start = opened
        out = macro_tools.loot_area()
        self.assertIn("STOPPED: ", out)
        self.assertIn("took damage outside combat", out)
        self.assertEqual(self.g.ops("ui.loot")[:1], [("ui.loot", {"all": True})], (self.g.ops("ui.loot"), out))  # Take All ran
        self.assertIn(CHEST, macro_tools._looted, out)
        self.assertEqual(self.corpses, [])                       # the next target was not started

    def test_refused_in_combat(self):
        self.g.watch["combat"] = True
        self.assertIn("FAILED: loot_area is for outside combat", macro_tools.loot_area())

    def _in_convo(self, refusals: int) -> None:
        """The Guard refuses the first `refusals` act steps (live S021: InDialog after a fight)."""
        n = {"left": refusals}

        def refuse(op, args):
            if op.startswith("act.") and n["left"] > 0:
                n["left"] -= 1
                return "character is in a conversation (InDialog)"
            return None
        self.g.refuse_if = refuse

    def test_conversation_on_screen_stops_at_once(self):
        # S021: 4 corpses each "FAILED — character is in a conversation"; now one try, then hand it back.
        self._in_convo(99)
        self.g.ui = {"screen": "dialogue"}
        out = macro_tools.loot_area()
        self.assertIn("STOPPED: a conversation is open: answer it (choose_dialogue)", out)
        self.assertIn(f"not reached: {self.chest} Chest, {self.gob} Goblin", out)
        self.assertEqual(len(self.g.ops("act.interact")), 1)
        self.assertEqual(self.corpses, [])

    def test_stale_conversation_flag_retried(self):
        saved, macro_tools.CONVO_WAIT_S = macro_tools.CONVO_WAIT_S, 2.0
        self.addCleanup(setattr, macro_tools, "CONVO_WAIT_S", saved)
        self._in_convo(2)
        out = macro_tools.loot_area()
        self.assertNotIn("STOPPED", out)
        self.assertNotIn("conversation", out)
        self.assertEqual(len(self.g.ops("act.interact")), 3)  # 2 refused, then opened
        self.assertEqual(self.corpses, [self.gob])

    def test_conversation_flag_that_never_clears(self):
        saved, macro_tools.CONVO_WAIT_S = macro_tools.CONVO_WAIT_S, 0.5
        self.addCleanup(setattr, macro_tools, "CONVO_WAIT_S", saved)
        self._in_convo(99)
        out = macro_tools.loot_area()
        self.assertIn("STOPPED: still in a conversation after 0.5s with no dialogue on screen", out)
        self.assertEqual(self.corpses, [])


class CombatTurnBackgroundEnd(Base):
    """Latency (naut series): the party's last end before enemy turns runs in the background (END_ASYNC); the
    agent plans while the game ends the turn (1.6-5.3 s a time, ~35 s a run blocked)."""

    def setUp(self) -> None:
        super().setUp()
        self.g.combat = combat()
        self.g.watch["combat"] = True

        def advance(op, args):  # ending the turn hands it to the goblin
            if op == "act.end_turn":
                self.g.combat["current"] = GOB
        self.g.on_start = advance
        turn_tool.END_ASYNC = True
        self.g.dur["act.end_turn"] = 0.6

    def plan(self, **kw) -> str:
        return json.dumps(kw)

    def gate_end(self) -> None:
        """The background end runs until a hold waits on it (released at the hold's first busy check) or the test
        releases it: nothing here may depend on a 0.6 s end outlasting the tool calls in between (flaky under
        full-suite load, 2026-10-17: no 'waited' in the next combat_turn, 1 of 5 full runs)."""
        self.g.dur["act.end_turn"] = 3600.0
        mgr = queueing._queues()
        busy, hold = mgr.busy, turn_tool._hold_for_bg_end
        holding = threading.local()

        def held_busy(who=None):
            b = busy(who)
            if b and getattr(holding, "on", False):
                self.release_end()  # the hold saw the end running: it waits, then sees it done
            return b

        def gated_hold(char=None):
            holding.on = True
            try:
                return hold(char)
            finally:
                holding.on = False
        for p in (mock.patch.object(mgr, "busy", held_busy), mock.patch.object(turn_tool, "_hold_for_bg_end", gated_hold)):
            p.start()
            self.addCleanup(p.stop)

    def release_end(self) -> None:
        """The end finishes at its next poll (one not started yet finishes at once)."""
        with self.g.lock:
            self.g.dur["act.end_turn"] = 0.0
            for j in self.g.jobs.values():
                if j["op"] == "act.end_turn" and j["status"] == "running":
                    j["end"] = 0.0

    def test_returns_before_the_end_finishes_and_reports_it_later(self):
        # The end is the character's queue step: its worker thread sends act.end_turn after combat_turn returned (or
        # just before). Flaky under full-suite load when this asserted the op at once (se, 2026-10-16: 0 != 1 in 1 of
        # 4 full runs, 10/10 alone): wait for it, bounded. The end runs until released, so "returned first" is no race.
        self.gate_end()
        out = turn_tool.combat_turn(self.plan(action="Shout_Dodge"))
        self.assertIn("end_turn: ending in the background", out)
        self.assertIn(f"NEXT {self.gob} Goblin", out)
        self.assertIn("RUNNING queue", out)
        until = time.monotonic() + 3.0
        while not self.g.ops("act.end_turn") and time.monotonic() < until:
            time.sleep(0.01)
        self.assertEqual(len(self.g.ops("act.end_turn")), 1)
        self.assertNotIn("queue_done", observe.get_events())  # still running
        self.release_end()
        evs = ""
        until = time.monotonic() + 5.0
        while "queue_done" not in evs and time.monotonic() < until:
            time.sleep(0.1)
            evs += observe.get_events()
        self.assertIn("queue_done", evs)
        self.assertEqual(len(self.g.ops("act.end_turn")), 1)

    def test_next_combat_turn_waits_for_the_background_end(self):
        self.gate_end()
        turn_tool.combat_turn(self.plan(action="Shout_Dodge"))
        self.g.combat["current"] = P1  # the enemies acted; Claude again
        out = turn_tool.combat_turn(self.plan(action="Shout_Dodge"))
        self.assertRegex(out, r"waited [\d.]+s for e\d+ Claude's turn end to finish first")
        ends = [i for i, (o, _) in enumerate(self.g.calls) if o == "act.end_turn"]
        casts = [i for i, (o, _) in enumerate(self.g.calls) if o == "act.cast"]
        self.assertLess(ends[0], casts[1])

    def test_a_direct_action_for_that_character_waits_too(self):
        self.gate_end()
        turn_tool.combat_turn(self.plan(action="Shout_Dodge"))
        out = combat_tools.use_ability("Shout_Dodge")
        self.assertIn("turn end to finish first", out)

    def test_review_m4_queued_steps_wait_and_get_their_own_interrupts(self):
        self.gate_end()
        # the end's executor ignores every interrupt: steps queued for that character during it must not run in it
        turn_tool.combat_turn(self.plan(action="Shout_Dodge"))
        out = batch.queue_actions(json.dumps([{"tool": "use_ability", "ability": "Shout_Dodge"}]),
                                   who=core.H.get(P1))
        self.assertIn("turn end to finish first", out)
        q = queueing._queues().queues[P1]
        ex = q.executor
        self.assertEqual(ex.watcher.ignore, set())               # not the end's all-kinds set
        # queue_actions returns before its worker sends the cast (flaky under full-suite load, 2026-10-17: the cast
        # list still ended with combat_turn's own cast, before the end): wait for that worker, bounded
        q.thread.join(5.0)
        self.assertFalse(q.thread.is_alive())
        ends = [i for i, (o, _) in enumerate(self.g.calls) if o == "act.end_turn"]
        casts = [i for i, (o, _) in enumerate(self.g.calls) if o == "act.cast"]
        self.assertLess(ends[0], casts[-1])

    def test_review_m5_cancel_queue_never_cancels_the_end_half_way(self):
        self.gate_end()
        turn_tool.combat_turn(self.plan(action="Shout_Dodge"))
        until = time.monotonic() + 3.0
        while not self.g.ops("act.end_turn") and time.monotonic() < until:
            time.sleep(0.01)
        out = batch.cancel_queue()
        self.assertIn("turn end to finish first", out)
        self.assertEqual(self.g.ops("job.cancel"), [])
        end = next(j for j in self.g.jobs.values() if j["op"] == "act.end_turn")
        core.game.rpc("job.poll", {"id": next(k for k, j in self.g.jobs.items() if j is end)})
        self.assertEqual(end["status"], "done")

    def test_review_m5_replace_waits_for_the_end_too(self):
        self.gate_end()
        turn_tool.combat_turn(self.plan(action="Shout_Dodge"))
        out = batch.queue_actions(json.dumps([{"tool": "use_ability", "ability": "Shout_Dodge"}]),
                                   who=core.H.get(P1), replace=True)
        self.assertIn("turn end to finish first", out)
        self.assertFalse([j for j in self.g.jobs.values() if j["op"] == "act.end_turn"
                          and j["status"] == "interrupted"])

    def test_s084_a_member_shown_open_while_another_end_runs_names_the_running_end(self):
        # S084 (7x in one fight): Gale's background end (the group's last) still running, the game showed Shadowheart
        # (ended earlier) as open: Claude's plan was refused "e3 Shadowheart (yours) must act first … end it", and
        # the end_turn on her found nothing to end
        p3 = "33333333-2222-3333-4444-555555555555"
        self.g.more_party = [{"uuid": p3, "name": "Shadowheart"}]
        self.g.combat["order"].append({"uuid": p3, "name": "Shadowheart", "relation": "party"})
        self.g.combat["current"] = p3
        self.g.on_start = lambda op, a: None                 # the end never hands the turn on here
        self.g.dur["act.end_turn"] = 3600.0
        self.addCleanup(self.release_end)
        self.assertTrue(turn_tool._end_in_background(P2))
        with mock.patch.object(turn_tool, "END_BG_HOLD_S", 0.2):
            out = turn_tool.combat_turn(self.plan(action="Shout_Dodge"), who=core.H.get(P1), wait_turn=5)
        self.assertIn("Lae'zel's turn end is still running in the background", out)
        self.assertIn("Shadowheart's turn open meanwhile", out)
        self.assertNotIn("must act first", out)
        self.assertIn("waited 0.2s for", out)                # the hold's wait is reported, not only "waited 0.0s"
        self.assertEqual(self.g.ops("act.cast"), [])

    def test_a_shared_turn_group_member_left_ends_inline(self):
        self.g.combat["turn_group"] = [P1, P2]
        self.g.combat["current"] = P1

        def advance(op, args):
            if op == "act.end_turn":
                self.g.combat["turn_group"] = [P2]
        self.g.on_start = advance
        out = turn_tool.combat_turn(json.dumps({}), who=core.H.get(P1))
        self.assertIn("end_turn: done", out)
        self.assertNotIn("background", out)

    def test_next_turn_ours_ends_inline_and_shows_its_actions(self):
        self.g.combat["order"] = [o for o in self.g.combat["order"] if o["uuid"] in (P1, P2)] + \
            [o for o in self.g.combat["order"] if o["uuid"] not in (P1, P2)]
        self.g.on_start = lambda op, a: op == "act.end_turn" and self.g.combat.update(current=P2)
        out = turn_tool.combat_turn(self.plan(action="Shout_Dodge"))
        self.assertIn("end_turn: done", out)
        self.assertIn("'s turn: plan it", out)

    def test_wait_next_blocks_as_before(self):
        self.g.on_start = lambda op, a: op == "act.end_turn" and self.g.combat.update(current=P2)
        out = turn_tool.combat_turn(self.plan(action="Shout_Dodge"), wait_next=True)
        self.assertNotIn("background", out)

    def test_off_switch(self):
        turn_tool.END_ASYNC = False
        out = turn_tool.combat_turn(self.plan(action="Shout_Dodge"))
        self.assertIn("end_turn: done", out)


class DialogueAdvance(Base):
    def test_advances_filler_and_stops_at_a_choice(self):
        choice = {"screen": "dialogue", "dialogue": {"text": "Choose.", "options": [
            {"i": 1, "text": "Yes"}, {"i": 2, "text": "No"}]}}
        self.g.ui_seq = [{"screen": "dialogue", "dialogue": {"text": "Hello there.", "continue": True}},
                         {"screen": "dialogue", "dialogue": {"text": "Listen.", "options": [{"i": 1, "text": "..."}]}},
                         choice]
        out = macro_tools.dialogue_advance()
        self.assertEqual([a["i"] for _, a in self.g.ops("ui.dialogue_choose")], [0, 1])
        self.assertIn("2 filler line(s)", out)
        self.assertIn("STOPPED: a real choice (2 options)", out)
        self.assertIn("2. No", out)

    def test_never_picks_a_tagged_or_committing_option(self):
        for opt in ({"i": 1, "text": "Let me through.", "tags": ["PERSUASION"]}, {"i": 1, "text": "Attack them."}):
            self.g.ui_seq = [{"screen": "dialogue", "dialogue": {"options": [opt]}}]
            out = macro_tools.dialogue_advance()
            self.assertIn("0 filler line(s)", out)
        self.assertEqual(self.g.ops("ui.dialogue_choose"), [])

    def test_conversation_over(self):
        self.assertIn("STOPPED: conversation ended", macro_tools.dialogue_advance())

    def test_nobody_in_a_conversation_returns_at_once(self):
        # naut series (latency): "0 filler line(s), STOPPED: conversation ended" took 2.7 s each (the whole 2.5 s
        # open window), 2-3 calls a run. The game says no party member is in one: wait DIALOGUE_IDLE_OPEN_S only.
        self.g.activity = {"in_dialogue": False}
        t = time.monotonic()
        out = macro_tools.dialogue_advance()
        self.assertLess(time.monotonic() - t, macro_tools.DIALOGUE_IDLE_OPEN_S + 0.6)
        self.assertIn("STOPPED: conversation ended", out)
        self.assertEqual(len(self.g.ops("nav.activity")), 1)

    def test_a_conversation_still_opening_gets_the_whole_window(self):
        self.g.activity = {"in_dialogue": True}  # started on the server, not drawn yet
        self.g.ui_seq = [{"screen": "game"}] * 4 + [{"screen": "dialogue", "dialogue": {"text": "Choose.", "options": [
            {"i": 1, "text": "Yes"}, {"i": 2, "text": "No"}]}}]
        out = macro_tools.dialogue_advance()
        self.assertIn("STOPPED: a real choice (2 options)", out)

    # naut series (helm): "*The helm's alien transponder - you've made it in time.*" in the party's combat turn.
    HELM = {"screen": "dialogue", "dialogue": {"speaker": "Narrator", "listening": True,
                                               "text": "*The helm's alien transponder - you've made it in time.*"}}

    def test_a_line_autoskip_cannot_skip_hands_back_early(self):
        self.g.ui = self.HELM
        for blocked, says in (("our turn", "AutoSkip doesn't skip lines in your combat turn"),
                              ("tries", "still up after 8 skips")):
            self.g.autoskip = {"on": True, "line": {"id": 7, "tries": 8, "age": 4000, "blocked": blocked}}
            t = time.monotonic()
            out = macro_tools.dialogue_advance()
            self.assertLess(time.monotonic() - t, 2.5)  # not DIALOGUE_LINE_WAIT_S (15 s)
            self.assertIn("STOPPED: line still playing (a line playing 4s, " + says, out)
            self.assertIn("wait_for dialogue_ended or level_changed", out)
        self.g.autoskip = {"on": True, "line": {"id": 7, "notext": True, "tries": 60, "age": 61000, "blocked": "tries"}}
        self.assertIn("a cinematic shot playing 61s", macro_tools.dialogue_advance())
        self.g.autoskip = {"on": False}
        self.assertIn("AutoSkip is off", macro_tools.dialogue_advance())

    def test_a_line_autoskip_is_skipping_is_waited_for(self):
        # Skipping (no blocked) or too new: the usual wait, and the next prompt is clicked through.
        self.g.autoskip = {"on": True, "line": {"id": 7, "tries": 2, "age": 1500, "turn": True}}
        self.g.ui_seq = [self.HELM] * 8 + [{"screen": "dialogue", "dialogue": {"text": "Choose.", "options": [
            {"i": 1, "text": "Yes"}, {"i": 2, "text": "No"}]}}]
        out = macro_tools.dialogue_advance()
        self.assertIn("STOPPED: a real choice (2 options)", out)
        self.assertGreaterEqual(len(self.g.ops("autoskip.status")), 1)

    def test_stuck_exit_off_switch_and_old_mod_wait_the_full_window(self):
        self.g.ui = self.HELM
        for exit_s, status in ((0.0, {"on": True, "line": {"tries": 8, "age": 9000, "blocked": "our turn"}}),
                               (2.0, None)):
            self.g.calls.clear()
            self.g.autoskip = status
            with mock.patch.object(macro_tools, "DIALOGUE_STUCK_EXIT_S", exit_s), \
                    mock.patch.object(macro_tools, "DIALOGUE_LINE_WAIT_S", 1.2):
                t = time.monotonic()
                out = macro_tools.dialogue_advance()
            self.assertGreaterEqual(time.monotonic() - t, 1.2)
            self.assertIn("STOPPED: line still playing", out)
            self.assertNotIn("line still playing (", out)
            if exit_s == 0:
                self.assertEqual(self.g.ops("autoskip.status"), [])

    def test_a_read_tool_waits_briefly_on_a_playing_line(self):
        # naut-r2: get_combat_state (dialogue_started in its events) took 15.4 s on the helm line.
        self.g.ui = self.HELM
        self.g.autoskip = {"on": True, "line": {"id": 7, "tries": 1, "age": 300, "turn": True}}
        with mock.patch.object(macro_tools, "AUTO_DIALOGUE_READ_LINE_S", 0.6):
            t = time.monotonic()
            out = reporting._auto_dialogue("get_combat_state", "COMBAT round=6", "dialogue_started []")
            took = time.monotonic() - t
        self.assertLess(took, 1.5)
        self.assertIn("STOPPED: line still playing", out)
        self.assertEqual(macro_tools.AUTO_DIALOGUE_READ_LINE_S, 3.0)  # the default; a talk_to/wait_for keeps 15 s

    def test_filler_rules(self):
        self.assertEqual(macros.dialogue_filler({"continue": True}), (True, 0, ""))
        self.assertFalse(macros.dialogue_filler({"listening": True})[0])
        self.assertFalse(macros.dialogue_filler({"options": [{"i": 1, "text": "Give him the gold."}]})[0])
        self.assertEqual(macros.dialogue_filler({"options": [{"i": 1, "text": "Hm."}]})[:2], (True, 1))
        self.assertEqual(macros.dialogue_filler({"options": [{"i": 1, "text": "x"}, {"i": 2, "text": "y", "off": True}]})[:2],
                         (True, 1))


class AutoDialogue(Base):
    """Fast reflexes: the bridge clicks through opening/follow-up filler itself (never a real answer)."""
    CHOICE = {"screen": "dialogue", "dialogue": {"text": "Choose.", "options": [
        {"i": 1, "text": "Yes"}, {"i": 2, "text": "No"}]}}

    def test_talk_to_shows_the_first_real_choice(self):
        self.g.ui_seq = [{"screen": "game"},
                         {"screen": "dialogue", "dialogue": {"text": "Hello there.", "continue": True}},
                         self.CHOICE]
        out = travel.talk_to(self.gob)
        self.assertEqual([a["i"] for _, a in self.g.ops("ui.dialogue_choose")], [0])
        self.assertIn("auto-advanced 1 opening filler line(s)", out)
        self.assertIn("STOPPED: a real choice (2 options)", out)
        self.assertIn("2. No", out)

    def test_choose_dialogue_clicks_through_following_filler(self):
        self.g.ui_seq = [{"screen": "dialogue", "dialogue": {"text": "Hm.", "options": [{"i": 1, "text": "..."}]}},
                         self.CHOICE]
        out = dialogue.choose_dialogue(2)
        self.assertEqual([a["i"] for _, a in self.g.ops("ui.dialogue_choose")], [2, 1])
        self.assertIn("then auto-advanced 1 filler line(s)", out)
        self.assertIn("STOPPED: a real choice (2 options)", out)

    def test_choose_dialogue_never_picks_a_committing_or_tagged_answer(self):
        for opt in ({"i": 1, "text": "Let me through.", "tags": ["PERSUASION"]}, {"i": 1, "text": "Attack them."}):
            self.g.calls.clear()
            self.g.ui_seq = [{"screen": "dialogue", "dialogue": {"text": "So?", "options": [opt]}}]
            out = dialogue.choose_dialogue(1)
            self.assertEqual([a["i"] for _, a in self.g.ops("ui.dialogue_choose")], [1])
            self.assertNotIn("auto-advanced", out)
            self.assertIn(opt["text"], out)

    def test_choose_dialogue_end(self):
        self.g.ui = {"screen": "game"}
        out = dialogue.choose_dialogue(1)
        self.assertIn("STOPPED: conversation ended", out)

    def test_plain_interact_adds_nothing_and_stays_quick(self):
        t = time.monotonic()
        out = world_tools.interact(self.chest)
        self.assertLess(time.monotonic() - t, 1.5)
        self.assertNotIn("DIALOGUE", out)
        self.assertEqual(self.g.ops("ui.dialogue_choose"), [])

    def test_dialogue_started_event_after_a_walk(self):
        self.g.push("dialogue_started", {"speakers": [{"name": "Shadowheart"}]})
        self.g.ui_seq = [self.CHOICE]
        out = batch.wait_for("idle", timeout_s=0.1)
        self.assertIn("1. Yes", out)
        self.assertEqual(out.count("1. Yes"), 1)
        # A result that already shows the dialogue (get_status) is left alone.
        self.g.push("dialogue_started", {"speakers": [{"name": "Shadowheart"}]})
        self.assertNotIn("STOPPED", overview.get_status())

    def test_ambient_dialogue_event_adds_nothing(self):
        self.g.push("dialogue_started", {"speakers": [{"name": "Lae'zel"}]})
        out = batch.wait_for("idle", timeout_s=0.1)
        self.assertNotIn("STOPPED", out)

    def test_autopilot_never_says_a_lone_spoken_answer(self):
        # dialogue_advance (asked for) may take "Hm."; the bridge's own autopilot leaves any spoken line to the agent.
        self.g.ui_seq = [{"screen": "dialogue", "dialogue": {"text": "Well?", "options": [{"i": 1, "text": "Hm."}]}}]
        out = dialogue.choose_dialogue(1)
        self.assertEqual([a["i"] for _, a in self.g.ops("ui.dialogue_choose")], [1])
        self.assertIn("the only answer is yours to give: 'Hm.'", out)

    def test_skill_check_options_show_the_speakers_modifier_not_the_dc(self):
        self.g.sheets = {P1: {"skills": {"Persuasion": 3, "Intimidation": 5, "SleightOfHand": 0},
                              "abilities": {"str": 17, "wis": 10}},
                         P2: {"skills": {"Persuasion": 6, "Intimidation": 2}, "abilities": {"str": 16}}}
        reporting._sheet_cache.clear()
        self.g.push("dialogue_started", {"party": [{"uuid": P1, "name": "Claude"}], "speakers": []})
        observe.get_events()
        self.g.ui = {"screen": "dialogue", "dialogue": {"text": "Halt.", "options": [
            {"i": 1, "text": "Let me through.", "tags": ["PERSUASION"]},
            {"i": 2, "text": "Move.", "tags": ["INTIMIDATION"]},
            {"i": 3, "text": "Shove him.", "tags": ["STRENGTH"]},
            {"i": 4, "text": "Pray.", "tags": ["CLERIC"]},
            {"i": 5, "text": "Use the tadpole.", "tags": ["ILLITHID"]},
            {"i": 6, "text": "Leave."}], "dc": 15}}
        out = observe.get_dialogue()
        claude, laezel = core.H.get(P1), core.H.get(P2)
        self.assertIn(f"1. [PERSUASION] Let me through.  ← Persuasion check: {claude} +3 (party best {laezel} +6", out)
        self.assertIn(f"2. [INTIMIDATION] Move.  ← Intimidation check: {claude} +5", out)
        self.assertNotIn("party best", out.split("\n")[[i for i, l in enumerate(out.split("\n")) if "2. [" in l][0]])
        self.assertIn(f"Strength check: {claude} +3", out)
        self.assertIn("4. [CLERIC] Pray.  ← class/race/background option", out)
        self.assertIn("tadpole's power", out)
        self.assertIn("6. Leave.", out)
        self.assertNotIn("15", out.split("EVENTS")[0].replace("+15", ""))

    def test_off_switch(self):
        saved, macro_tools.AUTO_DIALOGUE = macro_tools.AUTO_DIALOGUE, False
        self.addCleanup(setattr, macro_tools, "AUTO_DIALOGUE", saved)
        self.g.ui_seq = [{"screen": "dialogue", "dialogue": {"text": "Hello there.", "continue": True}}]
        out = travel.talk_to(self.gob)
        self.assertNotIn("STOPPED", out)
        self.assertEqual(self.g.ops("ui.dialogue_choose"), [])


class TravelTo(Base):
    def setUp(self) -> None:
        super().setUp()
        plumbing.SETTLE_S = 10.0  # the trips end within the settle window: their whole report comes back
        (self.tmp / "markers.json").write_text(json.dumps([{"id": "m1", "label": "Old well", "pos": [50, 0, 0],
                                                             "level": "TUT"}]))
        self.g.result_for = lambda op, a: {"after": {"pos": a.get("pos")}} if op == "act.move_to" else {}

        def reach(a):
            here = self.g.ops("act.move_to")
            x = here[-1][1]["pos"][0] if here else 0
            return {"reachable": True, "pos": [min(50.0, x + a["max_m"]), 0.0, 0.0]}
        self.g.reach = reach

    def test_chained_walks_to_a_marker(self):
        out = travel_macro.travel_to(marker="well", wait=True)
        self.assertEqual([a["pos"][0] for _, a in self.g.ops("act.move_to")], [30.0, 50.0])
        self.assertIn("travel_to marker m1 Old well: 50m", out)
        self.assertIn("arrived", out)

    def test_objective_that_moved_on_is_named_on_arrival(self):
        # naut2 run 1: o1 "arrived (0.7m from it)", then the marker stood 66.8m W; 3 calls to find out
        seen = [[{"label": "Escape Route", "pos": [50.0, 0.0, 0.0], "dist": 50.0, "dir": "E", "here": True}],
                [{"label": "Escape Route", "pos": [50.0, 0.0, 66.0], "dist": 66.8, "dir": "N", "here": True}]]
        calls = []
        overview._objectives["last"] = []
        overview._objective_ids.clear()  # o1 = this marker, whatever ids earlier tests handed out

        def markers(max_age=0.0):
            calls.append(max_age)
            arrived = any(a["pos"][0] >= 50.0 for _, a in self.g.ops("act.move_to"))
            ms = [dict(m) for m in seen[1 if arrived else 0]]
            overview._set_objectives(ms)
            return ms
        orig = overview._objective_markers
        overview._objective_markers = markers
        self.addCleanup(setattr, overview, "_objective_markers", orig)
        out = travel_macro.travel_to(objective="o1", wait=True)
        self.assertIn("arrived", out)
        self.assertIn('o1 Escape Route moved on: now 66.8m N: travel_to(objective="o1") again', out)
        self.assertEqual(calls[-1], 0.0)  # a fresh read on arrival, not the cached markers

    def test_no_way_further(self):
        self.g.reach = {"reachable": True, "pos": [0.2, 0, 0]}
        out = travel_macro.travel_to(x=50.0, y=0.0, z=0.0, wait=True)
        self.assertIn("STOPPED: no walkable ground leads further", out)
        self.assertEqual(self.g.ops("act.move_to"), [])

    def test_no_way_further_says_why_the_games_route_was_not_taken(self):
        # live window #2: inside the Last Light Inn the straight legs dead-ended 106 m short, the reason unsaid
        self.g.reach = {"reachable": True, "pos": [0.2, 0, 0], "direct_why": "partial"}
        out = travel_macro.travel_to(x=50.0, y=0.0, z=0.0, wait=True)
        self.assertIn("; the game's path ends short of it", out)
        out = travel_macro.travel_to(x=20.0, y=0.0, z=0.0, wait=True)  # one sampled leg: no route asked about
        self.assertIn("STOPPED: no walkable ground leads further", out)
        self.assertNotIn("the game's path", out)

    def test_interrupt_stops_the_trip(self):
        self.g.on_start = lambda op, a: op == "act.move_to" and self.g.push("dialogue_started", {})
        out = travel_macro.travel_to(marker="m1", wait=True)
        self.assertIn("STOPPED: a conversation started", out)
        self.assertEqual(len(self.g.ops("act.move_to")), 1)

    def test_stalled_legs_end_the_trip(self):
        # S001: after the landing, every leg came back "stopped_short ... path blocked" at the same spot.
        self.g.reach = {"reachable": True, "pos": [20.0, 0.0, 0.0]}
        self.g.result_for = lambda op, a: ({"stopped_short": True, "reason": "path blocked",
                                            "after": {"pos": [5.0, 0.0, 0.0]}} if op == "act.move_to" else {})
        out = travel_macro.travel_to(x=50.0, y=0.0, z=0.0, wait=True)
        # leg 1 gets 5 m closer (progress), legs 2 and 3 end at the same spot: stop after 2 stalls
        self.assertEqual(len(self.g.ops("act.move_to")), 3)
        self.assertIn("STOPPED: stopped short 2 legs in a row", out)

    def test_fire_on_the_only_way_stops_before_walking_into_it(self):
        self.g.reach = {"reachable": False, "hazard": {"surface": "Fire", "pos": [-18, 18, -424], "at_m": 6}}
        out = travel_macro.travel_to(x=50.0, y=0.0, z=0.0, wait=True)
        self.assertIn("STOPPED: the way on crosses Fire 6m ahead", out)
        self.assertEqual(self.g.ops("act.move_to"), [])

    def test_legs_report_the_surface_they_kept_clear_of(self):
        def reach(a):
            return {"reachable": True, "pos": [50.0, 0.0, 0.0], "avoided": 1,
                    "hazard_beyond": {"surface": "Acid", "at_m": 31}}
        self.g.reach = reach
        out = travel_macro.travel_to(x=50.0, y=0.0, z=0.0, wait=True)
        self.assertIn("kept clear of Acid 31m ahead", out)
        self.assertIn("arrived", out)

    def test_stepping_into_fire_interrupts_and_steps_out(self):
        def burn(op, a):
            if op == "act.move_to" and len(self.g.ops("act.move_to")) == 1:
                self.g.watch["party"][0]["surface"] = "Fire"
        self.g.on_start = burn
        self.g.dur["act.move_to"] = 0.2
        self.g.safe = {"safe": False, "standing_in": "Fire", "found": True, "pos": [-2.0, 0.0, 0.0],
                       "path_m": 2.0, "dir": "W"}
        out = travel_macro.travel_to(marker="m1", wait=True)
        # it steps out (toward the destination), then goes on
        self.assertIn("left Fire W 2m", out)
        self.assertEqual(self.g.ops("nav.safe_spot")[-1][1]["pos"], [50, 0, 0])
        self.assertIn("arrived", out)

    def test_stepping_into_fire_strict_steps_out_and_stops(self):
        def burn(op, a):
            if op == "act.move_to" and len(self.g.ops("act.move_to")) == 1:
                self.g.watch["party"][0]["surface"] = "Fire"
        self.g.on_start = burn
        self.g.dur["act.move_to"] = 0.2
        self.g.safe = {"safe": False, "standing_in": "Fire", "found": True, "pos": [-2.0, 0.0, 0.0],
                       "path_m": 2.0, "dir": "W"}
        out = travel_macro.travel_to(marker="m1", wait=True, avoid_hazards="strict")
        self.assertIn("standing in Fire", out)
        self.assertIn("left Fire W 2m", out)
        self.assertEqual(self.g.ops("act.move_to")[-1][1]["pos"], [-2.0, 0.0, 0.0])

    def test_starting_in_fire_steps_out_first(self):
        self.g.watch["party"][0]["surface"] = "Fire"
        self.g.safe = {"safe": False, "standing_in": "Fire", "found": True, "pos": [3.0, 0.0, 0.0],
                       "path_m": 3.0, "dir": "E"}
        out = travel_macro.travel_to(marker="m1", wait=True)
        self.assertEqual(self.g.ops("act.move_to")[0][1]["pos"], [3.0, 0.0, 0.0])
        self.assertIn("left Fire E 3m", out)
        self.assertIn("arrived", out)

    def test_marker_on_another_level(self):
        (self.tmp / "markers.json").write_text(json.dumps([{"id": "m1", "label": "x", "pos": [1, 0, 0],
                                                             "level": "WLD", "region": "Wilderness"}]))
        self.assertIn("fast_travel there first", travel_macro.travel_to(marker="m1", wait=True))


class Regroup(Base):
    def test_far_members_walk_back(self):
        # Walks back in travel_to legs (S005: one 77 m move_to was refused as too far).
        self.g.watch["party"][1]["pos"] = [20, 0, 0]
        self.g.reach = {"reachable": True, "pos": [0.0, 0.0, 0.0]}

        def arrive(op, a):
            if op == "act.move_to" and a.get("char") == P2:
                self.g.watch["party"][1]["pos"] = list(a["pos"])
        self.g.on_start = arrive
        out = travel_macro.regroup()
        mv = self.g.ops("act.move_to")
        self.assertEqual((mv[0][1]["char"], mv[0][1]["pos"]), (P2, [0.0, 0.0, 0.0]))
        self.assertIn("1 walking", out)
        self.assertIn("(20m → 0m): back", out)

    def test_nobody_far(self):
        self.assertIn("everyone is within", travel_macro.regroup())


class ReadAll(Base):
    def test_reads_new_books_once(self):
        self.g.inv = {"items": [{"uuid": BOOK, "name": "Dusty Book", "owner": P1},
                                {"uuid": LETTER, "name": "Letter to Edowin", "owner": P2},
                                {"uuid": POTION, "name": "Potion of Healing", "owner": P1},
                                {"uuid": "c" * 8 + CHEST[8:], "name": "Scroll of Revivify", "owner": P1}]}
        self.g.ui = {"screen": "book", "popup": {"title": "Dusty Book", "text": "The tadpoles must be removed."}}
        self.g.on_start = lambda op, a: op == "act.interact" and self.g.ui.update(screen="book")
        orig = self.g.rpc

        def rpc(op, args=None, context="server", timeout=12.0):
            if op == "ui.dismiss":
                self.g.ui["screen"] = "game"
            return orig(op, args, context, timeout)
        self.g.rpc = rpc
        out = travel_macro.read_all()
        self.assertEqual([a["target"] for _, a in self.g.ops("act.interact")], [BOOK, LETTER])
        self.assertEqual(self.g.ops("act.interact")[1][1]["char"], P2)  # the owner reads it
        self.assertIn("The tadpoles must be removed.", out)
        self.assertEqual(len(self.g.ops("ui.dismiss")), 2)
        self.assertEqual(set(json.loads((self.tmp / "read_items.json").read_text())), {BOOK, LETTER})
        self.assertIn("nothing new to read", travel_macro.read_all())

    def test_readable_names(self):
        self.assertTrue(macros.readable({"name": "Necromancy of Thay"}) is False)  # needs a book word
        self.assertTrue(macros.readable({"name": "Gnoll's Diary"}))
        self.assertFalse(macros.readable({"name": "Scroll of Fireball"}))


class RestIfNeeded(Base):
    def test_no_rest_needed(self):
        self.assertIn("no rest needed (lowest HP 100%)", travel_macro.rest_if_needed())
        self.assertEqual(self.g.ops("ui.short_rest"), [])

    def test_short_rest_when_low(self):
        self.g.watch["party"][1]["hp"] = 6
        out = travel_macro.rest_if_needed()
        self.assertEqual(len(self.g.ops("ui.short_rest")), 1)
        self.assertIn("low: ", out)
        self.assertIn("6/20", out)

    def test_long_rest_only_when_allowed(self):
        self.g.watch["party"][0]["hp"] = 2
        self.g.refuse_if = lambda op, a: "no short rests left" if op == "ui.short_rest" else None
        out = travel_macro.rest_if_needed()
        self.assertIn("long rest at camp", out)
        self.assertEqual(self.g.ops("ui.long_rest"), [])
        travel_macro.rest_if_needed(allow_long=True)
        self.assertEqual(len(self.g.ops("act.go_to_camp")), 1)
        self.assertEqual(len(self.g.ops("ui.long_rest")), 1)


class ClockStuck(unittest.TestCase):
    def setUp(self) -> None:
        saved = (core.DEADLINE, core.PROGRESS_PATH, dict(core._clock_shown), dict(core._stuck_cache))
        self.addCleanup(self._restore, saved)
        self.path = Path(tempfile.mkdtemp()) / "S005.progress.jsonl"
        self.addCleanup(shutil.rmtree, self.path.parent, True)
        core.PROGRESS_PATH = str(self.path)
        core.DEADLINE = time.time() + 40 * 60
        core._clock_shown.update(at=time.time(), stuck=None)
        p = mock.patch.object(core, "_fight_now", lambda: False)  # this module's fake game may be mid-fight
        p.start()
        self.addCleanup(p.stop)

    def _restore(self, saved) -> None:
        core.DEADLINE, core.PROGRESS_PATH = saved[0], saved[1]
        core._clock_shown.clear()
        core._clock_shown.update(saved[2])
        core._stuck_cache.clear()
        core._stuck_cache.update(saved[3])

    def write(self, *recs: dict) -> None:
        with self.path.open("a") as f:
            for r in recs:
                f.write(json.dumps(r) + "\n")
        os.utime(self.path, (time.time(), time.time() + len(self.path.read_text())))  # new mtime each write

    def test_stuck_signal_shows_at_once_then_clears(self):
        self.assertNotIn("no story progress", core._clock_line())  # shown recently, not stuck: terse at most
        now = time.time()
        self.write({"type": "stuck", "t": now, "since": now - 17 * 60, "idle_min": 17, "actions": 40})
        line = core._clock_line()
        self.assertIn(" · no story progress for 17 min: change approach", line)
        self.assertNotIn("no story progress", core._clock_line())  # announced once, then with the clock
        self.write({"type": "progress", "t": now + 1, "kind": "area", "what": "x", "points": 3})
        self.assertNotIn("no story progress", core._clock())

    def test_wrapup_points_to_the_playbook(self):
        core.DEADLINE = time.time() + 5 * 60
        line = core._clock_line()
        self.assertIn("WRAP UP NOW", line)
        self.assertIn("knowledge/playbooks/session-wrapup.md", line)
        self.assertNotIn("skill", line)


if __name__ == "__main__":
    unittest.main()

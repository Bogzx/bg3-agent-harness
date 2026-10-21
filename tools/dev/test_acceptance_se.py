"""acceptance.py helpers of se's items, offline (live window 20261016-183253 follow-ups).

    uv run --project bridge python -m unittest tools/dev/test_acceptance_se.py
"""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[1] / "bridge" / "src"))
from acceptance import act, core, hotbar, se  # noqa: E402

LUA = HERE.parents[1] / "mod/Mods/BG3AI/ScriptExtender/Lua/Client/UIActions.lua"
HINT = re.search(r'A\.SAVELOAD_HINT = "(.*)"', LUA.read_text()).group(1)


class UI3Refused(unittest.TestCase):
    # the three mod refusals the item sends, built as Client/UIActions.lua refuseSaveLoad builds them
    MSGS = ["ui.command QuickLoadCommand refused (quickload)" + HINT,
            "F8 is quickload: refused" + HINT,
            "ui.command QuickSaveCommand refused (quicksave)" + HINT]

    def test_each_mod_refusal_counts(self):
        for m in self.MSGS:
            self.assertTrue(hotbar.ui3_refused(m), m)
            self.assertTrue(hotbar.ui3_refused("RPC error: " + m), m)   # the bridge's wrapper prefix

    def test_window1_failure_was_the_cut(self):
        # 10-16 18:32: the old verdict read str(e)[:90], which ends at "go through save_game /"
        cut = self.MSGS[0][:90]
        self.assertTrue(cut.endswith("save_game /"), cut)
        self.assertFalse(hotbar.ui3_refused(cut))

    def test_other_errors_do_not_count(self):
        self.assertFalse(hotbar.ui3_refused("widget not open: *"))
        self.assertFalse(hotbar.ui3_refused("no command 'QuickLoadCommand' on gui::DCHotBar"))


class HoverCompare(unittest.TestCase):
    T = ["rotate_occluded_right", "rotate_occluded_left"]

    def test_window1_shape_fails_now(self):
        # 10-16 18:32 e7: new 19 tries 1.60 s with two occluded turns, old 1 try 0.12 s with none: it PASSed then
        ok, why = se.hover_compare(((904, 742), 1, 0.12, None, []), ((904, 742), 19, 1.60, "cursor", self.T))
        self.assertFalse(ok)
        self.assertIn("turned the camera", why)

    def test_same_first_point(self):
        self.assertEqual(se.hover_compare(((1, 1), 1, 0.12, None, []), ((1, 1), 1, 0.13, "cursor", [])), (True, ""))

    def test_more_tries_where_old_hit_early(self):
        ok, why = se.hover_compare(((1, 1), 3, 0.4, None, []), ((1, 1), 5, 0.5, "cursor", []))
        self.assertFalse(ok)
        self.assertIn("more tries", why)

    def test_occluded_target_new_may_turn(self):
        ok, _ = se.hover_compare(((1, 1), 82, 12.6, None, self.T), ((1, 1), 44, 5.4, "cursor", self.T))
        self.assertTrue(ok)

    def test_new_miss_or_slower(self):
        self.assertFalse(se.hover_compare(((1, 1), 1, 0.1, None, []), (None, 50, 8.0, "cursor", self.T))[0])
        self.assertFalse(se.hover_compare(((1, 1), 30, 2.0, None, []), ((1, 1), 30, 2.6, "frames", []))[0])


class UndoCamera(unittest.TestCase):
    def test_net_turn_and_zoom_undone(self):
        from unittest import mock
        from bg3ai import clicker
        inp = mock.Mock()
        inp.window_size.return_value = (1920, 1080)
        with mock.patch.object(clicker, "_turn") as turn, mock.patch.object(clicker, "settle_camera"), \
                mock.patch.object(se, "S", mock.Mock()):
            se.undo_search_camera(inp, "u", ["zoom_out", "rotate_occluded_right", "rotate_occluded_left"])
        turn.assert_called_once()
        self.assertEqual(turn.call_args.args[-1], 1)          # +1 - 2 = -1 net -> turn back +1
        inp.wheel.assert_called_once_with(2, 960, 540)

    def test_nothing_to_undo(self):
        from unittest import mock
        from bg3ai import clicker
        inp = mock.Mock()
        with mock.patch.object(clicker, "_turn") as turn:
            se.undo_search_camera(inp, "u", ["center", "rotate_left"])
        turn.assert_not_called()
        inp.wheel.assert_not_called()


class ServerMapLine(unittest.TestCase):
    def test_every_deck_in_slot_order_no_cut(self):
        m = {"ClassHotBar": {str(i): f"Target_Spell{i}_LongEnoughNameToFillTheLine" for i in range(1, 17)},
             "CommonHotBar": {"10": "Target_MainHandAttack", "9": "Shout_Dash"}}
        line = core.server_map_line({"map": m, "container": "DefaultBarContainer", "bars": 3, "slots": 18})
        self.assertGreater(len(line), 800)   # window #3: the old [:800] cut ClassHotBar 5-8 off
        self.assertIn("16=Target_Spell16_", line)
        self.assertTrue(line.startswith("container=DefaultBarContainer bars=3 slots=18 ClassHotBar[1="), line[:80])
        self.assertIn("CommonHotBar[9=Shout_Dash 10=Target_MainHandAttack]", line)

    def test_no_map(self):
        self.assertEqual(core.server_map_line({"why": "no component"}), '{"why": "no component"}')
        self.assertEqual(core.server_map_line(None), "null")


class HotbarCasterPick(unittest.TestCase):
    """Live window #3 (10-17 14:07, S027-start): 'caster with a usable Bless=None'. The pick now reads each member's
    server hotbar map (no HUD switch), the selected member first, then polls the HUD read after selecting."""

    BLESS_OFF = {"book": [{"id": "Target_Bless", "prep": "RequiresPreparation", "prepared": False, "unprepared": True}],
                 "prepared_read": True}
    STATES = {"claude": BLESS_OFF}
    MAPS = {"gale": {"ClassHotBar": {"3": "Projectile_MagicMissile", "4": "Projectile_ScorchingRay"}},
            "karl": {"CommonHotBar": {"9": "Target_MainHandAttack"}},
            "sh": {"ClassHotBar": {"2": "Target_GuidingBolt"}},
            "claude": {"ClassHotBar": {"6": "Target_Bless", "4": "Target_CureWounds"}}}

    def _run(self, maps, sel, found_after=0):
        from unittest import mock
        from bg3ai import hotbar as HB
        reads, selects = [], []
        fake_s = mock.Mock()
        fake_s.H.get = lambda u: u
        fake_s.game.run_job = lambda op, args, wait_s=0: selects.append(args["char"])

        def slot(game, spell, char, tabs=False, cache=False):
            reads.append(char)
            hit = len(reads) > found_after
            return {"found": hit, "can_use": True, "selected": True, "deck": "ClassHotBar", "index": 7}
        with mock.patch.object(hotbar, "S", fake_s), mock.patch.object(hotbar, "log"), \
                mock.patch.object(hotbar, "spell_state", lambda u, needle: self.STATES.get(u, {"book": []})), \
                mock.patch.object(HB, "server_map", lambda game, u: maps.get(u)), \
                mock.patch.object(HB, "_slot", slot), mock.patch.object(HB.time, "sleep"):
            pick, why = hotbar.pick_hotbar_caster(["gale", "karl", "sh", "claude"], sel)
        return pick, why, reads, selects

    def test_window3_party_picks_claude_from_the_server_map(self):
        pick, why, reads, selects = self._run(self.MAPS, sel="gale")
        self.assertEqual(pick, ("claude", "Target_Bless", "BLESS", 3))
        self.assertEqual(selects, ["claude"])          # nobody else selected (no HUD read per member)
        self.assertEqual(reads, ["claude"])

    def test_selected_member_first_and_not_reselected(self):
        maps = dict(self.MAPS, sh={"ClassHotBar": {"1": "Target_Bless"}})
        pick, _, _, selects = self._run(maps, sel="claude")
        self.assertEqual(pick[0], "claude")
        self.assertEqual(selects, [])

    def test_hud_lag_after_select_is_polled(self):
        pick, _, reads, _ = self._run(self.MAPS, sel="gale", found_after=3)
        self.assertEqual(pick[0], "claude")
        self.assertEqual(len(reads), 4)                 # 3 stale reads, then the cell

    def test_nobody_has_it_says_what_each_has(self):
        maps = {k: v for k, v in self.MAPS.items() if k != "claude"}
        pick, why, _, selects = self._run(maps, sel="gale")
        self.assertIsNone(pick)
        self.assertEqual(selects, [])
        self.assertIn("gale: 2 ids, multi-target ['Projectile_MagicMissile', 'Projectile_ScorchingRay']", why)
        self.assertIn("claude: no map", why)

    def test_s065_end_names_the_unprepared_bless(self):
        # live 20261017-152016 (S065-end): no Bless on any map; Claude knows it but never prepared it (A4: the
        # Osiris route's prepared check never ran, so S065's Bless was cast unprepared)
        maps = dict(self.MAPS, claude={"ClassHotBar": {"4": "Target_CureWounds"}})
        pick, why, _, _ = self._run(maps, sel="gale")
        self.assertIsNone(pick)
        self.assertIn("claude: 1 ids, multi-target none; Target_Bless in the spellbook (RequiresPreparation), "
                      "not prepared", why)
        self.assertIn("karl: 1 ids, multi-target none; Target_Bless not in the spellbook", why)

    def test_map_has_upcast_base(self):
        from bg3ai import hotbar as HB
        self.assertTrue(HB.map_has({"ClassHotBar": {"6": "Target_Bless"}}, "target_bless"))
        self.assertTrue(HB.map_has({"ClassHotBar": {"6": "Projectile_MagicMissile"}}, "Projectile_MagicMissile_3"))
        self.assertFalse(HB.map_has({"ClassHotBar": {"6": "Target_Bane"}}, "Target_Bless"))
        self.assertFalse(HB.map_has(None, "Target_Bless"))


class PrepReason(unittest.TestCase):
    def _b(self, **kw):
        return {"book": [dict({"id": "Target_Bless", "prep": "RequiresPreparation"}, **kw)], "prepared_read": True}

    def test_each_case_names_its_cause(self):
        R = hotbar.prep_reason
        self.assertIn("not prepared: not castable", R(self._b(prepared=False, unprepared=True), "Target_Bless", False))
        self.assertIn("Shared/HotbarMap.lua misses a bar",
                      R(self._b(prepared=True, unprepared=False), "Target_Bless", False))
        self.assertEqual(R(self._b(prepared=True, unprepared=False), "Target_Bless", True),
                         "prepared (RequiresPreparation) and on the hotbar map")
        self.assertEqual(R({"book": []}, "Target_Bless", False), "not in the spellbook")
        self.assertIn("did not read", R(dict(self._b(), prepared_read=False), "Target_Bless", False))
        self.assertIn("pre-A4", R({"why": "the mod has no Guard.prepared_ids (pre-A4)"}, "Target_Bless", False))

    def test_prepared_pick(self):
        states = {"karl": {"book": [{"id": "Target_Rage", "prep": "AlwaysPrepared"}]},
                  "sh": {"book": [{"id": "Target_Bane", "prep": "RequiresPreparation", "unprepared": True},
                                  {"id": "Target_CureWounds", "prep": "RequiresPreparation", "prepared": True}]}}
        self.assertEqual(act.prepared_pick(states), ("sh", "Target_Bane", "Target_CureWounds"))
        self.assertIsNone(act.prepared_pick({"karl": states["karl"], "x": {"why": "no entity"}}))


class TwostepContainer(unittest.TestCase):
    """Live L4 (2026-10-17): the DD item read e5 Shadowheart's cell while Claude was selected, got Claude's Blinding
    Smite at ClassHotBar #26 (no container flag) and FAILed at the route's "container" stop. A7: each candidate is
    read from its own HUD (selected first), and a container cell (the upcast level picker) goes on to the cast."""

    def _run(self, slots, after_select, maps, lands=False):
        from unittest import mock

        from bg3ai import hotbar as HB
        fake_s = mock.Mock()
        fake_s.H.get = lambda u: {"u3": "e3", "u4": "e4", "u5": "e5"}[u]
        fake_s.game.try_rpc = lambda *a, **k: {}
        selects = []
        fake_s.game.run_job = lambda op, args, **k: selects.append(args["char"])
        got, probes, logs = [], [], []
        party = [{"uuid": u, "pos": [0, 0, 0]} for u in slots]
        with mock.patch.object(hotbar, "S", fake_s), \
                mock.patch.object(hotbar, "RESULTS", [{"item": "hotbar widget tree (ACT-8, read-only)", "verdict": "PASS"}]), \
                mock.patch.object(hotbar, "status", lambda: {"party": party}), \
                mock.patch.object(hotbar, "hb_slot", lambda spell, u: slots[u]), \
                mock.patch.object(HB, "server_map", lambda g, u: maps.get(u)), \
                mock.patch.object(HB, "_slot_after_select", lambda g, sp, u: after_select[u]), \
                mock.patch.object(hotbar, "container_probe", lambda u, r: probes.append(u) or "server {} | hud slot{}"), \
                mock.patch.object(hotbar, "lua_server", lambda code: ""), \
                mock.patch.object(hotbar, "took", side_effect=AssertionError("cast attempted")), \
                mock.patch.object(hotbar, "log", logs.append), \
                mock.patch.object(hotbar, "item", lambda *a: got.append(a)):
            self.assertFalse(hotbar.twostep_dd_probe())
        return got, probes, logs, selects

    def test_reads_the_members_own_hud_and_takes_the_container_cell(self):
        dd = {"ClassHotBar": {"25": "Teleportation_DimensionDoor"}}
        slots = {"u4": {"found": False, "selected": True}, "u5": {"found": True, "can_use": True, "selected": False,
                                                                  "deck": "ClassHotBar", "index": 26}}
        after = {"u5": {"found": True, "can_use": True, "container": True, "selected": True, "deck": "ClassHotBar",
                        "index": 26}}
        got, probes, logs, selects = self._run(slots, after, {"u5": dd})
        self.assertEqual(selects, ["u5"], "selected before reading its cell")
        self.assertEqual(probes, ["u5"], "the container cell is probed (read-only) and taken")
        self.assertTrue(any(line.startswith("PROBE twostep container e5:") for line in logs))
        # no floor point (lua_server stubbed): the item stops there, not at the old container SKIP
        self.assertFalse(any("container slot" in str(g) for g in got), got)

    def test_nobody_has_it_skips(self):
        got, probes, _, selects = self._run({"u3": {"found": False}}, {}, {})
        self.assertEqual((got[0][0], got[0][1]), (hotbar.TWOSTEP_ITEM, "SKIP"))
        self.assertIn("nobody in the party has", got[0][2])
        self.assertEqual((probes, selects), ([], []))

    def test_greyed_out_skips(self):
        dd = {"ClassHotBar": {"25": "Teleportation_DimensionDoor"}}
        got, _, _, _ = self._run({"u3": {"found": True, "can_use": False, "selected": True}}, {}, {"u3": dd})
        self.assertEqual(got[0][1], "SKIP")
        self.assertIn("e3 has Teleportation_DimensionDoor greyed out", got[0][2])


if __name__ == "__main__":
    unittest.main()

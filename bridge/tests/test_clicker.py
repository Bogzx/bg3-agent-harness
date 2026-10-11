"""Offline tests for clicker.py: the camera moves that bring a click target into view (live S005: lockpick
'the target is not on screen' x3 on a door 1.5 m away), the door-leaf aims and the off_screen/hover_miss split."""

import unittest
from unittest import mock

from bg3ai import clicker


class FakeInput:
    def __init__(self):
        self.log: list[tuple] = []

    def window_size(self):
        return (1920, 1080)

    def wheel(self, n, x=None, y=None, sync=True):
        self.log.append(("wheel", n))

    def drag(self, x1, y1, x2, y2, button="L", steps=12):
        self.log.append(("drag", button, x2 - x1))

    def move(self, x, y, sync=True):
        self.log.append(("move", x, y))

    def click(self, x, y, button="L", count=1, sync=True):
        self.log.append(("click", x, y, button))
        self.buttons = getattr(self, "buttons", []) + [button]

    def wait_frames(self, n, sync=True):
        pass
    def key(self, k, sync=True):
        self.log.append(("key", k))


class FakeGame:
    """A target whose projection follows the camera moves the FakeInput made."""

    def __init__(self, inp, ndc=(0.2, 1.15), behind=False, side="right", hover_aims=None, zoom_gain=0.2,
                 rotate_gain=0.6, screen_after=None):
        self.inp, self.ndc, self.behind, self.side = inp, list(ndc), behind, side
        self.hover_aims = hover_aims          # set of (aim, ox) that hover the target; None = any in view
        self.zoom_gain, self.rotate_gain = zoom_gain, rotate_gain
        self.seen = 0
        self.last_aim = None
        self.screen_after = screen_after
        self.clicked = False
        self.hover_other = None               # what the picking helper reports instead of the target
        self.rc = None                        # where the right-click opened the context menu
        self.cursor = (0, 0)

    want_row = "Lockpick"

    def menu_row(self):
        return self.menu.index(self.want_row) + 1 if self.want_row in self.menu else None

    def _row(self):
        """The menu row under the cursor: rows 27 px tall from 30 px below the right-click, 200 px wide."""
        dx, dy = self.cursor[0] - self.rc[0], self.cursor[1] - self.rc[1]
        if not (10 <= dx <= 210 and dy >= 30):
            return None
        i = int((dy - 30) // 27) + 1
        return i if i <= len(self.menu) else None

    def _apply_moves(self):
        for ev in self.inp.log[self.seen:]:
            if ev[0] == "wheel" and ev[1] < 0:
                self.ndc = [v * (1 - self.zoom_gain) for v in self.ndc]
            elif ev[0] == "drag":
                # dragging left (dx<0) turns the view right: a point on the right moves toward the centre
                if self.behind:
                    self.behind = False
                    self.ndc = [1.3 if self.side == "right" else -1.3, 0.1]
                else:
                    self.ndc[0] += self.rotate_gain * (1 if ev[2] > 0 else -1)
            elif ev[0] == "click":
                if getattr(self, "menu", None) is None:
                    self.clicked = True
                elif ev[3] == "R":
                    self.rc = (ev[1], ev[2])   # the context menu opens at the cursor
                elif self.rc and self._row() == self.menu_row():
                    self.clicked = self.menu_done = True
            elif ev[0] == "move":
                self.cursor = (ev[1], ev[2])
            elif ev[0] == "key" and ev[1] == clicker.CENTER_KEY and getattr(self, "home_ndc", None):
                self.ndc, self.behind = list(self.home_ndc), False   # the camera jumps back to the actor
        self.seen = len(self.inp.log)

    def rpc(self, op, args=None, context="server"):
        self._apply_moves()
        args = args or {}
        if op == "ui.screen_pos":
            if self.behind:
                return {"on_screen": False, "behind": True, "side": self.side}
            nx, ny = self.ndc
            if args.get("aim") == "chest":
                ny -= 0.2
            self.last_aim = (args.get("aim"), args.get("ox", 0.0))
            return {"ndc": [nx, ny], "on_screen": -1 <= nx <= 1 and -1 <= ny <= 1, "side": self.side}
        if op == "ui.hover_check":
            ok = self.hover_aims is None or self.last_aim in self.hover_aims
            return {"hovered": ok} if ok or not self.hover_other else {"hovered": False, "hover": self.hover_other}
        if op == "ui.state":
            return {"screen": self.screen_after if self.clicked else "game"}
        if op == "act.target_state":
            return dict(getattr(self, "target_state", {}))
        if op == "ui.context_menu":
            menu = getattr(self, "menu", None)
            is_open = menu is not None and self.rc is not None and not getattr(self, "menu_done", False)
            if not args.get("item"):
                return {"open": is_open, "items": menu}
            self.pressed = args
            if not is_open:
                raise clicker.GameError("the context menu is not open")
            if args["item"] not in menu:
                raise clicker.GameError(f"no '{args['item']}' entry in the context menu ({', '.join(menu)})")
            i = self.menu_row()
            return {"open": True, "items": menu, "match": i, "over": self._row() == i}
        raise AssertionError(op)


class CameraMove(unittest.TestCase):
    def test_moves(self):
        self.assertIsNone(clicker.camera_move({"on_screen": True, "ndc": [0.1, 0.3]}))
        self.assertEqual(clicker.camera_move({"on_screen": False, "ndc": [0.2, 1.1]}), "zoom_out")
        self.assertEqual(clicker.camera_move({"on_screen": True, "ndc": [0.2, 0.95]}), "zoom_out")
        self.assertEqual(clicker.camera_move({"on_screen": False, "behind": True, "side": "left"}), "rotate_left")
        self.assertEqual(clicker.camera_move({"on_screen": False, "ndc": [2.5, 0.1], "side": "right"}), "rotate_right")


class BringIntoView(unittest.TestCase):
    def test_target_below_the_bottom_edge_zooms_out(self):
        inp = FakeInput()
        g = FakeGame(inp, ndc=(0.2, 1.15))
        moves = clicker.bring_into_view(g, inp, "u")
        self.assertEqual(set(moves), {"zoom_out"})
        self.assertTrue(clicker._centered(clicker._screen_pos(g, "u")))

    def test_target_behind_rotates_then_centres(self):
        inp = FakeInput()
        g = FakeGame(inp, behind=True, side="right")
        moves = clicker.bring_into_view(g, inp, "u")
        self.assertEqual(moves[0], "rotate_right")
        self.assertTrue(clicker._centered(clicker._screen_pos(g, "u")))

    def test_wrong_drag_direction_flips(self):
        inp = FakeInput()
        g = FakeGame(inp, ndc=(1.6, 0.1), side="right", rotate_gain=-0.5)  # this camera turns the other way
        clicker.bring_into_view(g, inp, "u")
        drags = [e[2] for e in inp.log if e[0] == "drag"]
        self.assertGreaterEqual(len({d > 0 for d in drags}), 2, drags)
        self.assertTrue(clicker._centered(clicker._screen_pos(g, "u")))

    def test_zoom_without_progress_stops(self):
        inp = FakeInput()
        g = FakeGame(inp, ndc=(0.2, 1.15), zoom_gain=0.0)
        moves = clicker.bring_into_view(g, inp, "u")
        self.assertLessEqual(len(moves), 2)

    def test_in_view_no_moves(self):
        inp = FakeInput()
        self.assertEqual(clicker.bring_into_view(FakeGame(inp, ndc=(0.0, 0.2)), inp, "u"), [])
        self.assertEqual(inp.log, [])


class ClickEntity(unittest.TestCase):
    def test_door_hit_on_its_leaf_after_zoom(self):
        inp = FakeInput()
        g = FakeGame(inp, ndc=(0.2, 1.15), hover_aims={("mid", -0.5)}, screen_after="roll")
        r = clicker.click_entity(g, inp, "door", expect="roll", verify_timeout=0.2)
        self.assertTrue(r.ok, r)
        self.assertEqual(r.screen, "roll")
        self.assertIn("zoom_out", r.detail.get("camera", []))

    def test_right_click_menu_lockpick(self):
        """S024 07:23: the hand route right-click -> Lockpick got the roll where left-clicks did nothing."""
        inp = FakeInput()
        g = FakeGame(inp, ndc=(0.0, 0.2), screen_after="roll")
        g.menu = ["Open (locked) (Claude)", "Lockpick", "Attack: (Main Hand Attack)", "Examine"]
        r = clicker.click_entity(g, inp, "door", button="R", expect="roll", verify_timeout=0.2, menu="Lockpick")
        self.assertTrue(r.ok, r)
        self.assertEqual(inp.buttons, ["R", "L"])
        self.assertEqual(g._row(), 2)  # the left click landed on the Lockpick row
        self.assertEqual(g.pressed, {"item": "Lockpick"})
        self.assertEqual(r.detail.get("menu"), "Lockpick")

    def test_disarm_is_done_only_when_the_trap_is(self):
        """S031 17:10: a trapped bookcase (not locked) read 'unlocked' = done 3x while its trap stayed armed."""
        inp = FakeInput()
        g = FakeGame(inp, ndc=(0.0, 0.2), screen_after="game")
        g.menu, g.want_row = ["Open", "Disarm", "Attack: (Ranged Attack)", "Examine"], "Disarm"
        g.target_state = {"visible": True, "locked": False, "opened": False, "trap_armed": True}
        r = clicker.click_entity(g, inp, "shelf", button="R", expect="roll", verify_timeout=0.2, menu="Disarm")
        self.assertFalse(r.ok, r)
        self.assertEqual(r.reason, "no_effect")
        self.assertEqual(r.detail.get("trap"), "still armed")
        self.assertEqual(g.pressed, {"item": "Disarm"})
        inp2 = FakeInput()
        g2 = FakeGame(inp2, ndc=(0.0, 0.2), screen_after="game")
        g2.menu, g2.want_row = ["Open", "Disarm", "Examine"], "Disarm"
        g2.target_state = {"visible": True, "locked": False, "trap_armed": False}
        r = clicker.click_entity(g2, inp2, "shelf", button="R", expect="roll", verify_timeout=0.2, menu="Disarm")
        self.assertTrue(r.ok, r)
        self.assertEqual(r.screen, "disarmed")
        # a lockpick still counts an opened lock
        inp3 = FakeInput()
        g3 = FakeGame(inp3, ndc=(0.0, 0.2), screen_after="game")
        g3.menu = ["Open (locked) (Claude)", "Lockpick", "Examine"]
        g3.target_state = {"visible": True, "locked": False}
        r = clicker.click_entity(g3, inp3, "door", button="R", expect="roll", verify_timeout=0.2, menu="Lockpick")
        self.assertTrue(r.ok, r)
        self.assertEqual(r.screen, "unlocked")

    def test_right_click_menu_without_the_entry(self):
        inp = FakeInput()
        g = FakeGame(inp, ndc=(0.0, 0.2), screen_after="roll")
        g.menu = ["Examine"]
        old, clicker.MENU_WAIT_S = clicker.MENU_WAIT_S, 0.2
        try:
            r = clicker.click_entity(g, inp, "door", expect="roll", verify_timeout=0.2, menu="Lockpick")
        finally:
            clicker.MENU_WAIT_S = old
        self.assertFalse(r.ok)
        self.assertEqual(r.reason, "no_menu_entry")
        self.assertIn("no 'Lockpick' entry", r.detail.get("menu"))
        self.assertIn(("key", "ESCAPE"), inp.log)  # the menu is closed again

    def test_camera_on_a_companion_centers_on_the_actor_first(self):
        """lockpick live 12:3x: the camera followed the companions, the lock was off screen (tries=0)."""
        inp = FakeInput()
        g = FakeGame(inp, ndc=(-2.6, 25.0), screen_after="roll")
        g.home_ndc = (0.05, 0.1)
        r = clicker.click_entity(g, inp, "door", expect="roll", verify_timeout=0.2)
        self.assertTrue(r.ok, r)
        self.assertEqual(r.detail.get("camera"), ["center"])   # no zoom/rotate needed after the center key
        self.assertEqual([e for e in inp.log if e[0] == "key"], [("key", "Home")])

    def test_a_covering_screen_is_named_without_camera_moves(self):
        """S031 17:13: click_on under an open book page: 6 rotations, then off_screen."""
        inp = FakeInput()
        g = FakeGame(inp, ndc=(3.0, 0.1))
        g.rpc_orig = g.rpc
        g.rpc = lambda op, args=None, context="server": {"screen": "book"} if op == "ui.state" else g.rpc_orig(op, args, context)
        r = clicker.click_entity(g, inp, "pedestal")
        self.assertFalse(r.ok)
        self.assertEqual((r.reason, r.screen), ("covered", "book"))
        self.assertEqual([e for e in inp.log if e[0] in ("drag", "wheel", "key", "click")], [])

    def test_target_already_in_view_no_center_key(self):
        inp = FakeInput()
        g = FakeGame(inp, ndc=(0.0, 0.2), screen_after="roll")
        g.home_ndc = (0.5, 0.5)
        r = clicker.click_entity(g, inp, "door", expect="roll", verify_timeout=0.2)
        self.assertTrue(r.ok, r)
        self.assertNotIn(("key", "Home"), inp.log)
        self.assertIsNone(r.detail.get("camera"))

    def test_in_view_but_never_hovered_is_hover_miss(self):
        inp = FakeInput()
        g = FakeGame(inp, ndc=(0.0, 0.2), hover_aims=set())
        r = clicker.click_entity(g, inp, "door", expect="roll", verify_timeout=0.1)
        self.assertFalse(r.ok)
        self.assertEqual(r.reason, "hover_miss")
        self.assertEqual(r.detail.get("camera"), ["zoom_out"])  # one wider look before giving up

    def test_hover_miss_names_what_was_under_the_cursor(self):
        inp = FakeInput()
        g = FakeGame(inp, ndc=(0.0, 0.2), hover_aims=set())
        g.hover_other = "pillar"
        r = clicker.click_entity(g, inp, "door", expect="roll", verify_timeout=0.1)
        self.assertEqual(r.reason, "hover_miss")
        self.assertEqual(r.detail.get("hovered_instead"), ["pillar"])

    def test_wide_door_leaf_is_aimed_at(self):
        # S032 17:42 Vault Door: the leaf is ~1.4 m from the hinge (the origin)
        inp = FakeInput()
        g = FakeGame(inp, ndc=(0.0, 0.2), hover_aims={("mid", -1.4)})
        g.screen_after = "roll"
        r = clicker.click_entity(g, inp, "door", expect="roll", verify_timeout=0.1)
        self.assertTrue(r.ok)

    def test_duplicate_resolved_aim_is_skipped(self):
        inp = FakeInput()
        g = FakeGame(inp, ndc=(0.0, 0.2), hover_aims=set())
        real = g.rpc

        def item_aims(op, args=None, context="server"):
            out = real(op, args, context)
            if op == "ui.screen_pos" and isinstance(out, dict):
                out["aim"] = "low" if (args or {}).get("aim") in ("auto", "low") else (args or {}).get("aim")
            return out
        g.rpc = item_aims
        r = clicker.click_entity(g, inp, "door", expect="roll", verify_timeout=0.1)
        per_pass = (len(clicker.AIMS) - 1) * len(clicker.OFFSETS)
        self.assertEqual(r.tries, 2 * per_pass)

    def test_hover_on_an_object_with_no_id_is_named(self):
        inp = FakeInput()
        g = FakeGame(inp, ndc=(0.0, 0.2), hover_aims=set())
        real = g.rpc
        g.rpc = lambda op, args=None, context="server": (
            {"hovered": False, "hover_nouuid": True} if op == "ui.hover_check" else real(op, args, context))
        r = clicker.click_entity(g, inp, "door", expect="roll", verify_timeout=0.1)
        self.assertEqual(r.detail.get("hovered_instead"), ["(an object with no id)"])

    def test_no_id_miss_carries_the_diagnostics(self):
        # S050/S051: 8 lockpicks on doors "hovered_instead=[(an object with no id)]": say why it wasn't the target
        inp = FakeInput()
        g = FakeGame(inp, ndc=(0.0, 0.2), hover_aims=set())
        real = g.rpc
        g.rpc = lambda op, args=None, context="server": (
            {"hovered": False, "hover_nouuid": True, "nouuid": [{"m": 2.4, "t": "table"}]}
            if op == "ui.hover_check" else real(op, args, context))
        r = clicker.click_entity(g, inp, "door", expect="roll", verify_timeout=0.1)
        self.assertEqual(r.detail.get("no_id_objects"), [{"m": 2.4, "t": "table"}])

    def test_no_id_diagnostics_survive_a_last_empty_aim(self):
        # S063 10:57:47: 51 aims at a door, hovered_instead=[(an object with no id)] but no no_id_objects: the
        # last aim hovered nothing. The no-id details of any earlier aim are kept.
        inp = FakeInput()
        g = FakeGame(inp, ndc=(0.0, 0.2), hover_aims=set())
        real = g.rpc
        n = {"k": 0}

        def rpc(op, args=None, context="server"):
            if op != "ui.hover_check":
                return real(op, args, context)
            n["k"] += 1
            if n["k"] == 3:
                return {"hovered": False, "hover_nouuid": True, "nouuid": [{"m": 1.1, "t": "userdata"}]}
            if n["k"] == 4:
                return {"hovered": False, "hover_nouuid": True,
                        "nouuid_why": "the target has no client entity to compare with"}
            return {"hovered": False}
        g.rpc = rpc
        r = clicker.click_entity(g, inp, "door", expect="roll", verify_timeout=0.1)
        self.assertEqual(r.detail.get("hovered_instead"), ["(an object with no id)"])
        self.assertEqual(r.detail.get("no_id_objects"), [{"m": 1.1, "t": "userdata"}])
        self.assertEqual(r.detail.get("no_id_why"), "the target has no client entity to compare with")

    def test_no_id_match_is_named(self):
        inp = FakeInput()
        g = FakeGame(inp, ndc=(0.0, 0.2), screen_after="roll")
        real = g.rpc
        g.rpc = lambda op, args=None, context="server": (
            {"hovered": True, "hover_nouuid": True, "via": "origin"} if op == "ui.hover_check" else real(op, args, context))
        r = clicker.click_entity(g, inp, "door", expect="roll", verify_timeout=0.5)
        self.assertTrue(r.ok)
        self.assertEqual(r.detail.get("hover_via"), "origin")

    def _walking(self, steps_moving, roll_after):
        inp = FakeInput()
        g = FakeGame(inp, ndc=(0.0, 0.2))
        g.menu = ["Examine", "Lockpick"]
        real, n = g.rpc, [0]

        def rpc(op, args=None, context="server"):
            if op == "nav.activity":
                n[0] += 1
                x = 0.5 * min(n[0], steps_moving)
                return {"pos": {"claude": [90.0 + x, 40.5, 16.0]}}
            if op == "ui.state" and getattr(g, "menu_done", False):
                return {"screen": "roll" if n[0] >= roll_after else "game"}
            return real(op, args, context)
        g.rpc = rpc
        return clicker.click_entity(g, inp, "chest", expect="roll", verify_timeout=0.1, menu="Lockpick"), n[0]

    def test_roll_waits_while_the_game_walks_the_actor(self):
        # S047 02:57:54: Lockpick row pressed, the game walked the actor ~1 m first; the 4 s ran out ("nothing happened")
        r, polls = self._walking(steps_moving=4, roll_after=4)
        self.assertTrue(r.ok, r)
        self.assertEqual(r.screen, "roll")
        self.assertGreaterEqual(r.detail.get("walked_m", 0), 1.0)

    def test_roll_wait_ends_when_nobody_moves(self):
        r, polls = self._walking(steps_moving=0, roll_after=99)
        self.assertEqual(r.reason, "no_effect")
        self.assertLessEqual(polls, 2)   # one baseline + one still check: no extra wait

    def test_occluded_target_turns_the_camera(self):
        # S053 05:45:31: 31 aims on a Metal Crate all landed on another object 3.2 m away; a player turns the camera
        inp = FakeInput()
        g = FakeGame(inp, ndc=(0.0, 0.2))
        real = g.rpc
        clock = [0.0]

        def rpc(op, args=None, context="server"):
            if op == "ui.hover_check":
                clock[0] += 0.3
                turned = sum(1 for e in inp.log if e[0] == "drag" and e[1] == "M")
                if turned >= 2:   # visible from the other side only
                    return {"hovered": True}
                return {"hovered": False, "hover_nouuid": True, "nouuid": [{"m": 3.2}]}
            return real(op, args, context)
        g.rpc = rpc
        mono = clicker.time.monotonic
        clicker.time.monotonic = lambda: clock[0]
        try:
            r = clicker.click_entity(g, inp, "crate", verify_timeout=0.0)
        finally:
            clicker.time.monotonic = mono
        self.assertTrue(r.ok, r)
        self.assertEqual(r.detail.get("camera")[-2:], ["rotate_occluded_right", "rotate_occluded_left"])

    def test_hover_search_gives_up_at_the_time_budget(self):
        # live S017-S024: 144 tries = 15-33 s per failed lockpick; now capped at HOVER_BUDGET_S
        inp = FakeInput()
        g = FakeGame(inp, ndc=(0.0, 0.2), hover_aims=set())
        clock = [0.0]

        def slow_check(op, args=None, context="server", _rpc=g.rpc):
            if op == "ui.hover_check":
                clock[0] += 0.5   # each hover check costs 0.5 s
            return _rpc(op, args, context)

        g.rpc = slow_check
        real = clicker.time.monotonic
        clicker.time.monotonic = lambda: clock[0]
        try:
            r = clicker.click_entity(g, inp, "door", expect="roll", verify_timeout=0.0)
        finally:
            clicker.time.monotonic = real
        self.assertEqual(r.reason, "hover_miss")
        self.assertLessEqual(r.tries, int(clicker.HOVER_BUDGET_S / 0.5) + 1)
        self.assertEqual(r.detail.get("gave_up_after_s"), clicker.HOVER_BUDGET_S)
        self.assertNotIn("click", [e[0] for e in inp.log])


if __name__ == "__main__":
    unittest.main()


class ToolCheck(unittest.TestCase):
    """Live S011/S013: lockpick said "nothing happened" ~16 times while nobody carried Thieves' Tools."""

    class Inv:
        def __init__(self, items):
            self.items, self.jobs = items, []

        def rpc(self, op, args=None, context="server"):
            if op == "state.inventory":
                assert args.get("expand"), "tools can sit in a pouch"
                return {"items": self.items}
            if op == "state.status":
                return {"party": [{"uuid": "U1", "name": "Claude"}, {"uuid": "U2", "name": "Shadowheart"}]}
            raise AssertionError(op)

        def run_job(self, op, args, wait_s=40.0):
            self.jobs.append(op)
            return clicker.JobResult("done", {})

    def test_nobody_has_tools(self):
        g = self.Inv([{"name": "Warhammer", "owner": "U1"}])
        r = clicker.run_click_action(g, None, "act.lockpick", {"char": "U1", "target": "D"})
        self.assertEqual(r.status, "failed")
        self.assertIn("nobody in the party carries Thieves' Tools", r.error)
        self.assertIn("force it", r.error)
        self.assertEqual(g.jobs, [])  # never walked up or clicked

    def test_other_member_has_tools(self):
        g = self.Inv([{"name": "Thieves' Tools", "owner": "U2", "in": "Pouch"}])
        self.assertIn("Claude carries no Thieves' Tools; Shadowheart does: run it again with who=Shadowheart",
                      clicker.tool_check(g, "act.lockpick", "U1"))
        r = clicker.run_click_action(g, None, "act.lockpick", {"char": "U1", "target": "D"})
        self.assertEqual(g.jobs, ["act.lockpick"])  # the holder picks it instead
        self.assertEqual(r.status, "done")
        self.assertEqual(r.result.get("tool_holder"), "U2")

    def test_disarm_still_armed_says_so(self):
        g = self.Inv([{"name": "Trap Disarm Toolkit", "owner": "U1"}])
        g.run_job = lambda op, args, wait_s=40.0: clicker.JobResult(
            "done", {"needs_click": "T", "expect": "roll", "button": "R", "menu": "Disarm"})
        with mock.patch.object(clicker, "finish_needs_click", lambda game, inp, res: {
                "expect": "roll", "click": {"ok": False, "reason": "no_effect", "trap": "still armed"}}):
            r = clicker.run_click_action(g, object(), "act.disarm", {"char": "U1", "target": "T"})
        self.assertEqual(r.status, "failed")
        self.assertIn("the trap is still armed after Disarm", r.error)

    def test_actor_has_tools_runs_the_job(self):
        g = self.Inv([{"name": "Thieves' Tools", "owner": "U1", "count": 2}])
        r = clicker.run_click_action(g, None, "act.lockpick", {"char": "U1", "target": "D"})
        self.assertEqual(g.jobs, ["act.lockpick"])
        self.assertEqual(r.status, "done")

    def test_disarm_needs_kit_and_loot_needs_nothing(self):
        g = self.Inv([])
        self.assertIn("Trap Disarm Toolkit", clicker.tool_check(g, "act.disarm", "U1"))
        self.assertIsNone(clicker.tool_check(g, "act.loot_corpse", "U1"))

    def test_unreadable_inventory_does_not_block(self):
        class Broken(self.Inv):
            def rpc(self, op, args=None, context="server"):
                raise RuntimeError("no save loaded")
        self.assertIsNone(clicker.tool_check(Broken([]), "act.lockpick", "U1"))


class AnswerList(unittest.TestCase):
    """Live 13:4x Karlach: 10 answers, 6 rows of 84 px visible, 1 wheel notch = 48 px, scrollable 350."""

    class G:
        def __init__(self, inp):
            self.inp, self.off, self.seen = inp, 0.0, 0
            self.texts = [f"answer {k}" for k in range(1, 11)]

        def _apply(self):
            for ev in self.inp.log[self.seen:]:
                if ev[0] == "wheel":
                    self.off = min(350.0, max(0.0, self.off - 48.0 * ev[1]))
                elif ev[0] == "move":
                    self.cursor = (ev[1], ev[2])
            self.seen = len(self.inp.log)

        def first(self):
            return int(self.off // 84) + 1

        def options(self):
            self._apply()
            f = self.first()
            return [{"i": k, "text": self.texts[k - 1]} for k in range(f, min(f + 6, 11))]

        def rpc(self, op, args=None, context="server"):
            self._apply()
            assert op == "ui.answer_list", op
            c = getattr(self, "cursor", (0, 0))
            return {"open": True, "over": 700 <= c[1] <= 1000 and 700 <= c[0] <= 1200, "first": self.first(),
                    "total": 10, "offset": self.off, "scrollable": 350.0}

    def test_reads_every_answer_and_scrolls_back(self):
        clicker._list_hit["at"] = None
        inp = FakeInput()
        g = self.G(inp)
        got, why = clicker.read_answer_list(g, inp, g.options)
        self.assertIsNone(why)
        self.assertEqual(sorted(got), list(range(1, 11)))
        self.assertEqual(got[10]["text"], "answer 10")
        g._apply()
        self.assertEqual(g.off, 0.0)  # back at the top
        self.assertIsNotNone(clicker._list_hit["at"])  # remembered for the next list

    def test_list_not_found(self):
        clicker._list_hit["at"] = None
        inp = FakeInput()
        g = self.G(inp)
        g.rpc = lambda op, args=None, context="server": {"open": True, "over": False, "total": 10}
        got, why = clicker.read_answer_list(g, inp, g.options)
        self.assertEqual(why, "could not point at the answer list")
        self.assertEqual(sorted(got), list(range(1, 7)))

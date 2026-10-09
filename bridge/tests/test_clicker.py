"""Offline tests for clicker.py: the camera moves that bring a click target into view (live S005: lockpick
'the target is not on screen' x3 on a door 1.5 m away), the door-leaf aims and the off_screen/hover_miss split."""

import unittest

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
        self.log.append(("click", x, y))

    def wait_frames(self, n, sync=True):
        pass


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
                self.clicked = True
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
            return {"hovered": ok}
        if op == "ui.state":
            return {"screen": self.screen_after if self.clicked else "game"}
        if op == "act.target_state":
            return {}
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

    def test_in_view_but_never_hovered_is_hover_miss(self):
        inp = FakeInput()
        g = FakeGame(inp, ndc=(0.0, 0.2), hover_aims=set())
        r = clicker.click_entity(g, inp, "door", expect="roll", verify_timeout=0.1)
        self.assertFalse(r.ok)
        self.assertEqual(r.reason, "hover_miss")
        self.assertEqual(r.detail.get("camera"), ["zoom_out"])  # one wider look before giving up


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
        r = clicker.run_click_action(g, None, "act.lockpick", {"char": "U1", "target": "D"})
        self.assertEqual(r.status, "failed")
        self.assertIn("Claude carries no Thieves' Tools; Shadowheart does: run it again with who=Shadowheart", r.error)

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

"""Replay of naut-r2 S001 16:54-16:55 click_on on a simulated clock: the old search (HOVER_V2 off) vs the new one.

Recorded (runtime/actions.jsonl + S001.metrics.jsonl):
  e126 (the nerve) off_screen x2, 7.2 s / 7.6 s, camera=[center,rotate_left,rotate_left,rotate_right,rotate_left,
       rotate_right,rotate_left]: the camera on Lae'zel, the nerve behind it
  e126 clicked, tries=83, 12.6 s, camera=[rotate_occluded_right,rotate_occluded_left], hover_via=origin
  e127 clicked, tries=8, 1.0 s;  e124 clicked, tries=1, 0.3 s

The sim: frames every FRAME_S; a synced move reaches the game on the next frame and the picking helper reports it
one frame later (the conservative model; PICK_SAME_FRAME = the optimistic one); RPCs cost what the metrics show.
Calibrated so the old path lands near the recorded tries/time (~130 ms a try).
"""

import math
import unittest
from unittest import mock

from bg3ai import clicker

FRAME_S = 0.05
RPC_S = {"ui.screen_pos": 0.019, "ui.hover_check": 0.006, "ui.state": 0.019}
DRAG_FRAMES = 3


class Clock:
    def __init__(self):
        self.t = 1000.0

    def monotonic(self):
        return self.t

    def sleep(self, s):
        self.t += s

    def next_frame(self, n=1):
        k = math.floor(self.t / FRAME_S + 1e-9)
        self.t = (k + n) * FRAME_S


class SimInput:
    def __init__(self, clock, scene):
        self.c, self.scene = clock, scene
        self.moves = 0

    def window_size(self):
        return (1920, 1080)

    def move(self, x, y, sync=True):
        self.c.next_frame()
        self.moves += 1
        self.scene.cursor_moved((x, y), self.c.t)

    def wait_frames(self, n, sync=True):
        self.c.next_frame(n)

    def wheel(self, n, x=None, y=None, sync=True):
        self.c.next_frame()
        if n < 0:
            self.scene.zoom *= 0.8

    def drag(self, x1, y1, x2, y2, button="L", steps=12):
        self.c.next_frame(DRAG_FRAMES)
        self.scene.dragged(x2 - x1)

    def key(self, k, sync=True):
        self.c.next_frame()

    def click(self, x, y, button="L", count=1, sync=True):
        self.c.next_frame()
        self.scene.clicked = (x, y)


# Where each aim lands relative to the entity origin's "low" point, in px (an item: "auto" resolves to "low").
AIM_PX = {"low": (0, 0), "chest": (0, -55), "feet": (0, 20), "mid": (0, -40), "high": (0, -65), "top": (0, -90)}
PX_PER_M = 50


class Scene:
    """One target seen from a sequence of camera views (one view per occluded-pass drag).

    bearing: the target's direction from the camera in degrees (0 = straight ahead, + right, |b| > 90 behind);
    a drag of dx < 0 turns the view right (b decreases) unless `inverted`. Visible: |b| <= 35.
    views[i] = (occluded, hit_box): every in-window point hovers a no-id object when `occluded`, except inside
    hit_box = (x0, y0, x1, y1), which hovers the target (via origin)."""

    def __init__(self, clock, origin=(1017, 568), bearing=None, inverted=False, views=None, pick_same_frame=False):
        self.c, self.origin, self.bearing, self.inverted = clock, origin, bearing, inverted
        self.views = views or [(False, None)]
        self.view = 0
        self.zoom = 1.0                    # zooming out pulls an edge point toward the centre
        self.pick_same_frame = pick_same_frame
        self.pick = ((0, 0), 0.0)          # (cursor the helper has, from when)
        self.prev_pick = (0, 0)
        self.clicked = None

    # input side
    def cursor_moved(self, xy, t):
        self.prev_pick = self.picked_at(t)
        self.pick = (xy, t if self.pick_same_frame else t + FRAME_S)

    def dragged(self, dx):
        turn = 40.0 if dx < 0 else -40.0
        if self.inverted:
            turn = -turn
        if self.bearing_matters():
            self.bearing -= turn
            self.bearing = (self.bearing + 180) % 360 - 180
        else:
            self.view = min(self.view + 1, len(self.views) - 1)

    def bearing_matters(self):
        return self.bearing is not None

    def base(self):
        """The entity's "low" point in window px now (the hit box moves with it), None behind the camera."""
        b = self.bearing or 0.0
        if abs(b) > 90:
            return None
        nx = math.tan(math.radians(b)) / math.tan(math.radians(35)) * self.zoom
        return self.origin[0] + nx * 960, self.origin[1]

    def picked_at(self, t):
        xy, ready = self.pick
        return xy if t >= ready - 1e-9 else self.prev_pick

    # game side
    def rpc(self, op, args=None, context="server"):
        args = args or {}
        self.c.t += RPC_S.get(op, 0.01)
        if op == "ui.screen_pos":
            base = self.base()
            side = "left" if (self.bearing or 0) < 0 else "right"
            if base is None:
                return {"on_screen": False, "behind": True, "side": side}
            aim = args.get("aim") or "auto"
            aim = "low" if aim == "auto" else aim
            ax, ay = AIM_PX[aim]
            ax += float(args.get("ox") or 0) * PX_PER_M
            x, y = base[0] + ax, base[1] + ay
            ndc = [x / 960 - 1, y / 540 - 1]
            return {"ndc": ndc, "on_screen": -1 <= ndc[0] <= 1 and -1 <= ndc[1] <= 1, "side": side, "aim": aim}
        if op == "ui.hover_check":
            xy = self.picked_at(self.c.t)
            occluded, box = self.views[self.view]
            base = self.base()
            if base is None:
                return {"hovered": False, "cursor": list(xy)}
            rx, ry = xy[0] - (base[0] - self.origin[0]), xy[1] - (base[1] - self.origin[1])
            if box and box[0] <= rx <= box[2] and box[1] <= ry <= box[3]:
                return {"hovered": True, "via": "origin", "cursor": list(xy)}
            if occluded:
                return {"hovered": False, "hover_nouuid": True, "nouuid": [{"m": 1.2}], "cursor": list(xy)}
            return {"hovered": False, "cursor": list(xy)}
        if op == "ui.state":
            return {"screen": "game"}
        raise AssertionError(op)


def box_at(dx, dy, aim="low", ox=0.0, origin=(1017, 568), r=4):
    ax, ay = AIM_PX[aim]
    x, y = origin[0] + ax + ox * PX_PER_M + dx, origin[1] + ay + dy
    return (x - r, y - r, x + r, y + r)


def run(scene_kw, v2, pick_same_frame=False):
    clock = Clock()
    scene = Scene(clock, pick_same_frame=pick_same_frame, **scene_kw)
    inp = SimInput(clock, scene)
    clicker._drag["flip"] = False
    with mock.patch.object(clicker, "time", clock), mock.patch.object(clicker, "HOVER_V2", v2):
        t0 = clock.t
        r = clicker.click_entity(scene, inp, "e126")
        return r, clock.t - t0


# e126: occluded from the first view and after one turn right; after the turn left the leaf's "chest" aim, 14 px up
E126 = {"views": [(True, None), (True, None), (False, box_at(0, -14, "chest"))]}
E127 = {"views": [(True, box_at(-24, -10))]}     # the 8th point of the first aim
E124 = {"views": [(False, box_at(0, 0))]}        # the first point
# off_screen: the nerve straight behind-left of the camera (on Lae'zel), the drag turning the other way
BEHIND = {"bearing": -160.0, "inverted": True, "views": [(False, box_at(0, 0))]}


class Replay(unittest.TestCase):
    def setUp(self):
        self._flip = dict(clicker._drag)

    def tearDown(self):
        clicker._drag.update(self._flip)

    def test_e126_old_path_matches_the_recording(self):
        r, s = run(E126, v2=False)
        self.assertTrue(r.ok, r)
        self.assertEqual(r.detail["camera"], ["rotate_occluded_right", "rotate_occluded_left"])
        self.assertTrue(70 <= r.tries <= 95, r.tries)   # recorded 83
        self.assertTrue(11 <= s <= 15, s)               # recorded 12.6 s

    def test_e126_new_path_is_fast(self):
        r_old, s_old = run(E126, v2=False)
        r, s = run(E126, v2=True)
        self.assertTrue(r.ok, r)
        box = E126["views"][-1][1]   # the leaf's hit box (A14: the "high" aim's centre lands in it before the ring)
        self.assertTrue(box[0] <= r.pos[0] <= box[2] and box[1] <= r.pos[1] <= box[3], (r.pos, r_old.pos))
        self.assertEqual(r.detail["camera"], ["rotate_occluded_right", "rotate_occluded_left"])
        self.assertEqual(r.detail.get("settle"), "cursor")
        print(f"\n  e126 replay: old {r_old.tries} tries {s_old:.1f} s -> new {r.tries} tries {s:.1f} s")
        # 36 -> 44 tries since live window #1: the first view's probe also takes every aim's centre (the door fix).
        # The replay runs on the simulated clock only (clicker.time = Clock, frames = clock steps): the counts are
        # exact, not load-dependent. vis's 52 / 36 runs were the tree mid-edit (52 = centres in every probe, 36 =
        # before the door fix). A change here means the search order changed: update these numbers on purpose.
        # 44 -> 46 (A14): the first view's probe takes the 2 wall-mount aims' centres too (46/82 = 0.56).
        self.assertEqual((r_old.tries, r.tries), (82, 46))
        self.assertLessEqual(r.tries, int(r_old.tries * 0.57))
        self.assertLessEqual(s, s_old / 2.5)
        self.assertLessEqual(s, 5.7)   # 5.5 before A14's 2 wall-mount centres (+0.1 s simulated)

    def test_e126_new_path_with_same_frame_picking(self):
        r, s = run(E126, v2=True, pick_same_frame=True)
        self.assertTrue(r.ok, r)
        self.assertLessEqual(s, 3.8)

    def test_e127_and_e124_same_tries_less_time(self):
        for scene, want in ((E127, 8), (E124, 1)):
            r_old, s_old = run(scene, v2=False)
            r, s = run(scene, v2=True)
            self.assertTrue(r_old.ok and r.ok, (r_old, r))
            self.assertEqual((r_old.tries, r.tries), (want, want))
            self.assertEqual(r.pos, r_old.pos)
            self.assertLessEqual(s, s_old + 0.06)   # a 1-try hit: the full first wait + the hit's second look
            if want > 1:
                self.assertLess(s, s_old * 0.8)

    def test_unoccluded_miss_still_searches_everything(self):
        # nothing in front, the hit box on the 5th aim's 7th offset (old: try 34): no early stop, same point found
        scene = {"views": [(False, box_at(0, 28, "mid", 0.5))]}
        r_old, _ = run(scene, v2=False)
        r, _ = run(scene, v2=True)
        self.assertTrue(r_old.ok and r.ok, (r_old, r))
        self.assertEqual(r.pos, r_old.pos)

    def test_door_hinge_ring_on_the_frame_no_camera_turn(self):
        # live window #1 (10-16 S050 Foundry door e7): the "auto" aim is the hinge; its 9 points hover the frame
        # (no-id), the leaf answers at another aim's centre. v2 turned the camera twice (19 tries, 1.6 s) first.
        door = {"views": [(True, box_at(0, 0, "mid", 0.9))]}
        r_old, s_old = run(door, v2=False)
        r, s = run(door, v2=True)
        self.assertTrue(r_old.ok and r.ok, (r_old, r))
        self.assertEqual(r.pos, r_old.pos)
        self.assertNotIn("camera", r.detail)            # no rotate_occluded_*
        self.assertLessEqual(r.tries, 9 + len(clicker.AIMS) - 1)
        self.assertLess(r.tries, r_old.tries)
        self.assertLess(s, s_old)

    def test_wall_torch_above_the_item_aims(self):
        # S070 21:08:39: a Dragon Head Torch ~1.8 m up the wall: every aim (≤ 1.1 m over the origin) hovered the
        # wall behind it, 89 tries and both camera turns, hover_miss. The "top" aim's centre takes it, no turn.
        torch = {"views": [(True, box_at(0, 0, "top")), (True, box_at(0, 0, "top")), (True, box_at(0, 0, "top"))]}
        r, _ = run(torch, v2=True)
        self.assertTrue(r.ok, r)
        self.assertNotIn("camera", r.detail)
        old = clicker.AIMS
        clicker.AIMS = [a for a in old if a[0] not in ("high", "top")]
        try:
            r_old, _ = run(torch, v2=True)
        finally:
            clicker.AIMS = old
        self.assertFalse(r_old.ok)
        self.assertEqual(r_old.reason, "hover_miss")

    def test_never_worse_than_old_on_the_first_ring_or_a_centre(self):
        # the same view, everything else a no-id occluder: a hit the old path makes on the first aim's ring or any
        # aim's centre, the new path makes in no more tries and with no camera turn
        spots = [box_at(dx, dy) for dx, dy in clicker.OFFSETS]
        # the wall-mount aims (A14, last in AIMS) have their own test; in this fake "high" sits on the chest ring
        spots += [box_at(0, 0, aim, ox) for aim, ox in clicker.AIMS[1:] if aim not in ("low", "high", "top")]
        for box in spots:
            scene = {"views": [(True, box)]}
            r_old, _ = run(scene, v2=False)
            r, _ = run(scene, v2=True)
            self.assertTrue(r.ok, (box, r))
            if not r_old.ok:   # a far aim's centre: the old ring-by-ring order runs out of its 8 s first
                continue
            self.assertLessEqual(r.tries, r_old.tries, box)
            self.assertNotIn("camera", r.detail, box)

    def test_behind_old_path_oscillates(self):
        r, s = run(BEHIND, v2=False)
        self.assertFalse(r.ok)
        self.assertEqual(r.reason, "off_screen")
        rot = [m for m in r.detail["camera"] if m.startswith("rotate")]
        self.assertEqual(len(rot), clicker.CAMERA_STEPS)
        self.assertIn("rotate_right", rot)              # left/right alternating, as recorded
        self.assertTrue(5 <= s <= 9, s)                  # recorded 7.2 s / 7.6 s

    def test_behind_new_path_turns_the_right_way(self):
        r, s = run(BEHIND, v2=True)
        self.assertTrue(r.ok, r)
        rot = [m for m in r.detail["camera"] if m.startswith("rotate")]
        self.assertLessEqual(len(rot), 5, rot)
        self.assertTrue(clicker._drag["flip"])          # learned: the next call turns the right way at once
        r2, s2 = run_learned(BEHIND)
        self.assertTrue(r2.ok, r2)
        self.assertLess(len([m for m in r2.detail["camera"] if m.startswith("rotate")]), len(rot))
        print(f"\n  behind replay: old off_screen -> new ok in {s:.1f} s ({len(rot)} turns), learned {s2:.1f} s")


def run_learned(scene_kw):
    clock = Clock()
    scene = Scene(clock, **scene_kw)
    inp = SimInput(clock, scene)
    with mock.patch.object(clicker, "time", clock), mock.patch.object(clicker, "HOVER_V2", True):
        t0 = clock.t
        return clicker.click_entity(scene, inp, "e126"), clock.t - t0


if __name__ == "__main__":
    unittest.main()

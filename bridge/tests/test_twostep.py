"""Offline tests for twostep.py (audit ACT-9, act): Revivify / Dimension Door by the player's clicks, the hotbar cell
(hotbar.arm), the first pick hover-verified, the ground click proven by the cursor (ui.ground_check: its world point,
no character under it, no warning cursor) and by the spell state after the click; and the use_ability wiring behind
HOTBAR_CAST_ENABLED.

Run: cd bridge && uv run python -m unittest discover -s tests -p 'test_twostep.py'
"""

from __future__ import annotations

import unittest
from unittest import mock

from test_hotbar import FakeInput, FakeWorld
from test_turn_macros import P1, Base  # noqa: F401 (sets BG3AI_RUNTIME first)

from bg3ai import hotbar, server, twostep
from bg3ai.game import GameError

DD = "Teleportation_DimensionDoor"
REV = "Teleportation_Revivify"


def to_screen(p):
    """The fake camera: world x/z on a flat floor -> window px (40 px per m across, 20 px per m deep)."""
    return 960 + 40 * p[0], 300 + 20 * p[2]


def to_world(x, y):
    return [(x - 960) / 40.0, 0.0, (y - 300) / 20.0]


class TwoStepWorld(FakeWorld):
    """FakeWorld's hotbar, plus a floor: characters at world spots, a two-step cast that takes a first pick (a
    character in `first_ok`), then a ground click not on a character and not in `bad` (the game leaves the spell in
    hand there). `landing_first` = the game's order is the landing, then who goes."""

    def __init__(self):
        super().__init__(max_targets=2)
        self.decks[1]["spells"] = ["Target_Guidance", DD, REV, "Shout_Shield", "Target_CureWounds"]
        self.wpos = {"caster": [0.0, 0.0, 0.0], "ally": [1.0, 0.0, 0.0], "body": [4.0, 0.0, 0.0]}
        self.targets = {u: to_screen(p) for u, p in self.wpos.items()}
        self.first_ok = {"caster", "ally", "body"}
        self.bad: list = []                 # predicates on a world point: the game won't take it
        self.cursor_reads = True            # T1: the cursor shape is readable
        self.landing_first = False
        self.ground_at = None
        self.offscreen_pos = False
        self.body_r: dict[str, float] = {}

    def char_at(self, x, y):
        w = to_world(x, y)
        for u, r in self.body_r.items():   # a body this wide (m) under the cursor ray: L8 Shadowheart
            p = self.wpos[u]
            if ((w[0] - p[0]) ** 2 + (w[2] - p[2]) ** 2) ** 0.5 <= r:
                return u
        for u, p in self.targets.items():
            if p and abs(x - p[0]) <= 4 and abs(y - p[1]) <= 4:
                return u
        return None

    def bad_at(self, w):
        return any(f(w) for f in self.bad)

    def on_click(self, x, y, button):
        if button == "R" or not self.armed:
            return super().on_click(x, y, button)
        u = self.char_at(x, y)
        want_ground = (len(self.picked) == 1) != self.landing_first
        if not want_ground:
            if u in self.first_ok:
                self.picked.append(u)
                if len(self.picked) == 2:
                    self.fire()
            return
        if u is not None:
            return                          # "not Character()": nothing happens
        w = to_world(x, y)
        if self.bad_at(w):
            return
        self.ground_at = w
        self.picked.append("ground")
        if len(self.picked) == 2:
            self.fire()

    def rpc(self, op, args=None, context="server"):
        args = args or {}
        if op == "ui.screen_pos" and "pos" in args:
            self.rpcs.append(op)
            if self.offscreen_pos:
                return {"on_screen": False, "ndc": [1.6, 0.0], "side": "right", "world": args["pos"]}
            x, y = to_screen(args["pos"])
            return {"on_screen": True, "ndc": [x / 960 - 1, y / 540 - 1], "x": x, "y": y, "world": args["pos"]}
        if op == "ui.screen_pos" and args.get("aim") == "feet" and not args.get("toward") \
                and self.targets.get(args.get("uuid")):
            self.rpcs.append(op)
            p = self.wpos[args["uuid"]]
            x, y = to_screen(p)
            return {"on_screen": True, "ndc": [0, 0], "x": x, "y": y, "world": list(p), "aim": "feet"}
        if op == "ui.screen_pos" and args.get("toward"):
            self.rpcs.append(op)
            a, b = self.wpos[args["uuid"]], self.wpos[args["toward"]]
            dx, dz = b[0] - a[0], b[2] - a[2]
            d = (dx * dx + dz * dz) ** 0.5
            k = min(args["dist"], d) / d
            p = [a[0] + dx * k, 0.0, a[2] + dz * k]
            x, y = to_screen(p)
            return {"on_screen": True, "ndc": [0, 0], "x": x, "y": y, "world": p, "aim": "feet"}
        if op == "ui.ground_check":
            self.rpcs.append(op)
            w = to_world(*self.cursor)
            u = self.char_at(*self.cursor)
            out = {"world": w, "on_avoid": u is not None and (u in (args.get("avoid") or []) or args.get("no_chars"))}
            if u:
                out["hover"] = u
            if args.get("pos") is not None:
                p = args["pos"]
                d = ((w[0] - p[0]) ** 2 + (w[2] - p[2]) ** 2) ** 0.5
                out.update(d_m=round(d, 2), near=d <= args.get("tol", 1.0))
            if self.cursor_reads and self.armed:
                warn = self.bad_at(w)
                out.update(cursor="CastWarning" if warn else "Cast", warning=warn)
            return out
        return super().rpc(op, args, context)


def _cast(world, spell, first, landing=None):
    inp = FakeInput(world)
    with mock.patch.object(hotbar.time, "sleep"), mock.patch.object(twostep.time, "sleep"):
        return twostep.twostep_cast(world, inp, "caster", spell, first, landing=landing), inp


def clicks(inp):
    return [(c[1], c[2]) for c in inp.log if c[0] == "click" and c[3] == "L"]


class TwoStepCast(unittest.TestCase):
    def setUp(self):
        hotbar._geom.update(size=None, cells={})
        hotbar._tabs.update(size=None, at={})
        self.w = TwoStepWorld()

    def test_dimension_door_alone_who_then_landing(self):
        r, inp = _cast(self.w, DD, "caster", [0.0, 0.0, 10.0])
        self.assertTrue(r.ok, r.as_dict())
        self.assertEqual(self.w.cast_done, [(DD, ["caster", "ground"])])
        self.assertEqual(self.w.ground_at, [0.0, 0.0, 10.0])
        self.assertEqual(r.picked, 2)
        self.assertEqual(r.detail["ground_clicks"], 1)
        self.assertEqual(r.detail["looks"], [{"d_m": 0.0, "cursor": "Cast", "warning": False, "on_avoid": False}])
        self.assertEqual(len(clicks(inp)), 3)              # cell, the caster, the landing
        self.assertFalse(any(c[0] == "key" for c in inp.log))
        self.assertEqual(r.detail["two_step"], "teleport")

    def test_dimension_door_with_the_ally(self):
        r, _ = _cast(self.w, DD, "ally", [5.0, 0.0, 8.0])
        self.assertTrue(r.ok, r.as_dict())
        self.assertEqual(self.w.cast_done, [(DD, ["ally", "ground"])])

    def test_warning_cursor_at_the_landing_is_never_clicked_a_nudge_is(self):
        self.w.bad = [lambda p: abs(p[0]) < 0.2 and abs(p[2] - 10) < 0.2]
        r, inp = _cast(self.w, DD, "caster", [0.0, 0.0, 10.0])
        self.assertTrue(r.ok, r.as_dict())
        self.assertEqual(r.detail["ground_clicks"], 1)          # the warning spot was looked at, not clicked
        self.assertEqual(r.detail["looks"][0]["cursor"], "CastWarning")
        self.assertAlmostEqual(self.w.ground_at[0], 0.4)
        self.assertEqual(len(clicks(inp)), 3)

    def test_unreadable_cursor_the_state_after_the_click_decides(self):
        self.w.cursor_reads = False
        self.w.bad = [lambda p: abs(p[0]) < 0.2 and abs(p[2] - 10) < 0.2]
        r, inp = _cast(self.w, DD, "caster", [0.0, 0.0, 10.0])
        self.assertTrue(r.ok, r.as_dict())
        self.assertEqual(r.detail["ground_clicks"], 2)          # T4: the refused click left the spell in hand

    def test_every_spot_refused_cancels(self):
        self.w.bad = [lambda p: True]
        r, inp = _cast(self.w, DD, "caster", [0.0, 0.0, 10.0])
        self.assertFalse(r.ok)
        self.assertEqual((r.stage, r.reason), ("ground", "ground_refused"))
        self.assertIn("CastWarning", r.detail["why"])
        self.assertEqual(r.detail["cancelled"], "escape")
        self.assertIsNone(self.w.armed)
        self.assertEqual(self.w.cast_done, [])
        self.assertEqual(len(clicks(inp)), 2)                   # cell + the caster; no ground click

    def test_landing_off_screen_turns_the_camera_then_gives_up(self):
        self.w.offscreen_pos = True
        r, inp = _cast(self.w, DD, "caster", [30.0, 0.0, 0.0])
        self.assertFalse(r.ok)
        self.assertEqual((r.stage, r.reason), ("ground", "off_screen"))
        self.assertEqual(r.detail["camera"], ["rotate_right"] * twostep.VIEW_STEPS)
        self.assertEqual(r.detail["cancelled"], "escape")

    def test_landing_first_order_is_tried_once_when_the_first_pick_is_not_taken(self):
        self.w.landing_first = True
        r, _ = _cast(self.w, DD, "caster", [0.0, 0.0, 10.0])
        self.assertTrue(r.ok, r.as_dict())
        self.assertTrue(r.detail["order_flipped"])
        self.assertEqual(self.w.cast_done, [(DD, ["ground", "caster"])])

    def test_revivify_body_then_the_ground_beside_it_toward_the_caster(self):
        r, inp = _cast(self.w, REV, "body")
        self.assertTrue(r.ok, r.as_dict())
        self.assertEqual(self.w.cast_done, [(REV, ["body", "ground"])])
        self.assertAlmostEqual(self.w.ground_at[0], 4.0 - twostep.SPOT_DISTS[0])
        self.assertEqual(r.detail["two_step"], "revive")
        body = self.w.targets["body"]
        self.assertNotIn(body, clicks(inp)[2:])                  # the ground click is not on the body

    def test_l5_a_one_step_cast_by_the_first_pick_gets_no_ground_click(self):
        """Review L5: routed by the name regex, a spell the game casts on the first pick (one-step) got a ground
        click with nothing in hand: a plain move order. The cast started: done, no ground click."""
        w = self.w
        base = w.on_click

        def on_click(x, y, button):
            if button == "L" and w.armed and not w.picked and w.char_at(x, y) in w.first_ok:
                w.picked.append(w.char_at(x, y))
                w.fire()
                return
            return base(x, y, button)
        w.on_click = on_click
        r, inp = _cast(w, REV, "body")
        self.assertTrue(r.ok, r.as_dict())
        self.assertTrue(r.detail.get("one_step"), r.as_dict())
        self.assertIsNone(w.ground_at)
        self.assertNotIn("ui.ground_check", w.rpcs)

    def test_m1_should_stop_before_the_ground_click(self):
        inp = FakeInput(self.w)
        with mock.patch.object(hotbar.time, "sleep"), mock.patch.object(twostep.time, "sleep"):
            r = twostep.twostep_cast(self.w, inp, "caster", DD, "caster", landing=[0.0, 0.0, 10.0],
                                     should_stop=lambda: bool(self.w.picked))
        self.assertFalse(r.ok)
        self.assertEqual((r.stage, r.reason), ("ground", "cancelled"))
        self.assertEqual(self.w.cast_done, [])
        self.assertIsNone(self.w.armed)
        self.assertEqual(len(clicks(inp)), 2)                   # cell + who goes; no ground click

    def test_revivify_spot_on_a_character_skips_to_the_next(self):
        self.w.wpos["ally"] = [4.0 - 1.2, 0.0, 0.0]            # the ally stands on the first spot
        self.w.targets["ally"] = to_screen(self.w.wpos["ally"])
        r, _ = _cast(self.w, REV, "body")
        self.assertTrue(r.ok, r.as_dict())
        self.assertAlmostEqual(self.w.ground_at[0], 4.0 - 1.8)
        self.assertEqual(r.detail["ground_clicks"], 1)

    def test_revivify_body_not_taken_cancels_without_a_ground_click(self):
        self.w.first_ok = {"caster", "ally"}
        r, inp = _cast(self.w, REV, "body")
        self.assertFalse(r.ok)
        self.assertEqual((r.stage, r.reason), ("first", "not_picked"))
        self.assertNotIn("order_flipped", r.detail)
        self.assertEqual(r.detail["cancelled"], "escape")
        self.assertEqual(len(clicks(inp)), 2)

    def test_body_off_screen(self):
        self.w.targets["body"] = None
        r, _ = _cast(self.w, REV, "body")
        self.assertEqual((r.ok, r.stage, r.reason), (False, "first", "off_screen"))

    def test_not_on_the_hotbar_and_no_input(self):
        self.w.decks[1]["spells"] = ["Target_Guidance"]
        r, inp = _cast(self.w, DD, "caster", [0.0, 0.0, 10.0])
        self.assertEqual((r.ok, r.stage, r.reason), (False, "slot", "not_on_hotbar"))
        self.assertEqual(clicks(inp), [])
        r = twostep.twostep_cast(self.w, None, "caster", DD, "caster", landing=[0, 0, 1])
        self.assertEqual(r.reason, "no_input")

    def test_names(self):
        for s in (DD, REV, "Teleportation_Revivify_Scroll", "Teleportation_Resurrection"):
            self.assertTrue(twostep.is_two_step(s), s)
        self.assertFalse(twostep.is_two_step("Target_Bless"))
        self.assertTrue(twostep.is_revive(REV))
        self.assertFalse(twostep.is_revive(DD))

    def test_ground_ok(self):
        self.assertEqual(twostep.ground_ok({"near": True, "warning": False}), (True, None))
        self.assertEqual(twostep.ground_ok({"near": True})[0], True)    # no cursor read: the state decides
        self.assertFalse(twostep.ground_ok({"near": False, "d_m": 3})[0])
        self.assertFalse(twostep.ground_ok({"on_avoid": True})[0])
        self.assertFalse(twostep.ground_ok({"error": "no op"})[0])


class ScrollWorld(TwoStepWorld):
    """A scroll's Use already put a one-pick point/cone spell in hand (no hotbar cell): the game takes one ground
    click that is not on a character and not `bad`, then casts."""

    def on_click(self, x, y, button):
        if button == "R" or not self.armed:
            return FakeWorld.on_click(self, x, y, button)
        if self.char_at(x, y) is not None:
            return
        w = to_world(x, y)
        if self.bad_at(w):
            return
        self.ground_at = w
        self.picked.append("ground")
        self.fire()


class GroundClick(unittest.TestCase):
    """S066 #66 Scroll of Cone of Cold x,y,z 56,-53,996.5 and #143 Scroll of Misty Step x,y,z 24,-43,1005 came back
    "cast on <the reader> by a click": the spell in hand is aimed at the ground at the point instead."""

    def setUp(self):
        self.w = ScrollWorld()
        self.w.armed = "Zone_ConeOfCold"

    def _go(self, pos):
        inp = FakeInput(self.w)
        with mock.patch.object(hotbar.time, "sleep"), mock.patch.object(twostep.time, "sleep"):
            return twostep.ground_click(self.w, inp, "caster", "Scroll of Cone of Cold", pos), inp

    def test_the_point_is_clicked_on_the_ground(self):
        r, inp = self._go([0.0, 0.0, 10.0])
        self.assertTrue(r.ok, r.as_dict())
        self.assertEqual(self.w.cast_done, [("Zone_ConeOfCold", ["ground"])])
        self.assertEqual(self.w.ground_at, [0.0, 0.0, 10.0])
        self.assertEqual(clicks(inp), [to_screen([0.0, 0.0, 10.0])])   # no hotbar cell: the scroll armed it

    def test_a_character_on_the_point_is_not_clicked_a_nudge_is(self):
        self.w.targets["ally"] = to_screen([0.0, 0.0, 10.0])
        r, inp = self._go([0.0, 0.0, 10.0])
        self.assertTrue(r.ok, r.as_dict())
        self.assertAlmostEqual(self.w.ground_at[0], 0.4)
        self.assertEqual(len(clicks(inp)), 1)

    def test_a17_a_body_on_the_landing_wider_than_the_nudges_is_stepped_clear_of(self):
        """L8 23:58: Misty Step 6 m, every spot (the landing and +-0.4 m) read "a character is under the cursor
        (Shadowheart)": the landing is tried BODY_CLEAR_M from her feet, along the cast direction, and the result
        says where the ground point came from and how far the landing moved."""
        self.w.wpos["ally"] = [0.0, 0.0, 10.0]
        self.w.targets["ally"] = to_screen([0.0, 0.0, 10.0])
        self.w.body_r["ally"] = 0.7
        r, inp = self._go([0.0, 0.0, 10.0])
        self.assertTrue(r.ok, r.as_dict())
        self.assertEqual(self.w.ground_at, [0.0, 0.0, 10.0 + twostep.BODY_CLEAR_M])
        self.assertEqual(r.detail["clear_of"], ["ally"])
        self.assertEqual(r.detail["shifted_m"], 1.2)
        self.assertEqual(len(clicks(inp)), 1)
        self.assertEqual(r.detail["looks"][0]["hover"], "ally")
        self.assertIn("under the cursor: ally x1", twostep.looks_brief(r.detail["looks"]))

    def test_a17_clear_spots(self):
        # the landing on the body: along the cast direction; else away from the body through the landing
        self.assertEqual(twostep.clear_spots([0, 0, 10], [0, 0, 10], [0, 0, 0])[0], [0.0, 0, 11.2])
        self.assertEqual(twostep.clear_spots([1, 0, 10], [0, 0, 10])[0], [1.2, 0, 10.0])
        # all within CLEAR_MAX_SHIFT_M of the landing; the turned ones too
        for p in twostep.clear_spots([0, 0, 10], [0, 0, 10], [0, 0, 0]):
            self.assertLessEqual(((p[0]) ** 2 + (p[2] - 10) ** 2) ** 0.5, twostep.CLEAR_MAX_SHIFT_M)
        self.assertEqual(len(twostep.clear_spots([0, 0, 10], [0, 0, 10], [0, 0, 0])), 5)

    def test_nothing_in_hand_no_click(self):
        # a ground click with no spell in hand is a move order (movement spent in combat)
        self.w.armed = None
        r, inp = self._go([0.0, 0.0, 10.0])
        self.assertFalse(r.ok)
        self.assertEqual((r.stage, r.reason), ("arm", "not_armed"))
        self.assertEqual(clicks(inp), [])

    def test_every_spot_refused_puts_the_spell_back(self):
        self.w.bad = [lambda p: True]
        r, inp = self._go([0.0, 0.0, 10.0])
        self.assertFalse(r.ok)
        self.assertEqual((r.stage, r.reason), ("ground", "ground_refused"))
        self.assertEqual(r.detail["cancelled"], "escape")
        self.assertIsNone(self.w.armed)
        self.assertEqual((self.w.cast_done, clicks(inp)), ([], []))


class FakeTwoStep:
    def __init__(self, g, ok=True, reason=None, stage=None, raises=False):
        self.g, self.ok, self.reason, self.stage, self.raises = g, ok, reason, stage, raises
        self.calls: list[tuple] = []

    def __call__(self, game, inp, caster, spell, first, landing=None, select=True, should_stop=None):
        if select and self.g.active != caster:
            game.run_job("act.select", {"char": caster}, wait_s=5)   # like hotbar.arm
        running = [j for j in self.g.jobs.values() if j["op"] == "act.cast" and j["status"] == "running"]
        self.calls.append((caster, spell, first, landing, [j["args"].get("ui") for j in running]))
        self.select = select
        if isinstance(self.raises, Exception):
            raise self.raises
        if self.raises:
            raise GameError("ui.ground_check: no such op")
        return hotbar.HotbarCastResult(self.ok, reason=self.reason, stage=self.stage, picked=2 if self.ok else 0,
                                       elapsed_s=1.4, budget_s=twostep.BUDGET_S,
                                       detail={"why": f"{self.reason} (fake)"} if self.reason else {})


class TwoStepRoute(Base):
    def setUp(self) -> None:
        super().setUp()
        self.g.dur["act.cast"] = 0.05
        saved = (hotbar.HOTBAR_CAST_ENABLED, twostep.twostep_cast, server._inp)
        self.addCleanup(lambda: (setattr(hotbar, "HOTBAR_CAST_ENABLED", saved[0]),
                                 setattr(twostep, "twostep_cast", saved[1]), setattr(server, "_inp", saved[2])))
        server._inp = lambda: object()
        self.fake = FakeTwoStep(self.g)
        twostep.twostep_cast = self.fake
        self.p1 = server.H.get(P1)

    def cast_args(self) -> list[dict]:
        return [a for _, a in self.g.ops("act.cast")]

    def test_off_sends_the_osiris_form_the_mod_refuses(self):
        hotbar.HOTBAR_CAST_ENABLED = False
        server.use_ability(DD, x=1.0, y=0.0, z=10.0, who=self.p1)
        (a,) = self.cast_args()
        self.assertNotIn("ui", a)
        self.assertEqual(self.fake.calls, [])

    def test_on_dimension_door_alone(self):
        hotbar.HOTBAR_CAST_ENABLED = True
        out = server.use_ability(DD, x=1.0, y=0.0, z=10.0, who=self.p1)
        (a,) = self.cast_args()
        self.assertTrue(a["ui"])
        self.assertEqual(a["pos"], [1.0, 0.0, 10.0])
        self.assertNotIn("target", a)                        # the mod makes the caster the one who goes
        self.assertEqual(self.fake.calls, [(P1, DD, P1, [1.0, 0.0, 10.0], [True])])
        self.assertIn("hotbar: who goes + the landing clicked in 1.4s", out)
        self.assertEqual(self.g.ops("job.cancel"), [])

    def test_on_dimension_door_with_an_ally_from_targets(self):
        hotbar.HOTBAR_CAST_ENABLED = True
        server.use_ability(DD, targets=[self.gob], x=1.0, y=0.0, z=10.0, who=self.p1)
        (a,) = self.cast_args()
        self.assertEqual(a["target"], self.g_uuid(self.gob))
        self.assertNotIn("targets", a)
        self.assertEqual(self.fake.calls[0][2], a["target"])

    def test_on_revivify_body(self):
        hotbar.HOTBAR_CAST_ENABLED = True
        out = server.use_ability(REV, target=self.gob, who=self.p1)
        (a,) = self.cast_args()
        self.assertTrue(a["ui"])
        self.assertEqual(self.fake.calls[0][2:4], (a["target"], None))
        self.assertIn("the body + the ground beside it clicked", out)

    def test_a17_clicks_done_told_to_the_job_and_a_spell_left_in_hand_put_back(self):
        """L8 23:59 (se): Dimension Door "the game refused the cast", moved 0 m, the spell LEFT IN HAND. The mod job
        hears when the clicks are done (act.ui_clicked: a CastSpellFailed from before then is the upcast picker's),
        and whatever the outcome, a spell still in hand afterwards is put back."""
        hotbar.HOTBAR_CAST_ENABLED = True
        held = {"n": 0}

        def cancel(game, inp, char):
            held["n"] += 1
            return "escape"
        with mock.patch.object(hotbar, "cancel", cancel):
            out = server.use_ability(DD, x=1.0, y=0.0, z=10.0, who=self.p1)
        (clicked,) = [a for _, a in self.g.ops("act.ui_clicked")]
        self.assertIsNotNone(clicked.get("id"))
        self.assertEqual(held["n"], 1)
        self.assertIn("the spell was still in hand after the cast: put back (escape)", out)
        with mock.patch.object(hotbar, "cancel", lambda g, i, c: None):
            out = server.use_ability(DD, x=1.0, y=0.0, z=10.0, who=self.p1)
        self.assertNotIn("still in hand", out)
        # a failed route cancels the job instead: no act.ui_clicked
        self.fake.ok, self.fake.reason, self.fake.stage = False, "ground_refused", "ground"
        self.g.dur["act.cast"] = 5.0
        with mock.patch.object(hotbar, "cancel", lambda g, i, c: None):
            server.use_ability(DD, x=1.0, y=0.0, z=10.0, who=self.p1)
        self.assertEqual(len(self.g.ops("act.ui_clicked")), 2)
        self.assertEqual(len(self.g.ops("job.cancel")), 1)

    def test_route_failure_cancels_the_job_no_retry(self):
        hotbar.HOTBAR_CAST_ENABLED = True
        self.g.dur["act.cast"] = 5.0
        self.fake.ok, self.fake.reason, self.fake.stage = False, "ground_refused", "ground"
        out = server.use_ability(REV, target=self.gob, who=self.p1)
        self.assertEqual(len(self.cast_args()), 1)
        self.assertEqual(len(self.g.ops("job.cancel")), 1)
        self.assertIn("the two-step route failed at ground (ground_refused (fake)); nothing was cast", out)
        self.assertIn("Scroll of Revivify", out)

    def test_error_in_the_route_cancels_too(self):
        hotbar.HOTBAR_CAST_ENABLED = True
        self.g.dur["act.cast"] = 5.0
        self.fake.raises = True
        out = server.use_ability(DD, x=1.0, y=0.0, z=10.0, who=self.p1)
        self.assertEqual(len(self.g.ops("job.cancel")), 1)
        self.assertIn("failed at slot", out)

    def test_mod_refusal_no_clicks(self):
        hotbar.HOTBAR_CAST_ENABLED = True
        self.g.refuse_if = lambda op, a: "the landing is out of range (55.0 m, range 50.0 m)" if op == "act.cast" else None
        out = server.use_ability(DD, x=55.0, y=0.0, z=0.0, who=self.p1)
        self.assertEqual(self.fake.calls, [])
        self.assertIn("the landing is out of range", out)

    def test_upcast_stays_osiris(self):
        hotbar.HOTBAR_CAST_ENABLED = True
        server.use_ability(REV, target=self.gob, who=self.p1, upcast=5)
        self.assertNotIn("ui", self.cast_args()[0])
        self.assertEqual(self.fake.calls, [])

    def test_h1_unselected_caster_selected_before_the_ui_job(self):
        hotbar.HOTBAR_CAST_ENABLED = True
        self.g.supersede = True
        self.g.active = "p2-not-the-caster"
        out = server.use_ability(DD, x=1.0, y=0.0, z=10.0, who=self.p1)
        names = [op for op, _ in self.g.calls if op in ("act.select", "act.cast")]
        self.assertEqual(names, ["act.select", "act.cast"])
        self.assertFalse(self.fake.select)
        self.assertEqual(self.fake.calls[0][4], [True])
        self.assertIn("who goes + the landing clicked", out)

    def test_m2_input_error_cancels(self):
        hotbar.HOTBAR_CAST_ENABLED = True
        self.g.dur["act.cast"] = 5.0
        self.fake.raises = RuntimeError("the shim has not seen the game window yet")
        out = server.use_ability(DD, x=1.0, y=0.0, z=10.0, who=self.p1)
        self.assertEqual(len(self.g.ops("job.cancel")), 1)
        self.assertIn("failed at error (RuntimeError: the shim has not seen the game window yet)", out)

    def g_uuid(self, h):
        return server._uuid(h)


if __name__ == "__main__":
    unittest.main()

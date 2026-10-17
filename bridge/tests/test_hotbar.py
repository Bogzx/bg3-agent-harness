"""Offline tests for hotbar.py (audit ACT-8, se half): the hotbar cell found with the cursor, the cell click proven
by the spell in hand, one hover-verified click per target with a counter read, the tick for fewer targets than the
max, and a cancel on every later failure. A fake HUD maps the cursor to hotbar cells and holds the cast state."""

import unittest
from unittest import mock

from bg3ai import hotbar
from bg3ai.game import GameError, JobResult

W, H = 1920, 1080
PITCH = 44


class FakeInput:
    def __init__(self, world):
        self.world = world
        self.log: list[tuple] = []

    def window_size(self):
        return (W, H)

    def move(self, x, y, sync=True):
        self.world.cursor = (x, y)

    def wait_frames(self, n, sync=True):
        pass

    def click(self, x, y, button="L", count=1, sync=True):
        self.world.cursor = (x, y)
        self.log.append(("click", x, y, button))
        self.world.on_click(x, y, button)

    def key(self, k, sync=True):
        self.log.append(("key", k))
        self.world.on_key(k)

    def wheel(self, n, x=None, y=None, sync=True):
        self.log.append(("wheel", n))

    def drag(self, *a, **k):
        self.log.append(("drag",))


class FakeWorld:
    """Two decks side by side: Common (order 1, 2 cols x 1 row) at x 700, Class (order 2, 3 cols x 2 rows,
    column-first) right after it. Row 0 of each deck at y 1000, row 1 at 1000 + row_dy."""

    def __init__(self, row_dy=PITCH, max_targets=3):
        self.cursor = (0, 0)
        self.row_dy = row_dy
        self.y0 = 1000
        self.slot_args: list[dict] = []
        self.decks = [
            {"type": "CommonHotBar", "order": 1, "cols": 2, "rows": 1, "x0": 700,
             "spells": ["Target_MainHandAttack", "Projectile_Jump"]},
            {"type": "ClassHotBar", "order": 2, "cols": 3, "rows": 2, "x0": 700 + 2 * PITCH + 20,
             "spells": ["Target_Guidance", "Projectile_MagicMissile", "Target_Bless", "Shout_Shield",
                        "Target_CureWounds"]},
        ]
        self.selected = True
        self.rendered = True
        self.shown = "ClassHotBar"
        self.tab_at = None                  # (x, y) of the Class tab when paging is needed
        self.max = max_targets
        self.armed = None                   # spell in hand
        self.picked: list[str] = []
        self.phase = None
        self.cast_done: list = []
        self.arm_ignores = False
        self.refuse = set()                 # target uuids the game does not take
        self.escape_cancels = True
        self.right_cancels = True
        self.screen = "game"
        self.targets = {}                   # uuid -> (x, y) on screen; None = off screen
        self.rpcs: list[str] = []
        self.jobs: list[str] = []
        self.confirms = 0
        self.no_ids = False
        self.server_map = {"ClassHotBar": {"12": "Target_Bless"}}
        self.map_calls = 0

    # --- layout
    def cell_at(self, x, y):
        for d in self.decks:
            for i, sp in enumerate(d["spells"]):
                col, row = i // d["rows"], i % d["rows"]
                cx, cy = d["x0"] + col * PITCH, self.y0 + row * self.row_dy
                if abs(x - cx) < PITCH / 2 - 1 and abs(y - cy) < PITCH / 2 - 1:
                    if d["type"] == "ClassHotBar" and not self.rendered:
                        return None
                    return d, i + 1, sp
        return None

    # --- input
    def on_click(self, x, y, button):
        if button == "R":
            if self.armed and self.right_cancels:
                self.armed, self.picked = None, []
            return
        hit = self.cell_at(x, y)
        if hit and not self.armed:
            if not self.arm_ignores:
                self.armed, self.picked = hit[2], []
            return
        if self.tab_at and abs(x - self.tab_at[0]) < 30 and abs(y - self.tab_at[1]) < 12:
            self.rendered, self.shown = True, "ClassHotBar"
            return
        if self.armed:
            for u, p in self.targets.items():
                if p and abs(x - p[0]) <= 4 and abs(y - p[1]) <= 4 and u not in self.refuse:
                    self.picked.append(u)
                    if len(self.picked) >= self.max:
                        self.fire()
                    return

    def fire(self):
        self.cast_done.append((self.armed, list(self.picked)))
        self.armed, self.phase = None, "PrepareStart"

    def on_key(self, k):
        if k == "ESCAPE":
            if self.armed and self.escape_cancels:
                self.armed, self.picked = None, []
            elif not self.armed:
                self.screen = "game_menu" if self.screen == "game" else "game"

    # --- game API
    def rpc(self, op, args=None, context="server"):
        args = args or {}
        self.rpcs.append(op)
        if op == "ui.state":
            return {"screen": self.screen}
        if op == "ui.hotbar_slot":
            if self.no_ids and not args.get("map"):
                # live window #1: the slot VMs carry no spell id and the client entity has no hotbar component
                return {"selected": self.selected, "found": False, "ids": "none",
                        "why": "not on the hotbar (7 slots in 2 decks; no spell ids: no HotbarContainer)"}
            return self.slot(args)
        if op == "hotbar.map":
            self.map_calls += 1
            if self.server_map is None:
                return {"why": "no HotbarContainer"}
            return {"map": self.server_map, "bars": 2}
        if op == "ui.cast_state":
            st = {"armed": self.armed is not None, "can_force": bool(self.armed and self.picked),
                  "counter": self.armed is not None}
            if self.armed:
                st["spell"] = self.armed
                st["remaining"] = self.max - len(self.picked)
                st["cast"] = {"picked": len(self.picked), "phase": "None", "spell": self.armed}
            elif self.phase:
                st["cast"] = {"phase": self.phase}
            return st
        if op == "ui.cast_confirm":
            if not (self.armed and self.picked):
                raise GameError("the tick button is disabled")
            self.confirms += 1
            self.fire()
            return {"done": True}
        if op == "ui.cast_cancel":
            was = self.armed is not None
            self.armed, self.picked = None, []
            return {"done": True, "was_armed": was}
        if op == "ui.screen_pos":
            p = self.targets.get(args["uuid"])
            if p is None:
                return {"on_screen": False, "behind": True, "side": "left"}
            return {"on_screen": True, "ndc": [p[0] / W * 2 - 1, p[1] / H * 2 - 1], "x": p[0], "y": p[1],
                    "aim": "chest"}
        if op == "ui.hover_check":
            p = self.targets.get(args["uuid"])
            hov = p is not None and abs(self.cursor[0] - p[0]) <= 4 and abs(self.cursor[1] - p[1]) <= 4
            return {"hovered": hov}
        raise GameError(f"unknown op {op}")

    def slot(self, args):
        self.slot_args.append(dict(args))
        want = args["spell"].lower()
        out = {"selected": self.selected, "shown_deck": self.shown}
        hit = self.cell_at(*self.cursor)
        if hit:
            d, i, sp = hit
            out["at"] = {"deck": d["type"], "index": i, "spell": sp, "order": d["order"], "cols": d["cols"],
                         "rows": d["rows"]}
        if args.get("tabs") and self.tab_at and abs(self.cursor[0] - self.tab_at[0]) < 30 \
                and abs(self.cursor[1] - self.tab_at[1]) < 12:
            out["deck_button_over"] = "HotbarDeckButton_Class"
        for d in self.decks:
            for i, sp in enumerate(d["spells"]):
                if sp.lower() == want:
                    out.update(found=True, deck=d["type"], index=i + 1, cols=d["cols"], rows=d["rows"],
                               order=d["order"], can_use=True, spell=sp, cell={"w": 88, "h": 88},
                               deck_button="HotbarDeckButton_Class" if d["type"] == "ClassHotBar" else None,
                               rendered=self.rendered or d["type"] != "ClassHotBar")
                    out["over"] = bool(hit and hit[0] is d and hit[1] == i + 1)
                    return out
        out.update(found=False, why="not on the hotbar (7 slots in 2 decks)")
        return out

    def run_job(self, op, args, wait_s=0):
        self.jobs.append(op)
        if op == "act.select":
            self.selected = True
        return JobResult("done", result={})


def _cast(world, spell="Target_Bless", targets=("a", "b", "c")):
    inp = FakeInput(world)
    with mock.patch.object(hotbar.time, "sleep"):
        return hotbar.hotbar_cast(world, inp, "caster", spell, list(targets)), inp


class HotbarCast(unittest.TestCase):
    def setUp(self):
        hotbar._geom.update(size=None, cells={})
        hotbar._tabs.update(size=None, at={})
        hotbar._maps.clear()
        self.w = FakeWorld()
        self.w.targets = {"a": (500, 500), "b": (700, 480), "c": (900, 520), "far": None}

    def test_bless_three_targets(self):
        r, inp = _cast(self.w)
        self.assertTrue(r.ok, r.as_dict())
        self.assertEqual(r.picked, 3)
        self.assertEqual(self.w.cast_done, [("Target_Bless", ["a", "b", "c"])])
        self.assertEqual(self.w.confirms, 0)            # the 3rd target cast it by itself
        x, y = r.detail["cell_pos"]
        self.assertEqual(self.w.cell_at(x, y)[2], "Target_Bless")
        self.assertEqual([c[3] for c in inp.log if c[0] == "click"], ["L"] * 4)
        self.assertFalse(any(c[0] == "key" for c in inp.log))
        self.assertEqual(r.detail["cell"], {"deck": "ClassHotBar", "index": 3, "rows": 2, "cols": 3})
        self.assertGreater(r.detail["probes"], 1)       # the scan landed on Shield below it, the walk went up a row

    def test_cached_spot_second_cast_one_probe(self):
        _cast(self.w)
        r, _ = _cast(self.w)
        self.assertTrue(r.ok)
        self.assertEqual(r.detail["probes"], 1)

    def test_rows_grow_upward_sign_learned(self):
        # row 1 above row 0: the first row move goes the wrong way, falls off the bar and flips the sign
        self.w.row_dy = -PITCH
        r, _ = _cast(self.w, "Projectile_MagicMissile")
        self.assertTrue(r.ok, r.as_dict())
        x, y = r.detail["cell_pos"]
        self.assertEqual(self.w.cell_at(x, y)[2], "Projectile_MagicMissile")

    def test_cell_from_another_deck(self):
        # the scan's first hit is on the Common deck (left of Class): the walk crosses into Class
        self.w.decks[0]["x0"] = 960
        self.w.decks[1]["x0"] = 960 + 2 * PITCH + 20
        r, _ = _cast(self.w, "Target_CureWounds", ["a", "b"])
        self.assertTrue(r.ok, r.as_dict())
        x, y = r.detail["cell_pos"]
        self.assertEqual(self.w.cell_at(x, y)[2], "Target_CureWounds")
        self.assertEqual(self.w.confirms, 1)

    def test_fewer_targets_than_max_uses_the_tick(self):
        r, _ = _cast(self.w, targets=["a", "b"])
        self.assertTrue(r.ok, r.as_dict())
        self.assertEqual(self.w.confirms, 1)
        self.assertTrue(r.detail["confirmed"])
        self.assertEqual(self.w.cast_done, [("Target_Bless", ["a", "b"])])

    def test_same_target_twice(self):
        self.w.max = 3
        r, _ = _cast(self.w, "Projectile_MagicMissile", ["a", "a", "b"])
        self.assertTrue(r.ok, r.as_dict())
        self.assertEqual(self.w.cast_done, [("Projectile_MagicMissile", ["a", "a", "b"])])

    def test_target_not_taken_cancels(self):
        self.w.refuse = {"b"}
        r, inp = _cast(self.w)
        self.assertFalse(r.ok)
        self.assertEqual((r.stage, r.reason, r.picked), ("target", "not_picked", 1))
        self.assertEqual(r.detail["cancelled"], "escape")
        self.assertIsNone(self.w.armed)
        self.assertEqual(self.w.cast_done, [])

    def test_should_stop_between_clicks_cancels(self):
        # review M1: the step was cancelled after the first target: no more clicks, the spell put back
        inp = FakeInput(self.w)
        with mock.patch.object(hotbar.time, "sleep"):
            r = hotbar.hotbar_cast(self.w, inp, "caster", "Target_Bless", ["a", "b", "c"],
                                   should_stop=lambda: len(self.w.picked) >= 1)
        self.assertFalse(r.ok)
        self.assertEqual((r.stage, r.reason), ("target", "cancelled"))
        self.assertIsNone(self.w.armed)
        self.assertEqual(self.w.cast_done, [])
        self.assertEqual(len([c for c in inp.log if c[0] == "click" and c[3] == "L"]), 2)   # cell + target 1

    def test_should_stop_before_the_cell_click(self):
        inp = FakeInput(self.w)
        with mock.patch.object(hotbar.time, "sleep"):
            r = hotbar.hotbar_cast(self.w, inp, "caster", "Target_Bless", ["a"], should_stop=lambda: True)
        self.assertEqual((r.stage, r.reason), ("arm", "cancelled"))
        self.assertFalse([c for c in inp.log if c[0] == "click"])

    def test_late_start_is_a_cast(self):
        # review M3: the tick's cast starts after START_WAIT_S: one more read says it went, not "not_started"
        w = self.w
        real = w.rpc
        fire_after = {"n": None}

        def rpc(op, args=None, context="server"):
            if op == "ui.cast_confirm":
                fire_after["n"] = 2
                w.confirms += 1
                return {"done": True}
            if op == "ui.cast_state" and fire_after["n"] is not None:
                fire_after["n"] -= 1
                if fire_after["n"] == 0:
                    w.fire()
            return real(op, args, context)
        w.rpc = rpc
        with mock.patch.object(hotbar, "START_WAIT_S", 0.0):
            r, _ = _cast(w, targets=["a", "b"])
        self.assertTrue(r.ok, r.as_dict())
        self.assertEqual(w.cast_done, [("Target_Bless", ["a", "b"])])

    def test_off_screen_target_cancels(self):
        r, _ = _cast(self.w, targets=["a", "far", "c"])
        self.assertFalse(r.ok)
        self.assertEqual((r.stage, r.reason), ("target", "off_screen"))
        self.assertIsNone(self.w.armed)
        self.assertIn("cancelled", r.detail)

    def test_fired_early(self):
        self.w.max = 2
        r, _ = _cast(self.w)
        self.assertFalse(r.ok)
        self.assertEqual((r.stage, r.reason, r.picked), ("target", "fired_early", 2))
        self.assertNotIn("cancelled", r.detail)

    def test_not_on_hotbar(self):
        r, inp = _cast(self.w, "Target_HoldPerson")
        self.assertFalse(r.ok)
        self.assertEqual((r.stage, r.reason), ("slot", "not_on_hotbar"))
        self.assertEqual(inp.log, [])

    def test_cell_click_does_not_arm(self):
        self.w.arm_ignores = True
        r, inp = _cast(self.w)
        self.assertEqual((r.stage, r.reason), ("arm", "not_armed"))
        self.assertNotIn("cancelled", r.detail)
        self.assertFalse(any(c[0] == "key" for c in inp.log))

    def test_wrong_cell_cancels_and_forgets_the_spot(self):
        _cast(self.w)                                    # caches Bless's spot
        key = ("ClassHotBar", 3)
        self.assertIn(key, hotbar._geom["cells"])
        # the bar changed under us: Bless's old spot now holds Guidance
        sp = self.w.decks[1]["spells"]
        sp[0], sp[2] = sp[2], sp[0]
        self.w.decks[1]["spells"] = sp
        # the Lua read still says index 3 is Bless (a stale read): simulate by swapping the read too
        real = self.w.slot

        def stale(args):
            out = real(args)
            if out.get("found"):
                out.update(index=3, over=out.get("at", {}).get("index") == 3)
            return out
        self.w.slot = stale
        r, _ = _cast(self.w)
        self.assertEqual((r.stage, r.reason), ("arm", "wrong_spell"))
        self.assertEqual(r.detail["cancelled"], "escape")
        self.assertNotIn(key, hotbar._geom["cells"])
        self.assertIsNone(self.w.armed)

    def test_caster_selected_first(self):
        self.w.selected = False
        r, _ = _cast(self.w)
        self.assertTrue(r.ok, r.as_dict())
        self.assertEqual(self.w.jobs, ["act.select"])

    def test_paging_clicks_the_deck_tab(self):
        self.w.rendered, self.w.shown = False, "CommonHotBar"
        self.w.tab_at = (int(0.5 * W), int(0.83 * H))
        r, _ = _cast(self.w)
        self.assertTrue(r.ok, r.as_dict())
        self.assertEqual(r.detail["paging"]["tab"], "HotbarDeckButton_Class")

    def test_paging_tab_not_found(self):
        self.w.rendered, self.w.shown = False, "CommonHotBar"
        with mock.patch.object(hotbar, "LOCATE_BUDGET_S", 0.05):
            r, _ = _cast(self.w)
        self.assertEqual((r.stage, r.reason), ("locate", "not_rendered"))

    def test_spell_already_in_hand_is_put_back_first(self):
        self.w.armed = "Target_Guidance"
        r, _ = _cast(self.w)
        self.assertTrue(r.ok, r.as_dict())
        self.assertEqual(r.detail["cleared_before"], "escape")

    def test_cancel_escalates_to_right_click_then_command(self):
        self.w.armed, self.w.escape_cancels = "Target_Bless", False
        inp = FakeInput(self.w)
        self.assertEqual(hotbar.cancel(self.w, inp, "caster"), "right_click")
        self.w.armed, self.w.right_cancels = "Target_Bless", False
        self.assertEqual(hotbar.cancel(self.w, inp, "caster"), "command")
        self.assertIsNone(hotbar.cancel(self.w, inp, "caster"))   # nothing in hand: no input at all

    def test_covered_and_no_input(self):
        self.w.screen = "dialogue"
        r, _ = _cast(self.w)
        self.assertEqual((r.stage, r.reason), ("slot", "covered"))
        r = hotbar.hotbar_cast(self.w, None, "caster", "Target_Bless", ["a"])
        self.assertEqual(r.reason, "no_input")

    def test_budget_reported(self):
        r, _ = _cast(self.w)
        d = r.as_dict()
        self.assertEqual(d["budget_s"], 2.5)
        self.assertIn("elapsed_s", d)

    def test_no_ids_on_the_client_uses_the_server_map_once(self):
        # live window #1 (2026-10-16): ui.hotbar_slot said ids=none -> hotbar.map (server), then the read again
        hotbar._maps.clear()
        self.w.no_ids = True
        r, _ = _cast(self.w)
        self.assertTrue(r.ok, r.as_dict())
        self.assertEqual(self.w.cast_done, [("Target_Bless", ["a", "b", "c"])])
        self.assertEqual(self.w.map_calls, 1)            # cached: the locator's probes pass the map along

    def test_no_ids_and_no_server_map_fails_not_on_hotbar(self):
        hotbar._maps.clear()
        self.w.no_ids, self.w.server_map = True, None
        r, _ = _cast(self.w)
        self.assertFalse(r.ok)
        self.assertEqual(r.reason, "not_on_hotbar")
        self.assertIn("no spell ids", r.as_dict().get("why") or "")
        self.assertEqual(self.w.cast_done, [])

    def test_ids_on_the_client_skip_the_server(self):
        hotbar._maps.clear()
        r, _ = _cast(self.w)
        self.assertTrue(r.ok)
        self.assertEqual(self.w.map_calls, 0)

    def test_flag_off_by_default(self):
        self.assertFalse(hotbar.HOTBAR_CAST_ENABLED)


class Locate(unittest.TestCase):
    """Live window #2 (10-16 19:49): 39 probes along y=0.95 only (x 0.5..0.88), the 4 s budget gone, no cell seen."""

    def setUp(self):
        hotbar._geom.update(size=None, cells={})
        hotbar._maps.clear()
        self.w = FakeWorld()

    def _want(self, spell="Target_Bless"):
        self.w.cursor = (0, 0)
        return self.w.slot({"spell": spell})

    def test_hotbar_higher_and_left_found_by_the_grid(self):
        # decks at y 0.82 and x ~0.31: the old scan's first row never got there within its budget
        self.w.y0 = int(0.82 * H)
        for d, x0 in zip(self.w.decks, (520, 520 + 2 * PITCH + 20)):
            d["x0"] = x0
        inp = FakeInput(self.w)
        pos, last = hotbar.locate_cell(self.w, inp, "Target_Bless", None, self._want())
        self.assertIsNotNone(pos, last)
        self.assertTrue(last.get("over"))
        self.assertLess(last["probes"], 45)
        self.assertTrue(all(a.get("cache") for a in self.w.slot_args[1:]))   # every probe reuses the mod's walk

    def test_jump_to_the_next_deck_steps_over_the_gap(self):
        # the scan lands on Common first; Class sits 20 px past its edge: one jump lands in the gap, a half cell on
        # finds it (it used to re-plan the same jump until misses ran out)
        self.w.decks[1]["x0"] = 700 + 2 * PITCH + 20
        inp = FakeInput(self.w)
        loc = hotbar._Locator(self.w, inp, "Target_Bless", None, self._want())
        r0 = loc.probe(744, 1000)
        self.assertEqual(r0["at"]["deck"], "CommonHotBar")
        r = loc.walk(r0, deadline=hotbar.time.monotonic() + 5)
        self.assertIsNotNone(r)
        self.assertTrue(r["over"])

    def test_miss_reports_what_the_probes_saw(self):
        self.w.decks = []
        real = self.w.slot

        def slot(args):
            out = real(args)
            x, y = self.w.cursor
            if y > 0.9 * H:   # the bottom band is HUD, but nothing there reads as a cell
                out.update(over_ui=True, hover=["Concertina", "Border"])
            return out
        self.w.slot = slot
        want = {"found": True, "deck": "ClassHotBar", "index": 3, "rows": 2, "cols": 3, "order": 2,
                "cell": {"w": 88, "h": 88}}
        inp = FakeInput(self.w)
        with mock.patch.object(hotbar, "LOCATE_BUDGET_S", 0.3):
            pos, last = hotbar.locate_cell(self.w, inp, "Target_Bless", None, want)
        self.assertIsNone(pos)
        seen = last["seen"]
        self.assertGreater(seen["over_ui"], 0)
        self.assertEqual(seen["cells"], 0)
        self.assertEqual(seen["hover"]["chain"], ["Concertina", "Border"])
        self.assertIn("first_over_ui", seen)

    def test_on_the_decks_between_cells_looks_half_a_cell_around(self):
        # cells read within 16 px of their centres (12 px margins); the decks' area reads over_hud. The 0.5 column
        # falls in the margin between Common's two cells; no other grid point is on a cell.
        real_cell_at = self.w.cell_at

        def cell_at(x, y):
            hit = real_cell_at(x, y)
            if not hit:
                return None
            d, i, _ = hit
            col, row = (i - 1) // d["rows"], (i - 1) % d["rows"]
            cx, cy = d["x0"] + col * PITCH, self.w.y0 + row * self.w.row_dy
            return hit if abs(x - cx) <= 16 and abs(y - cy) <= 16 else None
        self.w.cell_at = cell_at
        real = self.w.slot

        def slot(args):
            out = real(args)
            x, y = self.w.cursor
            out["over_hud"] = 915 <= x <= 1005 and 960 <= y <= 1005
            return out
        self.w.slot = slot
        self.w.y0 = int(0.91 * H)            # Common's one row on the grid's 2nd row
        self.w.decks[0]["x0"] = 938          # centres 938 and 982: x 960 is between them
        self.w.decks[1]["x0"] = 2400         # Class off screen
        loc = hotbar._Locator(self.w, FakeInput(self.w), "Target_MainHandAttack", None,
                              self._want("Target_MainHandAttack"))
        with mock.patch.object(hotbar, "SCAN_FX_PASSES", ((0.0, 0.12, 0.24), (0.06, 0.18, 0.30))):
            r = loc.scan(hotbar.time.monotonic() + 5)
        self.assertIsNotNone(r)
        self.assertEqual(r["at"]["deck"], "CommonHotBar")
        self.assertEqual(r["_pos"], (982, round(0.91 * H)))   # the half-cell step right of (960, 983)


class Geometry(unittest.TestCase):
    def test_cell_rc_column_first(self):
        self.assertEqual([hotbar.cell_rc(i, 2) for i in (1, 2, 3, 4, 5)], [(0, 0), (0, 1), (1, 0), (1, 1), (2, 0)])
        self.assertEqual(hotbar.cell_rc(3, None), (2, 0))

    def test_default_pitch_scales_with_height(self):
        self.assertAlmostEqual(hotbar.default_pitch(1080), 44.0)
        self.assertAlmostEqual(hotbar.default_pitch(2160, {"w": 80}), 80.0)


if __name__ == "__main__":
    unittest.main()

"""Offline tests: the character name is set and verified before character creation is finished
(bg3ai.naming against a fake naming page and a fake input shim; no game needed).

Run: cd bridge && uv run python -m unittest discover -s tests
"""

from __future__ import annotations

import unittest
from unittest import mock

from bg3ai import naming
from bg3ai.naming import NamingError, ensure_name, name_and_finish, probe_points


HANDLE = "ResStr_3455818087"


class FakePage:
    """ui.cc_name / ui.cc_finish over a naming page whose box sits at rows box_y0..box_y1 (window coords).

    Like the live page: the character's name reads as a handle (unless `readable`), the box exists only
    after the page was shown once (`lazy_box` more polls), a click focuses the box and selects its text."""

    def __init__(self, text="Tav", readable=False, box=(1150, 1190), can_edit=True, focus_on_click=True,
                 typing_works=True, lazy_box=1, popup=False, popup_after=None, xs=(1500, 2300)):
        self.text, self.xs = text, xs
        self.popup = popup   # "Reset Tutorials" over the page: no hover reaches the box until it closes
        self.popup_after = popup_after   # the popup opens after this many cc_name reads on the naming page
        self.reads = 0
        self.name = text if readable else HANDLE
        self.readable = readable
        self.box, self.can_edit = box, can_edit
        self.focus_on_click, self.typing_works = focus_on_click, typing_works
        self.lazy_box, self.shown = lazy_box, False
        self.page, self.cursor, self.focused = 0, (0, 0), False
        self.finished = False
        self.calls: list[tuple[str, dict]] = []

    def on_dont_reset(self, x, y):
        return 2000 <= x <= 2300 and 760 <= y <= 800   # (0.56, 0.54) of 3840x1440

    def over(self, x, y):
        return self.box[0] <= y <= self.box[1] and self.xs[0] <= x <= self.xs[1]

    def view(self, want=None):
        if self.page == 3:
            self.reads += 1
            if self.popup_after is not None and self.reads > self.popup_after:
                self.popup, self.popup_after = True, None
        has_box = self.shown and self.lazy_box <= 0
        if self.shown and self.lazy_box > 0:
            self.lazy_box -= 1
        name = self.name if self.readable else None
        text = self.text if has_box else None
        v = {"name": name, "name_handle": None if self.readable else HANDLE, "text": text,
             "can_edit": self.can_edit, "page": self.page, "on_naming_page": self.page == 3, "box": has_box,
             "focused": self.focused,
             "hover_box": has_box and self.page == 3 and not self.popup and self.over(*self.cursor),
             "complete": True}
        if want is not None:
            v.update(want=want, confirmed=name == want, ok=text == want and name in (None, want))
        return v

    def rpc(self, op, args):
        self.calls.append((op, dict(args)))
        if op == "ui.cc_name":
            assert not args.get("set"), "the Text write route is gone"
            moved = False
            if args.get("page") and self.page != 3:
                self.page, self.shown, moved = 3, True, True
            v = self.view(args.get("name"))
            if moved:
                v["moved"] = True
            return v
        if op == "ui.box_open":
            return {"state": "Running", "box": bool(self.popup)}
        if op == "ui.confirm_probe":
            if not self.popup:
                return {"box": False}
            x, y = self.cursor
            return {"box": True, "title": "Reset Tutorials", "buttons": ["Reset Tutorials", "Don't Reset"],
                    "label": "Don't Reset", "over": self.on_dont_reset(x, y)}
        if op == "ui.cc_finish":
            v = self.view(args.get("name"))
            if args.get("name") is not None and not v["ok"]:
                raise RuntimeError("name check failed")
            self.finished = True
            return {"finished": True}
        raise AssertionError(op)


class FakeInput:
    def __init__(self, page: FakePage, size=(3840, 1440)):
        self.page, self.size = page, size
        self.typed: list[str] = []
        self.clicks: list[tuple[int, int]] = []
        self.selected = False

    def window_size(self):
        return self.size

    def move(self, x, y, sync=True):
        self.page.cursor = (x, y)

    def wait_frames(self, n, sync=True):
        pass

    def sync(self, timeout_ms=5000):
        pass

    def click(self, x, y, button="L", count=1, sync=True):
        self.page.cursor = (x, y)
        self.clicks.append((x, y))
        if self.page.popup and self.page.on_dont_reset(x, y):
            self.page.popup = False
        if self.page.focus_on_click and self.page.over(x, y):
            self.page.focused = self.selected = True

    def key(self, keys, sync=True):
        if keys != "BACKSPACE" or not self.typed or not self.typed[-1].startswith("<BACKSPACE"):
            self.typed.append(f"<{keys}>")
        else:
            n = self.typed[-1][len("<BACKSPACE"):-1]
            self.typed[-1] = f"<BACKSPACE{int(n or 1) + 1}>"
        if not self.page.focused:
            return
        if keys == "END":
            self.selected = False  # LCTRL+A does nothing live
        elif keys == "BACKSPACE":
            self.page.text = "" if self.selected else self.page.text[:-1]
            self.selected = False

    def text(self, s, sync=True):
        self.typed.append(s)
        if self.page.focused:
            self.page.text = s if self.selected else self.page.text + s
            self.selected = False
            if self.page.typing_works and self.page.readable:
                self.page.name = self.page.text


@mock.patch.object(naming.time, "sleep", lambda s: None)
class EnsureName(unittest.TestCase):
    def setUp(self):
        naming._last_at.clear()

    def test_already_named(self):
        page = FakePage(text="Claude", lazy_box=0)
        page.shown = True
        self.assertEqual(ensure_name(page.rpc, None, "Claude")["via"], "already")
        self.assertEqual(len(page.calls), 1)

    @mock.patch.dict(naming.os.environ, {"BG3AI_CC_PACED": "1"})
    def test_typed_over_unreadable_handle(self):
        # The live case: the character's name is a handle, the box shows Tav; the box appears late.
        # Paced (the old route): scan + centring walk.
        page = FakePage(lazy_box=3)
        inp = FakeInput(page)
        res = ensure_name(page.rpc, inp, "Claude")
        self.assertEqual((res["how"], res["pacing"]), ("scan", "fixed"))
        self.assertEqual(res["via"], "typed")
        self.assertEqual(page.text, "Claude")
        self.assertEqual(inp.typed, ["<END>", f"<BACKSPACE{naming.MAX_LEN + 2}>", "Claude", "<RETURN>"])
        x, y = res["at"]
        self.assertTrue(page.over(x, y))
        self.assertAlmostEqual(y, sum(page.box) / 2, delta=12)  # centred on the box

    def test_long_old_name_is_cleared(self):
        page = FakePage(text="x" * naming.MAX_LEN)
        self.assertEqual(ensure_name(page.rpc, FakeInput(page), "Claude")["via"], "typed")
        self.assertEqual(page.text, "Claude")

    def test_readable_name_typed(self):
        page = FakePage(readable=True)
        self.assertEqual(ensure_name(page.rpc, FakeInput(page), "Claude")["via"], "typed")
        self.assertEqual(page.name, "Claude")

    def test_box_never_appears(self):
        page = FakePage(lazy_box=10**9)
        with mock.patch.object(naming.time, "monotonic", side_effect=range(0, 10**6, 1)):
            with self.assertRaisesRegex(NamingError, "no name box"):
                ensure_name(page.rpc, FakeInput(page), "Claude")

    def test_no_shim_fails_loudly(self):
        with self.assertRaisesRegex(NamingError, "input shim"):
            ensure_name(FakePage().rpc, None, "Claude")

    def test_box_not_found(self):
        page = FakePage(box=(-50, -10))
        with self.assertRaisesRegex(NamingError, "never reported the cursor"):
            ensure_name(page.rpc, FakeInput(page), "Claude")

    def test_click_does_not_focus(self):
        page = FakePage(focus_on_click=False)
        inp = FakeInput(page)
        with self.assertRaisesRegex(NamingError, "didn't focus"):
            ensure_name(page.rpc, inp, "Claude")
        self.assertNotIn("Claude", inp.typed)

    def test_typing_not_taken_by_readable_character(self):
        page = FakePage(readable=True, typing_works=False)
        with self.assertRaisesRegex(NamingError, "character is 'Tav'"):
            ensure_name(page.rpc, FakeInput(page), "Claude")

    def test_origin_name_fixed(self):
        with self.assertRaisesRegex(NamingError, "cannot be changed"):
            ensure_name(FakePage(can_edit=False).rpc, None, "Claude")

    def test_bad_names(self):
        for bad in ("", "  ", " Claude", "a\nb", "x" * 21):
            with self.assertRaises(NamingError, msg=repr(bad)):
                ensure_name(FakePage().rpc, None, bad)

    def test_name_then_finish(self):
        page = FakePage()
        out = name_and_finish(page.rpc, FakeInput(page), "Claude")
        self.assertTrue(page.finished)
        self.assertEqual(out["name"]["via"], "typed")
        self.assertEqual([op for op, _ in page.calls][-1], "ui.cc_finish")

    def test_failed_naming_never_finishes(self):
        page = FakePage(focus_on_click=False)
        with self.assertRaises(NamingError):
            name_and_finish(page.rpc, FakeInput(page), "Claude")
        self.assertFalse(page.finished)
        self.assertNotIn("ui.cc_finish", [op for op, _ in page.calls])


def _reads(page):
    return sum(1 for op, a in page.calls if op == "ui.cc_name")


@mock.patch.object(naming.time, "sleep", lambda s: None)
class StateWaits(unittest.TestCase):
    """The default pacing: hover where the box is predicted/cached, wait on state, no fixed sleeps or centring."""

    def setUp(self):
        naming._last_at.clear()

    def test_predicted_point_no_centring_walk(self):
        # live 2026-10-08: the box was at 960,891 on 1920x1080, exactly probe_points' first point
        page = FakePage(box=(860, 920), xs=(700, 1220))
        inp = FakeInput(page, size=(1920, 1080))
        res = ensure_name(page.rpc, inp, "Orsen Vale")
        self.assertEqual((res["how"], res["pacing"], res["at"]), ("predicted", "state", [960, 891]))
        self.assertEqual(page.text, "Orsen Vale")
        self.assertEqual(inp.clicks, [(960, 891)])
        self.assertLessEqual(_reads(page), 10)
        naming._last_at.clear()
        with mock.patch.dict(naming.os.environ, {"BG3AI_CC_PACED": "1"}):
            page2 = FakePage(box=(860, 920), xs=(700, 1220))
            ensure_name(page2.rpc, FakeInput(page2, size=(1920, 1080)), "Orsen Vale")
        self.assertGreater(_reads(page2), 3 * _reads(page))  # the centring walk: ~2x12 more hovers

    def test_name_typed_over_the_selection(self):
        # naut2 (2026-10-17): `type` ~1.55 s = END + BACKSPACE x22 + RETURN at ~3 frames a key. The click selects
        # the box's text, so the name goes over it and RETURN follows: 2 shim commands instead of 25.
        page = FakePage(box=(860, 920), xs=(700, 1220))
        inp = FakeInput(page, size=(1920, 1080))
        res = ensure_name(page.rpc, inp, "Corran Vell")
        self.assertEqual(inp.typed, ["Corran Vell", "<RETURN>"])
        self.assertEqual((res["typed"], page.text), ("over_selection", "Corran Vell"))

    def test_no_selection_clears_only_what_the_box_holds(self):
        page = FakePage(box=(860, 920), xs=(700, 1220))
        inp = FakeInput(page, size=(1920, 1080))
        inp.click = lambda x, y, button="L", count=1, sync=True: (       # focuses, selects nothing
            setattr(page, "cursor", (x, y)), inp.clicks.append((x, y)),
            setattr(page, "focused", page.over(x, y)))
        res = ensure_name(page.rpc, inp, "Claude")
        # "Tav" + "Claude" appended -> clear 9 + 2, type again
        self.assertEqual(inp.typed, ["Claude", "<END>", "<BACKSPACE11>", "Claude", "<RETURN>"])
        self.assertEqual((res["typed"], page.text), ("cleared", "Claude"))

    def test_typed_name_that_never_shows_still_fails(self):
        page = FakePage(box=(860, 920), xs=(700, 1220))
        inp = FakeInput(page, size=(1920, 1080))
        inp.text = lambda s, sync=True: inp.typed.append(s)               # text never reaches the box
        with self.assertRaises(NamingError):
            ensure_name(page.rpc, inp, "Claude")
        self.assertFalse(page.finished)

    def test_cached_point_first(self):
        # the box sits off the layout's prediction: the first call scans, the next one goes straight there
        page = FakePage(box=(700, 760), xs=(700, 1220))
        inp = FakeInput(page, size=(1920, 1080))
        self.assertEqual(ensure_name(page.rpc, inp, "Ilse")["how"], "scan")
        page2 = FakePage(box=(700, 760), xs=(700, 1220))
        res = ensure_name(page2.rpc, FakeInput(page2, size=(1920, 1080)), "Ilse")
        self.assertEqual(res["how"], "cached")
        self.assertLessEqual(_reads(page2), 10)

    def test_popup_opens_after_the_page(self):
        # Reset Tutorials opening a moment after the naming page: the first popup check sees nothing, the quick
        # hover comes up empty, the popup is re-checked and closed, attempt 1 succeeds
        page = FakePage(popup_after=1)
        res = ensure_name(page.rpc, FakeInput(page), "Brannoch Vael")
        self.assertEqual(page.text, "Brannoch Vael")
        self.assertFalse(page.popup)
        self.assertEqual(res["popups"], 1)

    def test_popup_opens_after_the_box_was_found(self):
        page = FakePage()
        inp = FakeInput(page)
        real_click = inp.click

        def click(x, y, *a, **k):   # the popup lands just before the first click on the box
            if not inp.clicks:
                page.popup = True
                inp.clicks.append((x, y))
                return
            real_click(x, y, *a, **k)
        inp.click = click
        res = ensure_name(page.rpc, inp, "Claude")
        self.assertEqual(page.text, "Claude")
        self.assertEqual(res["popups"], 1)

    def test_popup_that_never_closes_still_fails_loudly(self):
        # runs 1-2 of the naut series before dismiss_popup: no hover reaches the box -> NamingError, no hang
        page = FakePage(popup=True)
        page.on_dont_reset = lambda x, y: False
        with mock.patch("bg3ai.boxclick.click_box", lambda rpc, inp, a: {"pressed": False}), \
                mock.patch("bg3ai.boxclick.describe", lambda r: "not pressed"):
            with self.assertRaisesRegex(NamingError, "never reported the cursor"):
                ensure_name(page.rpc, FakeInput(page), "Claude")
        self.assertFalse(page.finished)

    def test_paced_switch_keeps_the_old_sleeps(self):
        sleeps = []
        page = FakePage(popup=True)
        with mock.patch.dict(naming.os.environ, {"BG3AI_CC_PACED": "1"}), \
                mock.patch.object(naming.time, "sleep", sleeps.append):
            ensure_name(page.rpc, FakeInput(page), "Claude")
        for s in (naming.PAGE_SETTLE_S, naming.POPUP_SETTLE_S, naming.FOCUS_SETTLE_S, 0.3):
            self.assertIn(s, sleeps)

    def test_state_waits_sleep_only_polls(self):
        sleeps = []
        page = FakePage(popup=True)
        with mock.patch.object(naming.time, "sleep", sleeps.append):
            ensure_name(page.rpc, FakeInput(page), "Claude")
        self.assertTrue(all(s <= 0.1 for s in sleeps), sleeps)


class ResetTutorials(unittest.TestCase):
    def setUp(self):
        naming._last_at.clear()

    def test_popup_over_the_naming_page_is_dismissed_first(self):
        page = FakePage(popup=True)
        logs = []
        name_and_finish(page.rpc, FakeInput(page), "Brannoch Vael", logs.append)
        self.assertFalse(page.popup)
        self.assertTrue(page.finished)
        self.assertEqual(page.text, "Brannoch Vael")
        self.assertTrue(any("Reset Tutorials" in m for m in logs), logs)


class ProbePoints(unittest.TestCase):
    def test_nearest_first_and_on_screen(self):
        for w, h in ((5120, 1440), (1920, 1080), (1920, 1200), (3840, 2160)):
            pts = probe_points(w, h)
            s = min(w / 3840, h / 2160)
            self.assertEqual(pts[0], (w // 2, round(h - 378 * s)))
            self.assertTrue(all(0 <= y < h and x == w // 2 for x, y in pts))
            # the band covers the box (196 design px tall) with steps smaller than it
            ys = sorted(y for _, y in pts)
            self.assertLess(max(b - a for a, b in zip(ys, ys[1:])), 196 * s)


if __name__ == "__main__":
    unittest.main()

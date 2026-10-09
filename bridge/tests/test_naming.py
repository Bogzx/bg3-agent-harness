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
                 typing_works=True, lazy_box=1):
        self.text = text
        self.name = text if readable else HANDLE
        self.readable = readable
        self.box, self.can_edit = box, can_edit
        self.focus_on_click, self.typing_works = focus_on_click, typing_works
        self.lazy_box, self.shown = lazy_box, False
        self.page, self.cursor, self.focused = 0, (0, 0), False
        self.finished = False
        self.calls: list[tuple[str, dict]] = []

    def over(self, x, y):
        return self.box[0] <= y <= self.box[1] and 1500 <= x <= 2300

    def view(self, want=None):
        has_box = self.shown and self.lazy_box <= 0
        if self.shown and self.lazy_box > 0:
            self.lazy_box -= 1
        name = self.name if self.readable else None
        text = self.text if has_box else None
        v = {"name": name, "name_handle": None if self.readable else HANDLE, "text": text,
             "can_edit": self.can_edit, "page": self.page, "on_naming_page": self.page == 3, "box": has_box,
             "focused": self.focused, "hover_box": has_box and self.page == 3 and self.over(*self.cursor),
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
    def test_already_named(self):
        page = FakePage(text="Claude", lazy_box=0)
        page.shown = True
        self.assertEqual(ensure_name(page.rpc, None, "Claude")["via"], "already")
        self.assertEqual(len(page.calls), 1)

    def test_typed_over_unreadable_handle(self):
        # The live case: the character's name is a handle, the box shows Tav; the box appears late.
        page = FakePage(lazy_box=3)
        inp = FakeInput(page)
        res = ensure_name(page.rpc, inp, "Claude")
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

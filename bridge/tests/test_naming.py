"""Offline tests: the character name is set and verified before character creation is finished
(bg3ai.naming against a fake naming page and a fake input shim; no game needed).

Run: cd bridge && uv run python -m unittest discover -s tests
"""

from __future__ import annotations

import unittest
from unittest import mock

from bg3ai import naming
from bg3ai.naming import NamingError, ensure_name, name_and_finish, probe_points


class FakePage:
    """ui.cc_name / ui.cc_finish over a naming page whose box sits at rows box_y0..box_y1 (window coords)."""

    def __init__(self, name="Tav", text_write_works=False, readable=True, box=(1150, 1190), can_edit=True,
                 focus_on_click=True, typing_works=True):
        self.name, self.text = name, name
        self.text_write_works, self.readable = text_write_works, readable
        self.box, self.can_edit = box, can_edit
        self.focus_on_click, self.typing_works = focus_on_click, typing_works
        self.page, self.cursor, self.focused = 0, (0, 0), False
        self.finished = False
        self.calls: list[tuple[str, dict]] = []

    def over(self, x, y):
        return self.box[0] <= y <= self.box[1] and 1500 <= x <= 2300

    def view(self, want=None):
        name = self.name if self.readable else None
        v = {"name": name, "text": self.text, "can_edit": self.can_edit, "page": self.page,
             "on_naming_page": self.page == 3, "box": True, "focused": self.focused,
             "hover_box": self.page == 3 and self.over(*self.cursor), "complete": True}
        if want is not None:
            v.update(want=want, confirmed=name == want, ok=self.text == want and name in (None, want))
        return v

    def rpc(self, op, args):
        self.calls.append((op, dict(args)))
        if op == "ui.cc_name":
            if args.get("page"):
                self.page = 3
            if args.get("set"):
                self.text = args["name"]
                if self.text_write_works:
                    self.name = args["name"]
            return self.view(args.get("name"))
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

    def click(self, x, y, button="L", count=1, sync=True):
        self.page.cursor = (x, y)
        self.clicks.append((x, y))
        if self.page.focus_on_click and self.page.over(x, y):
            self.page.focused = True

    def key(self, keys, sync=True):
        self.typed.append(f"<{keys}>")
        if not self.page.focused:
            return
        if keys == "LCTRL+A":
            self.selected = True
        elif keys == "BACKSPACE" and self.selected:
            self.page.text, self.selected = "", False

    def text(self, s, sync=True):
        self.typed.append(s)
        if self.page.focused:
            self.page.text += s
            if self.page.typing_works:
                self.page.name = self.page.text


@mock.patch.object(naming.time, "sleep", lambda s: None)
class EnsureName(unittest.TestCase):
    def test_already_named(self):
        page = FakePage(name="Claude")
        self.assertEqual(ensure_name(page.rpc, None, "Claude")["via"], "already")
        self.assertFalse(any(a.get("set") for _, a in page.calls))

    def test_text_write_confirmed(self):
        page = FakePage(text_write_works=True)
        self.assertEqual(ensure_name(page.rpc, None, "Claude")["via"], "text")

    def test_box_showing_the_name_is_not_enough(self):
        # The box shows "Claude" after the write but the character is still "Tav": must type it.
        page = FakePage()
        inp = FakeInput(page)
        res = ensure_name(page.rpc, inp, "Claude")
        self.assertEqual(res["via"], "typed")
        self.assertEqual(page.name, "Claude")
        self.assertEqual(inp.typed, ["<LCTRL+A>", "<BACKSPACE>", "Claude", "<RETURN>"])
        x, y = res["at"]
        self.assertTrue(page.over(x, y))
        self.assertAlmostEqual(y, sum(page.box) / 2, delta=12)  # centred on the box

    def test_unreadable_name_still_needs_typing(self):
        # Without a readable CharacterName the box text alone can't confirm the write: typed route.
        page = FakePage(readable=False)
        self.assertEqual(ensure_name(page.rpc, FakeInput(page), "Claude")["via"], "typed")

    def test_no_shim_fails_loudly(self):
        with self.assertRaises(NamingError):
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

    def test_typing_not_taken_by_character(self):
        page = FakePage(typing_works=False)
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
        page = FakePage(typing_works=False)
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

"""Offline tests for recall(): what the party has read (book panels, read_all) and its journal, searched by words
(vis task 2026-10-10: puzzle hints a player would have, without wiki spoilers)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from bg3ai import lore

BOOK = ("History Never Forgets", "First the Tower rose under the moon, then came Grief, and last the General "
        "who held the line. " + "Filler about the old order of the Selûnites. " * 30)


class Lore(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp())

    def test_kept_once_and_listed_by_title(self):
        self.assertTrue(lore.remember(self.dir, *BOOK))
        self.assertFalse(lore.remember(self.dir, *BOOK))  # same book read again
        self.assertFalse(lore.remember(self.dir, "Empty", ""))
        self.assertEqual((self.dir / lore.FILE).read_text().count("\n"), 1)
        self.assertIn("History Never Forgets", lore.search(self.dir, ""))

    def test_search_finds_the_hint_with_a_snippet_and_journal_steps(self):
        lore.remember(self.dir, *BOOK)
        lore.remember(self.dir, "Shopping List", "Bread, cheese, two candles.")
        journal = [{"title": "Find Ketheric Thorm's Relic", "steps": ["The mausoleum's paintings hide a mechanism."]}]
        out = lore.search(self.dir, "paintings grief general order", journal)
        self.assertTrue(out.startswith("READ History Never Forgets: "))  # 3 words match: ranked first
        self.assertIn("then came Grief", out)
        self.assertIn("JOURNAL Find Ketheric Thorm's Relic: The mausoleum's paintings", out)
        self.assertNotIn("Shopping", out)
        self.assertLess(len(out.splitlines()[0]), lore.SNIPPET + 60)  # a long book shows a snippet

    def test_nothing_read_or_no_match_says_what_to_do(self):
        self.assertIn("Nothing read yet", lore.search(self.dir, ""))
        lore.remember(self.dir, *BOOK)
        self.assertIn("Nothing you have read", lore.search(self.dir, "password"))


class KeepFromScreen(unittest.TestCase):
    def test_a_book_panel_on_screen_is_kept(self):
        from test_turn_macros import Base  # noqa: F401  (sets BG3AI_RUNTIME before importing server)
        from bg3ai import server
        d = Path(tempfile.mkdtemp())
        old, server.MEMORY = server.MEMORY, d
        try:
            server._keep_read({"screen": "book", "popup": {"title": "Note", "text": "Press them in order."}})
            server._keep_read({"screen": "dialogue", "popup": {"title": "X", "text": "not a book"}})
            self.assertIn("READ Note: Press them in order.", lore.search(d, "order"))
            self.assertNotIn("not a book", (d / lore.FILE).read_text())
        finally:
            server.MEMORY = old


if __name__ == "__main__":
    unittest.main()

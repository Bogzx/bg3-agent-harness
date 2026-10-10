"""Offline tests: wiki_page formatting (vis verbosity trim, S026): infobox cells joined, heading/value pairs on one
line, the Sections list deduped to two levels, other difficulties' stat blocks left out of whole-page reads.

Run: cd bridge && uv run python -m unittest discover -s tests -p 'test_wiki_format.py'
"""

from __future__ import annotations

import unittest

from bg3ai import wiki


def page(text: str, sections: list[str], section=None, truncated=False) -> dict:
    return {"title": "Dammon", "url": "u", "license": "L", "section": section, "text": text,
            "truncated": truncated, "sections": sections}


class Format(unittest.TestCase):
    def test_infobox_cells_join_and_headings_pair(self):
        text = "Ability scores\nSTR\nDEX\nCON\nINT\n10\n(0)\n\n### HP\n10\n\nDammon is a blacksmith who fled Elturel."
        out = wiki.format_page(page(text, ["1. Dammon"], section="Stats"))
        self.assertIn("STR DEX CON INT 10 (0)", out)
        self.assertIn("HP: 10", out)
        self.assertIn("Dammon is a blacksmith", out)

    def test_sections_two_levels_once(self):
        secs = ["1. Stats", "2.   Balanced Difficulty Stats", "3.     Level", "4.   Explorer Difficulty Stats",
                "5.     Level", "6. Involvement", "7.   Stats"]
        out = wiki.format_page(page("x", secs))
        self.assertIn("Sections: 1. Stats | 2. Balanced Difficulty Stats | 4. Explorer Difficulty Stats | "
                      "6. Involvement", out)
        self.assertNotIn("Level", out)

    def test_skip_other_difficulties(self):
        text = "intro\n\nBalanced\nHP 10\n\nExplorer\nHP 8\n\nInvolvement\nsells gear"
        secs = [wiki.Section(2, "Balanced Difficulty Stats", "a", text.index("Balanced")),
                wiki.Section(2, "Explorer Difficulty Stats", "b", text.index("Explorer")),
                wiki.Section(2, "Involvement", "c", text.index("Involvement"))]
        out = wiki._skip_sections(text, secs)
        self.assertNotIn("HP 8", out)
        self.assertIn("HP 10", out)
        self.assertIn("sells gear", out)


if __name__ == "__main__":
    unittest.main()

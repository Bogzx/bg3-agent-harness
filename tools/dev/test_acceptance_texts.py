"""The texts acceptance checks compare live results with, against the source that serves them (B1 smoke 2026-11-05:
V18 kept a prone text the mod no longer has, and the other-floor check a next step #102 removed).

    python3 -m unittest tools/dev/test_acceptance_texts.py
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[1] / "bridge" / "src"))
from acceptance import nav, vis  # noqa: E402

GUARD_CAST = HERE.parents[1] / "mod/Mods/BG3AI/ScriptExtender/Lua/Server/GuardCast.lua"


class V18(unittest.TestCase):
    def test_the_prone_effect_is_the_text_the_mod_serves(self):
        effect = vis.V18_WANT.split("|")[2].split("/")[0]
        self.assertIn(f'"{effect}"', GUARD_CAST.read_text())


class OtherFloor(unittest.TestCase):
    LIVE = (  # live 2026-11-05 (B1 smoke), after #102
        "travel_to (-286,-31,-847.5): REFUSED: the goal is 69m below you and 955m off and too far to preview: likely "
        "inside an interior. The way there is likely: e88 Mausoleum Entrance <exit> 17.1m NW."
    )

    def test_the_refusal_passes_on_the_entrance_it_names(self):
        self.assertTrue(nav.entrance_named(self.LIVE))

    def test_a_refusal_that_names_no_entrance_fails(self):
        self.assertFalse(nav.entrance_named("travel_to (-286,-31,-847.5): REFUSED: the goal is 69m below you."))

    def test_an_answer_that_is_no_refusal_fails(self):
        self.assertFalse(nav.entrance_named("travel_to: done, near the Mausoleum Entrance"))


if __name__ == "__main__":
    unittest.main()

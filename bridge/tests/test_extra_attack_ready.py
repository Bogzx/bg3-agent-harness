"""An order's after= states a free attack in the list's words (extra-attack-read.md (4); orchestrator D331).

In the L12 S6 rerun 7 free attacks went unused by pM's count (3 attacks per action), inferred, all after the turn's
last paid swing, with no list read in between: the swing's result said action=0 and nothing else. The mod sends the
game's free status as it reads at the result (extra_attack_ready), or, in a held swing's landing gap, cast_unlanded;
the bridge prints them as get_actions does.
"""

from __future__ import annotations

import unittest

from bg3ai import render
from bg3ai.render import Handles
from bg3ai.tools import plumbing


def after(**mod):
    res = {
        "status": "done",
        "after": {
            "in_combat": True,
            "hp": 44,
            "max_hp": 44,
            "action": 0,
            "bonus": 1,
            "reaction": 1,
            "movement": 10.5,
            "my_turn": True,
            **mod,
        },
    }
    return plumbing._trim_after(res, "use_ability", "x")


class After(unittest.TestCase):
    def test_the_free_status_reads_in_the_lists_words_beside_action(self):
        a = after(extra_attack_ready=True)["after"]
        self.assertEqual(list(a)[:4], ["hp", "max_hp", "action", "Extra Attack: free"])
        self.assertIn("action=0 Extra Attack: free bonus=1", render.compact(after(extra_attack_ready=True), Handles()))

    def test_the_landing_gap_reads_as_the_list_does(self):
        a = after(cast_unlanded=True)["after"]
        self.assertIs(a.get("last cast still resolving"), True)
        self.assertNotIn("Extra Attack: free", a)

    def test_neither_says_nothing(self):
        a = after()["after"]
        words = {"Extra Attack: free", "last cast still resolving", "extra_attack_ready", "cast_unlanded"}
        self.assertFalse(words & set(a))


if __name__ == "__main__":
    unittest.main()

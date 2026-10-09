"""Offline tests: LEVEL UP line, nudge, stored level-up plans (bg3ai.leveling) and the server wiring.

Run: cd bridge && uv run python -m unittest discover -s tests
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from bg3ai import leveling as LV

# state.status as the mod returns it now (live 2026-10-10: Gale EocLevel 1, AvailableLevel 2)
ST = {"in_combat": False, "party": [
    {"uuid": "u-claude", "name": "Claude", "level": 2},
    {"uuid": "u-sh", "name": "Shadowheart", "level": 2},
    {"uuid": "u-gale", "name": "Gale", "level": 1, "level_up": 2},
    {"uuid": "u-dead", "name": "Ghost", "level": 1, "level_up": 2, "dead": True},
]}
BUILD = """# Party build
<!-- - Example L2: {"subclass": "x"} -->
## Level-up plans
- Gale L2: {"subclass": "Evocation", "spells": ["Magic Missile", "Shield"]}
- Claude L4: {"feat": "Ability Improvement", "abilities": {"Strength": 2}}
* Shadowheart Level 3: {"spells": ["Spiritual Weapon"]}
- Broken L5: {not json}
"""


def label(u: str) -> str:
    return {"u-gale": "e3 Gale"}.get(u, u)


class Leveling(unittest.TestCase):
    def setUp(self):
        self.mem = Path(tempfile.mkdtemp(prefix="bg3lv-"))
        (self.mem / "party-build.md").write_text(BUILD)

    def test_pending(self):
        self.assertEqual(LV.pending(ST), [{"uuid": "u-gale", "name": "Gale", "to": 2}])
        self.assertEqual(LV.pending(None), [])

    def test_plans(self):
        plans = LV.load_plans(BUILD)
        self.assertEqual(plans[("gale", 2)]["subclass"], "Evocation")
        self.assertEqual(plans[("claude", 4)]["abilities"], {"Strength": 2})
        self.assertIn(("shadowheart", 3), plans)
        self.assertNotIn(("example", 2), plans)        # inside a comment
        self.assertNotIn(("broken", 5), plans)
        self.assertEqual(LV.plan_for(self.mem, "Gale", 2)["spells"], ["Magic Missile", "Shield"])
        self.assertIsNone(LV.plan_for(self.mem, "Gale", 3))
        self.assertIsNone(LV.plan_for(self.mem / "nope", "Gale", 2))

    def test_line(self):
        line = LV.line(ST, label, self.mem)
        self.assertTrue(line.startswith("LEVEL UP: e3 Gale (2) → level_up(who) now"), line)
        self.assertNotIn("no plan yet", line)
        (self.mem / "party-build.md").write_text("")
        self.assertIn("no plan yet for Gale L2", LV.line(ST, label, self.mem))
        self.assertEqual(LV.line(dict(ST, in_combat=True), label, self.mem), "")   # not in combat
        self.assertEqual(LV.line({"party": [{"uuid": "a", "name": "A", "level": 3}]}, label), "")

    def test_nudge(self):
        n = LV.Nudge(every_s=300)
        n.observe(ST)
        self.assertIn("LEVEL UP pending: Gale (2)", n.text(now=1000))
        self.assertEqual(n.text(now=1100), "")                  # at most every 5 min
        self.assertIn("Gale", n.text(now=1301))
        n.text(now=1400, shown=True)                            # get_status printed it: timer restarts
        self.assertEqual(n.text(now=1600), "")
        n.observe(dict(ST, in_combat=True))
        self.assertEqual(n.text(now=5000), "")                  # never in combat
        n.observe(dict(ST, in_dialogue=True))
        self.assertEqual(n.text(now=5000), "")
        n.observe(ST)
        n.done("u-gale")
        self.assertEqual(n.text(now=9000), "")


class ServerWiring(unittest.TestCase):
    """level_up() with no plan sends the stored plan; get_status shows the LEVEL UP line."""

    def test_level_up_uses_stored_plan(self):
        from bg3ai import server as S

        mem = Path(tempfile.mkdtemp(prefix="bg3lv-"))
        (mem / "party-build.md").write_text(BUILD)
        sent = {}

        class G:
            def try_rpc(self, op, args=None, context="server", default=None):
                return ST if op == "state.status" else default

        def fake_act(tool, op, args, wait_s=25.0, context="server", wait=True, note=""):
            sent.update(op=op, args=args, note=note)
            return "level_up: done"

        saved = (S.game, S._act, S.MEMORY, S._who)
        S.game, S._act, S.MEMORY, S._who = G(), fake_act, mem, (lambda ref: "u-gale")
        try:
            S._LEVEL_NUDGE.observe(ST)
            out = S.level_up("Gale")
            self.assertEqual(out, "level_up: done")
            self.assertEqual(sent["op"], "ui.level_up")
            self.assertEqual(sent["args"], {"uuid": "u-gale", "plan": {"subclass": "Evocation",
                                                                       "spells": ["Magic Missile", "Shield"]}})
            self.assertIn("Gale L2 from memory/party-build.md", sent["note"])
            self.assertEqual(S._LEVEL_NUDGE.rows, [])            # done: no more nudging for Gale
            S.level_up("Gale", plan='{"subclass": "Divination"}')  # an explicit plan wins
            self.assertEqual(sent["args"]["plan"], {"subclass": "Divination"})
            self.assertTrue(S._level_up_line(ST).startswith("LEVEL UP: u-gale (2) → level_up(who)"))
        finally:
            S.game, S._act, S.MEMORY, S._who = saved


if __name__ == "__main__":
    unittest.main()

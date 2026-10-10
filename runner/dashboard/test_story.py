"""Offline tests for the dashboard Story panel parsers (story.py)."""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import story as ST  # noqa: E402

DECISIONS = """# Decisions and consequences

<!--
Format:
## D-07 Exposed Kagha's letter at the Grove (S013)
- Choice: example in a comment, not a card
-->

## Decisions

## D-01 Freed and recruited Shadowheart (S002)
- Choice: opened her pod with the Eldritch Rune
- Why: a cleric healer fills the party's biggest gap
- Consequence: she joined [seen S002]
- Expected later: she may leave over Shar [wiki]
- Reversible: no

## D-09 Killed the three goblin leaders; sided with the Grove (S017-S018)
- Choice: killed Priestess Gut (S017)
- Choice: killed Dror Ragzlin and Minthara (S018)
  instead of raiding the Grove
- Why: Devotion oath

## Archive
"""

STATUS = """<!-- generated -->
# Snapshot: status (2026-10-10 08:50, S025)

WHERE Shadow-Cursed Lands | exploring | gold 1093
PARTY Gale Wizard 5 (Evocation) HP 64/64 [Blessing of Selune]
PARTY * Claude Paladin 5 (Oath of Devotion) HP 52/88
"""

INVENTORY = """# Snapshot: inventory (2026-10-10 08:50, S025)

## Gale
- Scroll of Revivify usable
- Quarterstaff [equipped MeleeMainHand]

## Claude
- Moonlantern (rare) story usable [equipped Amulet]
- Ceremonial Warhammer (uncommon)
- Mysterious Artefact story usable
- Sword of Justice (very rare) [equipped MeleeMainHand]
- Spoon

## Wyll
- Blade of Frontiers [equipped MeleeMainHand]
"""

PLAYTHROUGH = """# Playthrough: run

Created: 2026-10-09 · Character: Claude
Now: ACT 2 · Region: Last Light Inn
Main thread: Cure the tadpole

## Read when
- Starting a session: HANDOFF.md

## Party
- Claude: Paladin

## Standing commitments
- Swore to Nettie
"""


class StoryParse(unittest.TestCase):
    def test_decisions(self):
        cards = ST.parse_decisions(DECISIONS)
        self.assertEqual([c["id"] for c in cards], ["D-01", "D-09"])  # the commented example is not a card
        d1 = cards[0]
        self.assertEqual(d1["title"], "Freed and recruited Shadowheart")
        self.assertEqual(d1["session"], "S002")
        self.assertEqual(d1["consequences"], [{"text": "she joined", "tag": "seen S002"}])
        self.assertEqual(d1["expected"][0]["tag"], "wiki")
        self.assertEqual(d1["reversible"], "no")
        d9 = cards[1]
        self.assertEqual(d9["session"], "S017-S018")
        self.assertEqual(d9["choice"][1], "killed Dror Ragzlin and Minthara (S018) instead of raiding the Grove")

    def test_status_and_inventory(self):
        st = ST.parse_status(STATUS)
        self.assertEqual((st["where"], st["mode"], st["gold"], st["when"]),
                         ("Shadow-Cursed Lands", "exploring", 1093, "2026-10-10 08:50, S025"))
        self.assertEqual(st["party"][1], {"name": "Claude", "active": True, "class": "Paladin 5 (Oath of Devotion)",
                                          "hp": 52, "max_hp": 88, "statuses": []})
        inv = ST.parse_inventory(INVENTORY)
        c = inv["Claude"]
        self.assertEqual([i["slot"] for i in c["equipped"]], ["Amulet", "MeleeMainHand"])
        self.assertEqual(c["equipped"][0]["name"], "Moonlantern")
        self.assertEqual([i["name"] for i in c["notable"]], ["Ceremonial Warhammer", "Mysterious Artefact"])
        self.assertEqual(inv["Gale"]["notable"], [])  # plain scrolls are not notable

    def test_playthrough(self):
        p = ST.parse_playthrough(PLAYTHROUGH)
        self.assertEqual(p["now"], "ACT 2 · Region: Last Light Inn")
        self.assertEqual(p["main_thread"], "Cure the tadpole")
        self.assertEqual(p["header"], ["Created: 2026-10-09 · Character: Claude"])  # not the "Read when" list
        self.assertEqual(p["party"], ["Claude: Paladin"])
        self.assertEqual(p["commitments"], ["Swore to Nettie"])

    def test_payload_and_cache(self):
        with tempfile.TemporaryDirectory() as d:
            mem = Path(d)
            (mem / "snapshots").mkdir()
            (mem / "chapters").mkdir()
            (mem / "decisions.md").write_text(DECISIONS)
            (mem / "PLAYTHROUGH.md").write_text(PLAYTHROUGH)
            (mem / "snapshots" / "status.md").write_text(STATUS)
            (mem / "snapshots" / "inventory.md").write_text(INVENTORY)
            (mem / "chapters" / "C01_S001-S010.md").write_text(
                "# Chapter 1\n## Story so far\n- S001: woke up\n## Lessons\n- kill the leader first\n")
            p = ST.story_payload(mem)
            self.assertEqual(len(p["decisions"]), 2)
            self.assertEqual(p["chapters"][0]["title"], "Chapter 1")
            self.assertEqual(p["chapters"][0]["sections"][1], {"heading": "Lessons", "items": ["kill the leader first"]})
            claude = next(m for m in p["status"]["party"] if m["name"] == "Claude")
            self.assertEqual(len(claude["equipped"]), 2)
            self.assertEqual([o["name"] for o in p["others"]], ["Wyll"])  # in camp: inventory only
            self.assertIs(ST.story_payload(mem), p)  # unchanged files: cached
            (mem / "decisions.md").write_text(DECISIONS.replace("## Archive", "## D-10 Next (S020)\n- Choice: x\n"))
            self.assertEqual(len(ST.story_payload(mem)["decisions"]), 3)

    def test_missing_memory(self):
        p = ST.story_payload(Path("/nonexistent/memory"))
        self.assertFalse(p["exists"])
        self.assertEqual((p["decisions"], p["chapters"], p["status"]["party"]), ([], [], []))


if __name__ == "__main__":
    unittest.main()

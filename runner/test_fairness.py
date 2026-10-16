"""Offline tests for the per-session fairness audit (runner/fairness.py) and its triage section."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fairness as F  # noqa: E402
import triage  # noqa: E402


def call(name: str, text: str, t: float = 1791600000.0) -> dict:
    return {"name": name, "text": text, "t": t}


class Rules(unittest.TestCase):
    def rules(self, *calls) -> dict:
        return {k: len(v) for k, v in F.audit_calls(list(calls)).items()}

    def test_clean_results_pass(self):
        self.assertEqual(self.rules(
            call("look", "e4 Claude moved 3m N\n  + e743 Plaque <usable> 19.9m\nEVENTS\n  status_applied name=Moonshield target={name=Gale e2}"),
            call("combat_turn", "Bless (Target_Bless) [slot L1+A] 9m L1 | Fireball (Projectile_Fireball) [slot L3+A] 18m"),
            call("roll_check", "roll_check: done — roll={result=18 dc=18 context=DialogCheck skill=Persuasion}"),
            call("get_dialogue", "ROLL check=Charisma Check dc=6 context=DialogCheck skill=Persuasion phase=intro"),
        ), {})

    def test_memory_wiki_and_screenshots_are_not_game_results(self):
        self.assertEqual(self.rules(
            call("Read", "Persuasion DC 15 [seen S007]; S_GOB_DrowCommander"),
            call("wiki_page", "UUID | cc51df85-792b-4d9a-8ec5-eff598720e5e | DC 14 Sleight of Hand"),
            call("screenshot", "[image source: /home/x/f8de6674-4be1-425e-b117-41602ff7ed5b/tool-results/a.jpg]"),
        ), {})

    def test_leaks(self):
        got = self.rules(
            call("look_at", "hp=11 statuses=[name=Magic Allergy id=GREMISHKA_MAGICALLERGY_AURA] relation=hostile"),
            call("wait_for", "status_applied name=Undead Ward status=MAG_MYRKULITES_UNDEAD_PRESENCE_BUFF target={e9 name=Ghoul}"),
            call("get_nearby", "e12 Chest <container> 3m (lock DC 15)"),
            call("get_status", "LESSONS (yours, for this fight)\n- WHEN Aelis, Lower City Sewers: failed Deception DC18 on Aelis started a fight [seen]"),
            call("look", "e7 cc51df85-792b-4d9a-8ec5-eff598720e5e Goblin"),
            call("wait_for", "roll_result roller={e1 name=Karlach} subject=e55 success"),
            call("get_nearby", "e30 S_GOB_DrowCommander_abc <character> 8m"),
        )
        self.assertEqual(got.get("status_id"), 2)
        self.assertEqual(got.get("dc_outside_roll"), 1)
        self.assertEqual(got.get("guid"), 1)
        self.assertEqual(got.get("roll_subject"), 1)
        self.assertEqual(got.get("template"), 1)

    def test_checks(self):
        got = self.rules(
            call("get_map", "level=SCL_Main_A waypoints=[id=WAYP_CHA_Chapel name=Overgrown Ruins]"),
            call("get_nearby", "e44 Meazel <character> 12m invisible"),
        )
        self.assertEqual(got.get("judged_id"), 2)  # a LEAK since #8 (get_map shows names)
        self.assertEqual(F.RULES["judged_id"][0], F.LEAK)
        self.assertEqual(got.get("hidden_state"), 1)
        self.assertNotIn("template", got)

    def test_invisibility_status_by_name_is_not_hidden_state(self):
        # the old event form and the bridge's compacted ones (compact.py EVENT_LINES / CHANGES)
        got = self.rules(
            call("wait_for", "  status_applied name=Invisibility target={e2 name=Tav} invisible"),
            call("wait_for", "  status_applied e2: Invisibility (invisible)"),
            call("combat_turn", "  status_applied e2 Tav: Invisibility, invisible"),
            call("look", "  e2 Tav +Invisibility invisible"),
        )
        self.assertNotIn("hidden_state", got)
        got = self.rules(call("get_nearby", "e44 Meazel <character> 12m invisible +Burning"))
        self.assertEqual(got.get("hidden_state"), 1)


    def test_compacted_combat_forms(self):
        # compact.py COMBAT_NARRATIVE / KIND_GROUP / ACTION_DUPS lines: no hidden-state or id hits of their own
        got = self.rules(
            call("combat_turn", "  turn e86 Commander Zhalk: Main Hand Attack (attack) -> e67 12 Slashing + 3 Fire"),
            call("combat_turn", "  cast e44: Invisibility -> e2 0\n  + neutral dead: e30 Imp 4.6m, e33 Imp 15.3m"),
            call("combat_turn", "action: Shout_Dash: done — statuses=[e2 Dash]\nend_turn: done (still to act: e2)"),
        )
        self.assertEqual(got, {})

    @unittest.skipUnless((Path(__file__).resolve().parents[1] / "bridge/tests/golden/naut_results.jsonl.gz").exists(),
                         "golden naut results missing")
    def test_golden_compacted_same_audit(self):
        """The naut results compacted as the bridge does give the same audit as the raw ones."""
        import gzip
        repo = Path(__file__).resolve().parents[1]
        sys.path.insert(0, str(repo / "bridge/src"))
        from bg3ai import compact
        by: dict[str, list] = {}
        with gzip.open(repo / "bridge/tests/golden/naut_results.jsonl.gz", "rt", encoding="utf-8") as f:
            for line in f:
                r = json.loads(line)
                by.setdefault(r["session"], []).append(r)
        for sess, rows in by.items():
            c = compact.Compactor(clock=lambda: 0.0)
            raw = [call(r["tool"], r["text"]) for r in rows]
            comp = [call(r["tool"], c.apply(r["tool"], r["text"])) for r in rows]
            self.assertEqual(self.rules(*raw), self.rules(*comp), sess)


class TriageSection(unittest.TestCase):
    def test_section_in_triage_report(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d) / "S099"
            evs = [
                {"type": "assistant", "message": {"content": [
                    {"type": "tool_use", "id": "u1", "name": "mcp__bg3__look_at", "input": {"target": "e5"}}]}},
                {"type": "user", "message": {"content": [
                    {"type": "tool_result", "tool_use_id": "u1",
                     "content": [{"type": "text", "text": json.dumps(
                         {"result": "statuses=[name=Moonshield id=SCL_MOONSHIELD]"})}]}]}},
            ]
            Path(f"{base}.jsonl").write_text("\n".join(json.dumps(e) for e in evs) + "\n")
            md = triage.build(base)
        self.assertIn("## Fairness audit: 1 leak(s), 0 to check", md)
        self.assertIn("LEAK status_id", md)
        self.assertIn("SCL_MOONSHIELD", md)

    def test_clean_section(self):
        self.assertIn("Nothing a player couldn't see", "\n".join(F.section([call("look", "e4 Claude moved 3m N")])))


if __name__ == "__main__":
    unittest.main()

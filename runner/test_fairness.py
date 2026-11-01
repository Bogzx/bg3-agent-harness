"""Offline tests for the per-session fairness audit (bg3harness.fairness) and its triage section."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # runs.py: bg3harness.events reads raw streams with it
sys.path.insert(1, str(Path(__file__).resolve().parents[1] / "packages" / "harness" / "src"))
sys.path.insert(1, str(Path(__file__).resolve().parents[1] / "bridge" / "src"))
from bg3harness import fairness as F
from bg3harness import triage


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

    def test_pickpocket_window_dc_is_fair(self):
        """Pickpocket.xaml shows the selected item's DC under "Target" (S070: 4 false hits on '60% DC9')."""
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bridge/src"))
        from bg3ai import economy

        def h(it: dict) -> str:
            return it.get("h", "")

        pockets = economy.pockets_text({"items": [
            {"h": "e659", "name": "Wyrm's Rock Prison Key", "chance": 60, "dc": 9, "story": True},
            {"name": "Gold", "count": 12, "chance": 60, "dc": 9}]}, h)
        self.assertIn("60% DC9", pockets)
        got = self.rules(
            call("pickpocket", pockets),
            call("do_actions", "1. pickpocket: " + pockets),
            call("pickpocket", economy.steal_text(
                {"item": {"name": "Ring"}, "success": True, "rolled": 14, "dc": 9, "chance": 60}, h)),
            call("pickpocket", economy.steal_text(
                {"item": {"name": "Ring"}, "success": False, "rolled": 3, "dc": 18, "chance": 20}, h)),
            call("pickpocket", "pickpocket: failed — Ring: 20% chance (DC 18) is below min_chance 70; nothing taken"),
        )
        self.assertEqual(got, {})
        # a DC on any other line of the same result is still a leak
        got = self.rules(call("pickpocket", pockets + "\n  + e12 Chest <container> 3m (lock DC 15)"))
        self.assertEqual(got.get("dc_outside_roll"), 1)

    def test_checks(self):
        got = self.rules(
            call("get_map", "level=SCL_Main_A waypoints=[id=WAYP_CHA_Chapel name=Overgrown Ruins]"),
            call("get_nearby", "e44 Meazel <character> 12m invisible"),
        )
        self.assertEqual(got.get("judged_id"), 2)  # a LEAK since #8 (get_map shows names)
        self.assertEqual(F.RULES["judged_id"][0], F.LEAK)
        self.assertEqual(got.get("hidden_state"), 1)
        self.assertNotIn("template", got)

    def test_audit_c1_classes(self):
        # the side channels, verdicts and engine truths of the 2026-10-18 audit (C8-C11, C13, C18), as they read
        got = self.rules(
            call("get_combat_state", "COMBAT round=2 unseen_combatants turn_group=[e1] current=e1"),
            call("get_status", "WHERE Grand Mausoleum | exploring\nALSO geo_gen=15"),
            call("look", "  e992 Painting <usable> 3m [glowing (solved)]"),
            call("wait_for", "  item_effect item={e993 Painting} state=flashing (wrong order: resets)"),
            call("interact", "interact: failed — clicked it: still locked (no lock to pick: the game opens it some other way)"),
            call("combat_turn", "action: use_ability e303: skipped (e303 Mind Flayer is gone (it died))"),
            call("wait_for", "  quest_update text=%%% EMPTY title=%%% EMPTY"),
        )
        self.assertEqual({k: got.get(k) for k in ("side_channel", "verdict", "engine_gone", "unlocalised")},
                         {"side_channel": 2, "verdict": 3, "engine_gone": 1, "unlocalised": 2})
        # what the harness says now passes
        self.assertEqual(self.rules(
            call("look", "  e992 Painting <usable> 3m [glowing]"),
            call("interact", "interact: failed — clicked it: still locked"),
            call("combat_turn", "skipped (e303 Mind Flayer is gone (it is no longer in the turn order))"),
        ), {})

    def test_runner_prompts_are_audited(self):
        with tempfile.TemporaryDirectory() as d:
            log = Path(d) / "S041.log"
            log.write_text("===== S041 2026-10-10 claude session (new)\n[prompt] This is session S041. ⏎  ⏎ RUNNER "
                           "DIRECTIVE, CHANGE APPROACH: ... level=SCL_Main_A, region=Shadow-Cursed Lands\n"
                           "12:00:00 [tool] look\n")
            calls = F.prompt_calls(log)
        self.assertEqual([c["name"] for c in calls], [F.PROMPT])
        self.assertEqual({k: len(v) for k, v in F.audit_calls(calls).items()}, {"judged_id": 1})
        self.assertEqual(F.prompt_calls(Path("/nonexistent/S000.log")), [])

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

    def test_the_partys_own_state_is_not_hidden_state(self):
        # live S3 windmill (pi): Tav's own Shout_Hide, the actor's after={..} portrait (bridge _trim_after), verbatim
        got = self.rules(
            call("use_ability", "use_ability: done — cast=Shout_Hide statuses=[e4 Hiding] after={hp=7/25 action=0 "
                                "bonus=1 reaction=1 movement=0.5 my_turn slots=1:2 sneaking @(37.3,35.2,429.6)}\n"
                                "  status_applied e4: Hiding"),
            call("end_turn", "end_turn e4 Tav: done — ended after={hp=5/25 action=0 bonus=1 reaction=1 movement=9 "
                             "slots=1:1 sneaking @(55.3,33.9,436.8)}\n  status_removed e4: Hiding"),
            call("use_ability", "use_ability: done — cast=Projectile_SneakAttack no_damage target=e81 "
                                "after={sneaking @(-431.7,5.5,-173.7)}"),
            # get_status's PARTY rows (render.state_text) and the same members on any later line
            call("get_status", "WHERE Windmill | exploring\nPARTY *e4 Tav Rogue2 HP 5/25 sneaking\n"
                               "PARTY e2 Shadowheart Cleric2 HP 12/12 sneaking"),
            call("get_nearby", "e2 Shadowheart <character> 3m sneaking"),
        )
        self.assertNotIn("hidden_state", got)
        # an enemy's hidden state still counts: on its own line, next to the actor's after={..}, after a party handle
        got = self.rules(
            call("get_status", "PARTY *e4 Tav Rogue2 HP 5/25"),
            call("get_nearby", "e44 Meazel <character> 12m sneaking"),
            call("use_ability", "use_ability: done — target=e44 (hidden) after={sneaking @(1,2,3)}"),
            call("look", "e4 Tav sees e45 Goblin ambushing 9m"),
        )
        self.assertEqual(got.get("hidden_state"), 3)

    # launch-v1 get_character results, verbatim (cut after the statuses' field): deepseek act3_gold_max r3 S001 20:22:41
    # (its handle mid-sheet) and haiku crash_site r3 S001-c5 21:53:38 (its handle after the equipment's)
    SHADOWHEART = ("Cleric7(Trickery Domain) statuses=[id=SPEAK_WITH_DEAD_RECAST name=Recast Speak with Dead] "
                   "name=Shadowheart race=High Half-Elf e4 features=[Absolute's Mercy,Absolute's Aegis] "
                   "equipment={Amulet=e76 Absolute's Talisman VanityBoots=e72 Acolyte's Sandals}")
    ASTARION = ("level=2 statuses=[name=Difficult Terrain: Deep Water id=DIFFICULT_TERRAIN_DEEPWATER turns=1; "
                "name=Wet id=WET turns=1] initiative=3 name=Astarion ac=14 equipment={Underwear=e7 Astarion's "
                "Underwear VanityBoots=e8 Tasteful Boots} darkvision_m=2 e62 max_hp=17 hp=17")

    def test_a_party_members_sheet_shows_its_own_status_ids(self):
        got = self.rules(
            call("get_status", "PARTY e2 Wyll Warlock7(The Fiend) HP 52/52+9\n"
                               "PARTY e4 Shadowheart Cleric7(Trickery Domain) HP 52/52 [Recast Speak with Dead]\n"
                               "PARTY *e5 Tav Paladin7(Oath of Devotion) HP 67/67"),
            call("get_character", self.SHADOWHEART),
            call("get_status", "PARTY *e62 Astarion Rogue2 HP 17/17 [Difficult Terrain: Deep Water,Wet]"),
            call("get_character", self.ASTARION),
        )
        self.assertNotIn("status_id", got)
        self.assertNotIn("template", got)

    def test_an_examined_characters_status_ids_still_leak(self):
        # the same sheets with their handles outside the party (no PARTY row names them): an examined character
        got = self.rules(
            call("get_status", "PARTY *e5 Tav Paladin7(Oath of Devotion) HP 67/67"),
            call("get_character", self.SHADOWHEART),
            call("get_character", self.ASTARION),
        )
        self.assertEqual(got.get("status_id"), 2)  # SPEAK_WITH_DEAD_RECAST, DIFFICULT_TERRAIN_DEEPWATER


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

"""Offline tests: action lists show names only; use_ability / combat_turn take the shown name.

Run: cd bridge/tests && uv run python -m unittest test_ability_names
"""

from __future__ import annotations

import json

from test_turn_macros import GOB, NOT_JOBS, P1, Base, combat  # noqa: F401  (sets BG3AI_RUNTIME first)

from bg3ai import render, server
from bg3ai.game import GameError

ACTS = {"resources": {"ActionPoint": [1, 1]}, "actions": [
    {"spell": "Target_MainHandAttack", "name": "Attack"},
    {"spell": "Projectile_FireBolt", "name": "Fire Bolt", "range": 18},
    {"spell": "Target_Smite_A", "name": "Smite"}, {"spell": "Target_Smite_B", "name": "Smite"}]}


class Names(Base):
    def setUp(self):
        super().setUp()
        server._ability_ids.clear()
        self.g.actions = json.loads(json.dumps(ACTS))

    def test_list_drops_ids_except_shared_names(self):
        out = render.render_actions(ACTS, server.H)
        self.assertIn("Fire Bolt 18m", out)
        self.assertNotIn("Projectile_FireBolt", out)
        self.assertIn("Smite (Target_Smite_A)", out)

    def test_extra_attack_shows_free(self):
        out = render.render_actions({"actions": [{"spell": "Target_MainHandAttack", "name": "Attack", "range": 1.5,
                                                  "cost": {"action": 1}, "extra_attack": True}]}, server.H)
        self.assertIn("Attack [Extra Attack: free] 1.5m", out)

    def test_resolution(self):
        self.assertEqual(server._ability_id(P1, "fire bolt"), "Projectile_FireBolt")
        self.assertEqual(server._ability_id(P1, "Fire Bolt (Projectile_FireBolt)"), "Projectile_FireBolt")
        self.assertEqual(server._ability_id(P1, "Shout_Dash"), "Shout_Dash")  # ids pass through
        self.assertEqual(len(self.g.ops("state.actions")), 1)  # one read, then cached
        with self.assertRaisesRegex(GameError, "2 actions called 'Smite'"):
            server._ability_id(P1, "Smite")
        with self.assertRaisesRegex(GameError, "no action called 'Fireball'"):
            server._ability_id(P1, "Fireball")

    # S029: "Rage" in Karlach's plan; a Berserker's rage is shown as 'Frenzy' (Shout_Rage_Frenzy).
    def test_name_that_starts_one_id(self):
        self.g.actions["actions"] += [{"spell": "Shout_Rage_Frenzy", "name": "Frenzy"},
                                      {"spell": "Shout_EndRage", "name": "End Rage"}]
        self.assertEqual(server._ability_id(P1, "Rage"), "Shout_Rage_Frenzy")
        self.assertEqual(server._ability_id(P1, "Fire"), "Projectile_FireBolt")  # its words: fire, bolt
        with self.assertRaisesRegex(GameError, "no action called 'Bolt'"):
            server._ability_id(P1, "Bolt")  # must start the id's words
        with self.assertRaisesRegex(GameError, "no action called 'Fireball'"):
            server._ability_id(P1, "Fireball")

    def test_use_ability_by_name(self):
        self.g.combat = combat()
        server.use_ability("Fire Bolt", target=self.gob)
        self.assertEqual([a["spell"] for _, a in self.g.ops("act.cast")], ["Projectile_FireBolt"])

    def test_combat_turn_plan_by_name(self):
        self.g.combat = combat()
        self.g.watch["combat"] = True
        self.g.on_start = lambda op, a: op == "act.end_turn" and self.g.combat.update(current=GOB)
        server.combat_turn(json.dumps({"action": {"ability": "Attack", "target": self.gob}}))
        self.assertEqual([a["spell"] for _, a in self.g.ops("act.cast")], ["Target_MainHandAttack"])


SUMMON = {"resources": {"BonusActionPoint": [1, 1]}, "actions": [
    {"spell": "Target_MainHandAttack_SpiritualWeapon_Greataxe", "name": "Greataxe Slash", "range": 1.5},
    {"spell": "Target_Slash_New", "name": "Lacerate", "range": 1.5},
    {"spell": "Shout_DismissSelf", "name": "Dismiss Summon"}]}


class OwnAttack(Base):
    """S028: the Spiritual Weapon lost two rounds to guessed attack ids it doesn't have."""
    def setUp(self):
        super().setUp()
        server._ability_ids.clear()
        self.g.actions = json.loads(json.dumps(SUMMON))

    def tearDown(self):
        server._ability_ids.clear()  # module cache: the summon's list must not leak into other tests
        super().tearDown()

    def test_guessed_attack_maps_to_its_own(self):
        for guess in ("Target_MainHandAttack", "Target_SpiritualWeapon_Attack", "attack"):
            sp, note = server._own_attack(P1, guess)
            self.assertEqual(sp, "Target_MainHandAttack_SpiritualWeapon_Greataxe", guess)
            self.assertIn("'greataxe slash'", note)
        self.assertEqual(len(self.g.ops("state.actions")), 1)

    def test_owned_or_unrelated_ids_unchanged(self):
        self.assertEqual(server._own_attack(P1, "Target_MainHandAttack_SpiritualWeapon_Greataxe"),
                         ("Target_MainHandAttack_SpiritualWeapon_Greataxe", ""))
        self.assertEqual(server._own_attack(P1, "Projectile_FireBolt"), ("Projectile_FireBolt", ""))
        self.assertEqual(server._own_attack(P1, "Projectile_MainHandAttack"), ("Projectile_MainHandAttack", ""))

    def test_normal_character_keeps_its_attack(self):
        self.g.actions = json.loads(json.dumps(ACTS))
        self.assertEqual(server._own_attack(P1, "Target_MainHandAttack"), ("Target_MainHandAttack", ""))

    def test_combat_turn_uses_the_summons_attack(self):
        self.g.combat = combat()
        self.g.watch["combat"] = True
        self.g.on_start = lambda op, a: op == "act.end_turn" and self.g.combat.update(current=GOB)
        out = server.combat_turn(json.dumps({"action": [{"ability": "Target_MainHandAttack", "target": self.gob}]}))
        self.assertEqual([a["spell"] for _, a in self.g.ops("act.cast")],
                         ["Target_MainHandAttack_SpiritualWeapon_Greataxe"])
        self.assertIn("its own weapon attack 'greataxe slash'", out)

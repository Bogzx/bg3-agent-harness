"""Offline tests for debrief.py: the fight debrief built from the events a player saw (research/fight-debrief.md)."""
import json
import tempfile
import unittest
from pathlib import Path

from bg3ai import debrief

C, SH, W = "u-claude", "u-sh", "u-wyll"
AV, SO, TR = "u-av", "u-so", "u-tr"
PARTY = {C, SH, W}
ROWS = [{"uuid": C, "name": "Claude", "hp": 13, "max_hp": 104}, {"uuid": SH, "name": "Shadowheart", "hp": 0, "max_hp": 90},
        {"uuid": W, "name": "Wyll", "hp": 40, "max_hp": 76}]


def ref(u, n):
    return {"uuid": u, "name": n}


def ev(kind, **data):
    return {"kind": kind, "data": data}


def dmg(src, sn, tgt, tn, amt, typ):
    return ev("damage", source=ref(src, sn), target=ref(tgt, tn), amount=amt, type=typ)


def cast(u, n, name):
    return ev("cast", caster=ref(u, n), spell="x", name=name)


def fight_events():
    """In the mod's order: a hit's damage events, then its cast (Events.lua AttackedBy before CastedSpell)."""
    return [
        ev("combat_started"), ev("round_started", round=1),
        dmg(SH, "Shadowheart", AV, "Justiciar Avenger", 20, "Radiant"), cast(SH, "Shadowheart", "Sacred Flame"),
        dmg(W, "Wyll", AV, "Justiciar Avenger", 0, ""), dmg(W, "Wyll", AV, "Justiciar Avenger", 12, "Force"),
        cast(W, "Wyll", "Eldritch Blast"),
        dmg(SH, "Shadowheart", TR, "Umbral Tremor", 2, "Necrotic"),
        dmg(SH, "Shadowheart", AV, "Justiciar Avenger", 9, "Necrotic"), cast(SH, "Shadowheart", "Spirit Guardians"),
        cast(SO, "Justiciar Soulhunter", "Living Shadow"),
        ev("status_applied", target=ref(W, "Wyll"), source=ref(SO, "Justiciar Soulhunter"), name="Blinded"),
        dmg(SO, "Justiciar Soulhunter", W, "Wyll", 30, "Piercing"),
        ev("round_started", round=2),
        dmg(SH, "Shadowheart", TR, "Umbral Tremor", 2, "Necrotic"),   # Spirit Guardians at its turn start (no cast)
        dmg(SH, "Shadowheart", TR, "Umbral Tremor", 2, "Necrotic"),   # and again (3 Tremor hits vs 9 on the Avenger)
        dmg(SO, "Justiciar Soulhunter", W, "Wyll", 46, "Piercing"), ev("downed", **ref(W, "Wyll")),
        dmg(AV, "Justiciar Avenger", C, "Claude", 20, "Slashing"),
        ev("used", char=ref(C, "Claude"), item=ref("u-pot", "Potion of Healing"), success=True),
        dmg(C, "Claude", TR, "Umbral Tremor", 10, "Bludgeoning"), dmg(C, "Claude", TR, "Umbral Tremor", 11, "Bludgeoning"),
        cast(C, "Claude", "Main Hand Attack"),
        ev("died", **ref(AV, "Justiciar Avenger")),
        ev("combat_ended"),
    ]


def cs_rows():
    return {"round": 2, "order": [
        {"uuid": C, "name": "Claude", "relation": "party", "pos": [0, 0, 0]},
        {"uuid": SH, "name": "Shadowheart", "relation": "party", "pos": [3, 0, 0]},
        {"uuid": W, "name": "Wyll", "relation": "party", "pos": [6, 0, 0]},
        {"uuid": AV, "name": "Justiciar Avenger", "relation": "hostile", "race": "Human", "pos": [1, 3, 0],
         "from": {C: [1.0, 1]}},
        {"uuid": SO, "name": "Justiciar Soulhunter", "relation": "hostile", "pos": [0, 3, 1], "from": {C: [1.2, 1]}},
        {"uuid": TR, "name": "Umbral Tremor", "relation": "hostile", "pos": [1, 3, 1], "from": {C: [2.0, 1]}},
        {"uuid": "u-skel", "name": "Reconstituted Duellist", "relation": "neutral", "pos": [9, 0, 0]}]}


class Build(unittest.TestCase):
    def run_fight(self, tr=None):
        tr = tr or debrief.Tracker()
        evs = fight_events()
        w1, ended1 = tr.feed(evs[:12], PARTY, now=100.0)
        tr.observe(cs_rows(), PARTY)
        w2, ended = tr.feed(evs[12:], PARTY, now=160.0)
        self.assertFalse(ended1)
        self.assertTrue(ended)
        rec = tr.close(ROWS, ["Scroll of Fireball"], {"session": "S033", "region": "SCL"}, now=220.0)
        return tr, rec, w1 + w2

    def test_record(self):
        _, rec, _ = self.run_fight()
        self.assertEqual(rec["grade"], "costly")
        self.assertEqual(rec["rounds"], 2)
        names = {e["name"]: e for e in rec["enemies"]}
        self.assertEqual(set(names), {"Justiciar Avenger", "Justiciar Soulhunter", "Umbral Tremor"})  # not the neutral
        self.assertEqual(names["Justiciar Avenger"]["killed"], 1)
        self.assertEqual(names["Justiciar Avenger"]["race"], "Human")
        self.assertEqual(names["Justiciar Soulhunter"]["abilities"], ["Living Shadow"])
        self.assertEqual(names["Justiciar Soulhunter"]["statuses_on_us"], ["Blinded"])
        self.assertEqual(names["Justiciar Soulhunter"]["dmg_to_us"], 76)
        self.assertEqual(rec["downed"], [{"name": "Wyll", "round": 2, "by": "Justiciar Soulhunter"}])
        eb = next(a for a in rec["abilities"] if a["name"] == "Eldritch Blast")
        self.assertEqual((eb["hits"], eb["misses"], eb["dmg"]), (1, 1, 12))
        sg = next(a for a in rec["abilities"] if a["name"] == "Spirit Guardians")
        self.assertEqual((sg["targets_max"], sg["hits"], sg["dmg"]), (2, 4, 15))
        low = [t for t in rec["dmg_types"] if t["low"]]
        self.assertEqual([(t["enemy"], t["type"]) for t in low], [("Umbral Tremor", "Necrotic")])
        # V18b: the same attack (Spirit Guardians) did 9 to the Avenger: 3 compared hits, avg 2.0 vs 9.0
        self.assertEqual({k: low[0][k] for k in ("compared", "got", "expected")},
                         {"compared": 3, "got": 2.0, "expected": 9.0})
        self.assertEqual(rec["pos"]["surrounded"], {"name": "Claude", "n": 3, "round": 1})
        self.assertEqual(rec["pos"]["elev_m"], 3.0)
        self.assertEqual(rec["position"], ["surrounded", "enemy_high_ground"])
        self.assertEqual(rec["group"], "justiciar avenger|justiciar soulhunter|umbral tremor")
        self.assertEqual(names["Umbral Tremor"]["resisted"], ["Necrotic"])
        self.assertEqual(names["Justiciar Soulhunter"]["n"], 1)
        self.assertEqual(rec["unused"], ["Scroll of Fireball"])
        self.assertEqual(rec["consumables"], {"used": ["Potion of Healing"], "unused": ["Scroll of Fireball"]})
        party = {p["name"]: p for p in rec["party"]}
        self.assertEqual(party["Shadowheart"]["dealt"], 35)
        sf = next(a for a in rec["abilities"] if a["name"] == "Sacred Flame")
        self.assertEqual((sf["hits"], sf["dmg"]), (1, 20))
        self.assertEqual(party["Wyll"]["taken"], 76)
        self.assertEqual(rec["key_facts"][0], "Wyll went down first (r2, by Justiciar Soulhunter)")
        json.dumps(rec)  # serialisable

    def test_render_and_lines(self):
        _, rec, _ = self.run_fight()
        txt = debrief.render(rec)
        self.assertTrue(txt.startswith("FIGHT DEBRIEF (costly, 2 rounds, 2m00s) vs "))
        self.assertLessEqual(len(txt.splitlines()), 8)
        self.assertIn("downed: Wyll r2 by Justiciar Soulhunter", txt)
        self.assertIn("low damage: Necrotic vs Umbral Tremor", txt)
        self.assertIn("carried, unused: Scroll of Fireball", txt)
        la = debrief.last_attempt(rec)
        self.assertTrue(la.startswith("LAST ATTEMPT: costly vs "))
        self.assertIn("protect Wyll", la)
        self.assertIn("focus Justiciar Soulhunter first", la)
        # vis V7: a scroll works in combat in BG3 (an action, like the spell); V6's "out of combat only" was wrong
        self.assertIn("use the unused scroll/potion (in combat a scroll costs its spell's action)", la)
        self.assertNotIn("out of combat", la)
        rec["consumables"]["unused"] = ["Potion of Healing"]
        self.assertIn("use the unused scroll/potion;", debrief.last_attempt(rec) + ";")
        note = debrief.fight_note(rec)
        self.assertTrue(note.startswith("costly @SCL vs Justiciar Soulhunter, Justiciar Avenger (Human), Umbral Tremor | "), note)
        self.assertIn("| rounds 2 | downed: Wyll by Justiciar Soulhunter | dealt 68 / taken 96 |", note)
        self.assertIn("| resisted: Umbral Tremor:Necrotic | unused: Scroll of Fireball | Wyll went down first", note)

    def test_saves_summons_and_mirror_names(self):
        # S035 Self-Same Trial: mirror copies named like the party, Toll the Dead saved (0 Necrotic) x3, the
        # Spiritual Weapon's hits arrive with Shadowheart as the source; the copies vanish (no died events).
        tr = debrief.Tracker()
        SW, MW, MS = "u-sw", "u-mirror-wyll", "u-mirror-sh"
        evs = [dmg(SH, "Shadowheart", MS, "Shadowheart", 0, "Necrotic"),
               ev("cast", caster=ref(SH, "Shadowheart"), spell="Target_TollTheDead", name="Toll the Dead", roll="save"),
               dmg(SH, "Shadowheart", MW, "Wyll", 0, "Necrotic"),
               ev("cast", caster=ref(SH, "Shadowheart"), spell="Target_TollTheDead", name="Toll the Dead", roll="save"),
               dmg(SH, "Shadowheart", MW, "Wyll", 11, "Slashing"),
               ev("cast", caster=ref(SW, "Spiritual Weapon"), spell="x", name="Greataxe Slash", roll="attack"),
               dmg(MW, "Wyll", C, "Claude", 9, "Force"),
               dmg(W, "Wyll", MW, "Wyll", 10, "Force"), cast(W, "Wyll", "Eldritch Blast"),
               ev("combat_ended")]
        tr.feed([ev("combat_started"), ev("round_started", round=1)], PARTY | {SW}, now=0.0)
        tr.observe({"order": [{"uuid": SW, "name": "Spiritual Weapon", "relation": "party", "summon": True},
                              {"uuid": MW, "name": "Wyll", "relation": "hostile"},
                              {"uuid": MS, "name": "Shadowheart", "relation": "hostile"}]}, PARTY | {SW})
        tr.feed(evs, PARTY | {SW}, now=0.0)
        rec = tr.close(ROWS, [], {}, now=60.0)
        self.assertFalse([t for t in rec["dmg_types"] if t["low"]])            # saves are not a resisted type
        self.assertNotIn("Necrotic", [t["type"] for t in rec["dmg_types"]])
        ab = {a["name"]: a for a in rec["abilities"]}
        self.assertEqual((ab["Toll the Dead"]["hits"], ab["Toll the Dead"]["saves"]), (0, 2))
        self.assertEqual((ab["Greataxe Slash"]["hits"], ab["Greataxe Slash"]["dmg"]), (1, 11))   # the summon's hit
        self.assertEqual(ab["Eldritch Blast"]["hits"], 1)
        ours = {o["ability"]: o for o in rec["ours"]}
        self.assertEqual(ours["Toll the Dead"]["saves"], 2)
        self.assertIsNone(ours["Eldritch Blast"]["saves"])
        party = {p["name"]: p for p in rec["party"]}
        self.assertEqual(party["Shadowheart"]["dealt"], 11)                    # summon damage stays with the owner
        self.assertEqual({e["name"] for e in rec["enemies"]}, {"Wyll (foe)", "Shadowheart (foe)"})
        txt = debrief.render(rec)
        self.assertIn("Toll the Dead x2: 0 hit/2 saved", txt)
        self.assertIn("killed 0/2 (2 gone at the end)", txt)
        self.assertIn("most damage to us: Wyll (foe)", " ".join(rec["key_facts"]))

    def test_grades(self):
        tr = debrief.Tracker()
        tr.feed([ev("combat_started"), dmg(C, "Claude", AV, "Justiciar Avenger", 9, "Slashing"), ev("combat_ended")],
                PARTY)
        self.assertEqual(tr.close(ROWS)["grade"], "clean")
        tr.feed([ev("combat_started")] + [ev("downed", **ref(u, n)) for u, n in ((C, "Claude"), (SH, "SH"), (W, "W"))],
                PARTY)
        self.assertEqual(tr.close(ROWS)["grade"], "wipe")
        tr.feed([ev("combat_started"), ev("downed", **ref(C, "Claude")), ev("downed", **ref(SH, "SH"))], PARTY)
        rec = tr.close(ROWS, reloaded=True)
        self.assertEqual((rec["grade"], rec["grade_before"]), ("reloaded", "near_wipe"))

    def test_ally_death_is_no_clean_fight(self):
        # S043: Jaheira (allied NPC on the turn order) died and the debrief said "clean"; S045 Aylin the same.
        J, NEC = "u-jaheira", "u-nec"
        tr = debrief.Tracker()
        tr.feed([ev("combat_started"), ev("round_started", round=1)], PARTY, now=10.0)
        tr.observe({"order": [{"uuid": C, "name": "Claude", "relation": "party"},
                              {"uuid": J, "name": "Jaheira", "relation": "ally"},
                              {"uuid": "u-sw", "name": "Spiritual Weapon", "relation": "ally", "summon": True},
                              {"uuid": NEC, "name": "Necromite", "relation": "hostile"}]}, PARTY)
        tr.feed([ev("round_started", round=3), ev("died", **ref(J, "Jaheira")), ev("died", **ref(NEC, "Necromite")),
                 ev("died", **ref("u-sw", "Spiritual Weapon")), ev("combat_ended")], PARTY, now=20.0)
        rec = tr.close(ROWS)
        self.assertEqual(rec["grade"], "near_wipe")
        self.assertEqual(rec["allies_died"], [{"name": "Jaheira", "round": 3}])
        self.assertEqual(sum(e["killed"] for e in rec["enemies"]), 1)       # Jaheira is no kill
        self.assertIn("ally DIED: Jaheira (r3)", rec["key_facts"][0])
        out = debrief.render(rec)
        self.assertIn("DIED: Jaheira (ally, r3) — revive now: Scroll of Revivify", out.splitlines()[1])
        self.assertIn("keep Jaheira alive", debrief.last_attempt(rec))
        self.assertEqual([a["name"] for a in tr.dead_allies], ["Jaheira"])   # for the ALERTS line
        tr.feed([ev("revived", **ref(J, "Jaheira"))], PARTY)                  # revived outside a fight
        self.assertEqual(tr.dead_allies, [])

    def test_party_death_is_named_first(self):
        tr = debrief.Tracker()
        tr.feed([ev("combat_started"), ev("downed", **ref(SH, "Shadowheart")), ev("died", **ref(SH, "Shadowheart")),
                 ev("combat_ended")], PARTY)
        rec = tr.close(ROWS)
        self.assertEqual(rec["key_facts"][0], "DIED: Shadowheart (r1)")
        self.assertIn("DIED: Shadowheart (r1) — revive now", debrief.render(rec).splitlines()[1])

    def test_retry_window(self):
        tr, rec, _ = self.run_fight()
        self.assertIs(tr.retry_of(now=220.0 + 300), rec)
        self.assertIsNone(tr.retry_of(now=220.0 + debrief.RETRY_WINDOW_S + 1))

    def test_joined_mid_fight(self):
        tr = debrief.Tracker()
        tr.feed([ev("round_started", round=4), dmg(SO, "Justiciar Soulhunter", C, "Claude", 5, "Piercing")], PARTY)
        rec = tr.close(ROWS)
        self.assertEqual(rec["joined_round"], 4)
        self.assertEqual(rec["rounds"], 4)

    def test_a_wipe_ends_the_fight(self):
        # The Game Over screen comes instead of combat_ended: the fight ends when every member is down at once.
        tr = debrief.Tracker()
        tr.observe({"order": [{"uuid": "u-sw", "name": "Spiritual Weapon", "relation": "party", "summon": True}]},
                   PARTY | {"u-sw"})
        _, ended = tr.feed([ev("combat_started"), dmg(AV, "Justiciar Avenger", C, "Claude", 30, "Slashing"),
                            ev("downed", **ref(C, "Claude")), ev("downed", **ref(SH, "Shadowheart"))],
                           PARTY | {"u-sw"})
        self.assertFalse(ended)
        _, ended = tr.feed([ev("downed", **ref(W, "Wyll"))], PARTY | {"u-sw"})   # the summon still stands
        self.assertTrue(ended)
        rec = tr.close(ROWS)
        self.assertEqual((rec["grade"], rec["end"]), ("wipe", "wipe"))

    def test_why_the_fight_closed(self):
        tr, rec, _ = self.run_fight()
        self.assertEqual(rec["end"], "combat_ended")
        tr.feed([ev("combat_started"), dmg(AV, "Justiciar Avenger", C, "Claude", 5, "Slashing")], PARTY)
        self.assertEqual(tr.close(ROWS, reloaded=True)["end"], "reloaded")
        tr.feed([ev("combat_started"), dmg(AV, "Justiciar Avenger", C, "Claude", 5, "Slashing")], PARTY)
        self.assertEqual(tr.close(ROWS, end="session_end")["end"], "session_end")

    def test_hp_at_the_end(self):
        _, rec, _ = self.run_fight()
        party = {p["name"]: p for p in rec["party"]}
        self.assertEqual((party["Claude"]["hp"], party["Claude"]["max_hp"]), (13, 104))
        self.assertEqual((party["Shadowheart"]["hp"], party["Shadowheart"]["max_hp"]), (0, 90))

    def test_friendly_fire(self):
        J = "u-jaheira"
        tr = debrief.Tracker()
        tr.feed([ev("combat_started"), ev("round_started", round=1)], PARTY)
        tr.observe({"order": [{"uuid": J, "name": "Jaheira", "relation": "ally"}]}, PARTY)
        tr.feed([
            # a Fireball: one hit on Wyll in two types, then its cast; a made save (0) is no hit
            dmg(SH, "Shadowheart", W, "Wyll", 14, "Fire"), dmg(SH, "Shadowheart", W, "Wyll", 3, "Radiant"),
            dmg(SH, "Shadowheart", C, "Claude", 0, "Fire"),
            dmg(SH, "Shadowheart", AV, "Justiciar Avenger", 17, "Fire"),
            cast(SH, "Shadowheart", "Fireball"),
            dmg(C, "Claude", J, "Jaheira", 8, "Slashing"), cast(C, "Claude", "Cleave"),
            ev("round_started", round=2),
            dmg(W, "Wyll", W, "Wyll", 4, "Fire"),   # no cast before or after it: no ability
            ev("turn_started", **ref(AV, "Justiciar Avenger")),
            ev("combat_ended"),
        ], PARTY)
        rec = tr.close(ROWS)
        self.assertEqual(rec["friendly"], [
            {"caster": "Shadowheart", "target": "Wyll", "dmg": 17, "types": ["Fire", "Radiant"], "round": 1,
             "ability": "Fireball", "ally": False},
            {"caster": "Claude", "target": "Jaheira", "dmg": 8, "types": ["Slashing"], "round": 1,
             "ability": "Cleave", "ally": True},
            {"caster": "Wyll", "target": "Wyll", "dmg": 4, "types": ["Fire"], "round": 2, "ability": None,
             "ally": False},
        ])
        party = {p["name"]: p for p in rec["party"]}
        self.assertEqual(party["Wyll"]["taken"], 21)   # counted as taken, as before
        tr.feed([ev("combat_started"), dmg(AV, "Justiciar Avenger", C, "Claude", 5, "Slashing"), ev("combat_ended")],
                PARTY)
        self.assertEqual(tr.close(ROWS)["friendly"], [])


class Warnings(unittest.TestCase):
    def test_repeated_patterns_warn_once(self):
        _, past, _ = Build().run_fight()
        tr = debrief.Tracker([past])
        start = [ev("combat_started"), ev("round_started", round=1)]
        self.assertEqual(tr.feed(start, PARTY)[0], [])
        tr.observe(cs_rows(), PARTY)
        first = tr.first_turn_warnings(cs_rows())
        self.assertEqual(len(first), 1)
        self.assertIn("LAST TIME vs", first[0])
        self.assertIn("Wyll went down first", first[0])
        w, _ = tr.feed([dmg(SH, "Shadowheart", TR, "Umbral Tremor", 1, "Necrotic"),
                        dmg(SH, "Shadowheart", TR, "Umbral Tremor", 1, "Necrotic")], PARTY)
        self.assertEqual(len(w), 1)
        self.assertIn("WARNING: Necrotic did little to Umbral Tremor last time too", w[0])
        w, _ = tr.feed([dmg(SO, "Justiciar Soulhunter", W, "Wyll", 40, "Piercing"), ev("downed", **ref(W, "Wyll"))],
                       PARTY)
        self.assertEqual(len(w), 1)
        self.assertIn("WARNING: Wyll went down first again", w[0])

    def test_no_warning_against_other_enemies(self):
        _, past, _ = Build().run_fight()
        tr = debrief.Tracker([past])
        tr.feed([ev("combat_started")], PARTY)
        w, _ = tr.feed([dmg(SH, "Shadowheart", "u-gob", "Goblin", 1, "Necrotic")], PARTY)
        self.assertEqual(w, [])
        self.assertEqual(tr.first_turn_warnings({"order": [{"name": "Goblin", "relation": "hostile"}]}), [])


class Store(unittest.TestCase):
    def test_append_and_load(self):
        _, rec, _ = Build().run_fight()
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "fights.jsonl"
            debrief.append(p, rec)
            debrief.append(p, rec)
            with p.open("a") as fh:
                fh.write("not json\n")
            got = debrief.load_history(p)
        self.assertEqual(len(got), 2)
        self.assertEqual(got[0]["summary"], rec["summary"])


if __name__ == "__main__":
    unittest.main()


GOLDEN = Path(__file__).parent / "golden" / "fights_claude_run_2.jsonl"   # real v1 records: S061 09:37/09:53/09:56,
K, GT, NU, OR, RE = "u-karlach", "u-gortash", "u-numia", "u-orin", "u-reaper"   # S065 11:43/11:50 (vis V6)
PARTY4 = PARTY | {K}
ROWS4 = ROWS + [{"uuid": K, "name": "Karlach", "hp": 0, "max_hp": 190}]


def hit(src, sn, tgt, tn, *parts):
    """One hit as the mod sends it: one damage event per type, back to back (Events.lua AttackedBy)."""
    return [dmg(src, sn, tgt, tn, a, t) for t, a in parts]


def unseen(tgt, tn, amt, typ):
    return ev("damage", source={"name": "Someone"}, target=ref(tgt, tn), amount=amt, type=typ)


class Act3Retries(unittest.TestCase):
    """vis V6: the Act 3 boss retries (claude-run-2 S059-S065) got wrong or empty facts. Shapes from the
    transcripts' combat_turn damage lists and the stored records."""

    def test_s060_gortash_rider_is_no_resistance_and_traps_are_the_top_damage(self):
        # S060-r1 09:15: every weapon hit carried 1-3 Radiant (Slashing 12 + Radiant 2); Divine Smite Piercing 8 +
        # Radiant 18, Guiding Bolt Radiant 18. The trap grid hit for 434 of 666 with no visible source.
        tr = debrief.Tracker()
        evs = [ev("combat_started"), ev("round_started", round=1)]
        evs += hit(K, "Karlach", GT, "Gortash", ("Slashing", 12), ("Radiant", 2))
        evs += hit(K, "Karlach", GT, "Gortash", ("Slashing", 11), ("Radiant", 2))   # extra attack: next hit
        evs += [cast(K, "Karlach", "Main Hand Attack")]
        evs += hit(C, "Claude", GT, "Gortash", ("Piercing", 8), ("Radiant", 18)) + [cast(C, "Claude", "Divine Smite")]
        evs += hit(SH, "Shadowheart", GT, "Gortash", ("Radiant", 18)) + [cast(SH, "Shadowheart", "Guiding Bolt")]
        evs += hit(K, "Karlach", GT, "Gortash", ("Slashing", 13), ("Radiant", 2)) + [cast(K, "Karlach", "Frenzied Strike")]
        # the down comes before the cast that did it (the mod's order): the cast names it
        evs += [ev("round_started", round=2), dmg(NU, "Black Gauntlet Numia", SH, "Shadowheart", 40, "Necrotic"),
                ev("downed", **ref(SH, "Shadowheart")), cast(NU, "Black Gauntlet Numia", "Blight")]
        evs += [unseen(u, n, 38, "Thunder") for u, n in ((C, "Claude"), (K, "Karlach"), (W, "Wyll"))]
        evs += [unseen(u, n, 36, "Fire") for u, n in ((C, "Claude"), (K, "Karlach"))]
        tr.feed(evs, PARTY4 | {"u-sw"}, now=0.0)
        tr.feed([ev("downed", **ref("u-sw", "Spiritual Weapon"))], PARTY4 | {"u-sw"}, now=10.0)
        rec = tr.close(ROWS4, ["Scroll of Revivify"], {"session": "S060"}, reloaded=True, now=60.0)
        self.assertEqual(rec["v"], debrief.V)
        self.assertFalse([t for t in rec["dmg_types"] if t["low"]], rec["dmg_types"])   # old code: Radiant (avg 2)
        self.assertNotIn("did little", " ".join(rec["key_facts"]))
        self.assertEqual(rec["unseen"]["dmg"], 186)
        self.assertEqual(rec["key_facts"][0], "Shadowheart went down first (r2, by Black Gauntlet Numia: Blight)")
        self.assertTrue(rec["key_facts"][1].startswith("most damage to us: no visible source 186 of 226 (Thunder 3 hits"
                                                       " avg 38.0, Fire 2 hits avg 36.0)"), rec["key_facts"])
        self.assertEqual([d["name"] for d in rec["downed"]], ["Shadowheart"])           # not our Spiritual Weapon
        self.assertNotIn("dmg_cap", rec)
        la = debrief.last_attempt(rec)
        self.assertIn("find what hits you unseen", la)
        self.assertNotIn("drink", la)                                                    # a scroll only
        self.assertNotIn("switch damage types", la)
        self.assertIn("(from no visible source: 186)", debrief.render(rec))

    def test_s065_orin_unstoppable_is_a_per_hit_cap_with_the_kill_and_the_heal(self):
        # S065 11:50: Unstoppable: 7 on Orin: each damage part of a hit did 1 (Radiant 9x1, Necrotic 3x1, Piercing 1 +
        # Radiant 1; 18 of our 31 hits), the rest after the stacks were gone. Orin's Slay killed Karlach in r6; Orin
        # got to 21 % and healed back to 33 %; Insect Plague killed 2 Reapers in r1.
        tr = debrief.Tracker()
        evs = [ev("combat_started"), ev("round_started", round=1),
               ev("status_applied", target=ref(OR, "Orin"), name="Slayer Form"),
               ev("status_applied", target=ref(OR, "Orin"), name="Unstoppable: 7"),
               ev("died", **ref(RE, "Reaper of Bhaal Eglantine")), ev("died", **ref("u-r2", "Reaper of Bhaal Hiila"))]
        for _ in range(3):
            evs += hit(C, "Claude", OR, "Orin", ("Piercing", 1), ("Radiant", 1)) + [cast(C, "Claude", "Main Hand Attack")]
            evs += hit(SH, "Shadowheart", OR, "Orin", ("Radiant", 1)) + [cast(SH, "Shadowheart", "Sacred Flame")]
            evs += hit(SH, "Shadowheart", OR, "Orin", ("Necrotic", 1)) + [cast(SH, "Shadowheart", "Toll the Dead")]
        evs += hit(W, "Wyll", OR, "Orin", ("Force", 4)) + [cast(W, "Wyll", "Eldritch Blast")]
        evs += [ev("round_started", round=6), dmg(OR, "Orin", K, "Karlach", 60, "Slashing"),
                ev("downed", **ref(K, "Karlach")), cast(OR, "Orin", "Slay"), dmg(OR, "Orin", K, "Karlach", 30, "Slashing"),
                ev("died", **ref(K, "Karlach")), cast(OR, "Orin", "Slay")]
        tr.feed(evs[:6], PARTY4, now=0.0)
        for pct in (100, 21, 33):
            tr.observe({"order": [{"uuid": OR, "name": "Orin", "relation": "hostile", "hp_pct": pct},
                                  {"uuid": RE, "name": "Reaper of Bhaal Eglantine", "relation": "hostile", "dead": True},
                                  {"uuid": "u-r2", "name": "Reaper of Bhaal Hiila", "relation": "hostile", "dead": True}]},
                       PARTY4)
        tr.feed(evs[6:], PARTY4, now=30.0)
        rec = tr.close(ROWS4, [], {"session": "S065"}, reloaded=True, now=530.0)
        self.assertFalse([t for t in rec["dmg_types"] if t["low"]], rec["dmg_types"])   # old code: Radiant + Necrotic
        self.assertEqual(rec["dmg_cap"][0]["enemy"], "Orin")
        self.assertEqual(rec["dmg_cap"][0]["statuses"], ["Slayer Form", "Unstoppable"])
        self.assertEqual(rec["deaths"], [{"name": "Karlach", "round": 6, "by": "Orin", "ability": "Slay"}])
        self.assertEqual(rec["kills"], [{"name": "Reaper of Bhaal Eglantine", "round": 1},
                                        {"name": "Reaper of Bhaal Hiila", "round": 1}])
        self.assertEqual(rec["key_facts"], [
            "DIED: Karlach (r6, by Orin: Slay)",                    # its first down that round: no 2nd fact
            "most damage to us: Orin (Slay) 90, left at 33% (was down to 21%)",
            "9 of 10 hits on Orin did 1 per damage type (any type; on it: Slayer Form, Unstoppable)"])
        la = debrief.last_attempt(rec)
        self.assertIn("Orin often took 1 per hit: many small hits, or end what shields it first", la)
        self.assertNotIn("switch damage types", la)
        self.assertNotIn("focus Orin first", la)
        txt = debrief.render(rec)
        self.assertIn("killed 2/", txt)
        self.assertIn("(Reaper of Bhaal Eglantine r1, Reaper of Bhaal Hiila r1)", txt)
        self.assertIn("left: Orin 33% (lowest 21%)", txt)
        self.assertLessEqual(len(txt.splitlines()), 8)

    def test_s065_orin_try_1_hits_are_a_shield_too(self):
        # S065 11:43, our own hits on Orin (combat_turn damage lists): 5 of 11 did 1, the median was 4
        tr = debrief.Tracker()
        evs = [ev("combat_started"), ev("round_started", round=1)]
        for amt, typ in ((1, "Radiant"), (1, "Force"), (1, "Force"), (1, "Piercing"), (1, "Radiant"), (4, "Force"),
                         (5, "Slashing"), (6, "Force"), (8, "Slashing"), (8, "Piercing"), (17, "Slashing")):
            evs += hit(C, "Claude", OR, "Orin", (typ, amt)) + [cast(C, "Claude", "Main Hand Attack")]
        tr.feed(evs, PARTY4)
        rec = tr.close(ROWS4)
        self.assertEqual((rec["dmg_cap"][0]["ones"], rec["dmg_cap"][0]["hits"]), (5, 11))
        self.assertFalse([t for t in rec["dmg_types"] if t["low"]])                     # old code: Radiant

    def test_stored_v1_records_lose_their_rider_lows(self):
        # S066 = Orin try 3 starts from S065's records: their "Radiant did little to Orin" must not come back as a
        # LAST TIME fact or a mid-fight "use another damage type" WARNING.
        hist = [json.loads(x) for x in GOLDEN.read_text().splitlines()]
        self.assertTrue(any("did little" in f for r in hist for f in r["key_facts"]))      # the v1 facts
        tr = debrief.Tracker(hist)
        cs = {"order": [{"name": "Orin", "relation": "hostile"}, {"name": "Reaper of Bhaal Dhoria", "relation": "hostile"}]}
        tr.feed([ev("combat_started"), ev("round_started", round=1)], PARTY4)
        line = tr.first_turn_warnings(cs)[0]
        self.assertTrue(line.startswith("LAST TIME vs Orin, Reaper of Bhaal Dhoria (reloaded, was near_wipe, S065 11:50): "
                                        "DIED: Karlach; Wyll went down first (r2, by Orin); most damage to us: Orin "
                                        "(Slay, Multiattack) 587"), line)
        self.assertNotIn("did little", line)
        w, _ = tr.feed(hit(SH, "Shadowheart", OR, "Orin", ("Radiant", 1)) * 3, PARTY4)
        self.assertEqual(w, [])
        # its YOUR LESSON lines count from try 1 (11:43), not from 11:50 (a v1 record has no chain_t)
        self.assertEqual(tr.chain_start(tr.last_time_rec(cs)), "2026-10-11T11:43:16")
        self.assertEqual(tr.chain_start(tr.history[0]), "2026-10-11T09:37:57")   # S061: nothing before it here

    def test_s062_last_time_cites_the_lost_attempt_not_the_lure_practice(self):
        # S062 10:00 (the win) was told the 09:56 lure practice (2 rounds, reloaded while clean: "enemies held high
        # ground; unused: …"), not 09:37's wipe (DIED: Wyll, Claude).
        hist = [json.loads(x) for x in GOLDEN.read_text().splitlines()][:3]
        tr = debrief.Tracker(hist)
        cs = {"order": [{"name": "Lord Enver Gortash", "relation": "hostile"},
                        {"name": "Black Gauntlet Ulova", "relation": "hostile"}]}
        line = tr.first_turn_warnings(cs)[0]
        self.assertIn("(reloaded, was wipe, S061 09:37): DIED: Wyll, Claude; Wyll went down first (r6, by Gortash)", line)
        self.assertIs(tr.informative({"Lord Enver Gortash"}), tr.history[0])
        self.assertIs(tr.informative({"Black Gauntlet Ulova"}, before=tr.history[0]), None)   # nothing older

    def test_empty_fight_is_not_recorded(self):
        # S059: round events with no enemy, damage or down closed as "[fight] clean … vs unseen enemies" x7
        tr, past, _ = Build().run_fight()
        tr.feed([ev("round_started", round=15), ev("combat_ended")], PARTY)
        self.assertIsNone(tr.close(ROWS))
        self.assertIs(tr.last, past)


S066 = Path(__file__).parent / "golden" / "fights_s066.jsonl"   # real v2 records: Orin tries 3-7 (S066 19:12-19:30)


class ThinAttempts(unittest.TestCase):
    """vis V9: S066's LAST ATTEMPT said `focus Orin first` x9. It came from the solo 19:12 try (Claude pulled into
    the arena, the others dealt/took 0, no hit on Orin) and the 1-round tests at 19:25/19:27/19:30, although 19:17
    had measured Orin's per-hit cap (11 of 14 hits did 1)."""

    def setUp(self):
        self.old = [debrief._trust(json.loads(x)) for x in GOLDEN.read_text().splitlines()]
        self.s066 = [debrief._trust(json.loads(x)) for x in S066.read_text().splitlines()]

    def test_which_attempts_are_thin(self):
        self.assertEqual([debrief.thin(r) for r in self.s066],
                         ["Claude fought alone", None, "it lasted 1 round", "it lasted 1 round", "it lasted 1 round"])

    def test_a_1_round_test_keeps_the_cap_the_chain_measured(self):
        for i in (2, 3, 4):
            la = debrief.last_attempt(self.s066[i], self.old + self.s066[:i + 1])
            self.assertNotIn("focus Orin first", la, i)
            self.assertIn("Orin often took 1 per hit", la, i)
            self.assertIn("it lasted 1 round, so it measured little; the fuller attempt vs them (reloaded, was costly, "
                          "S066 19:17): Wyll went down first", la, i)
            self.assertIn("protect Wyll", la, i)   # the fuller attempt's first down, not this test's
        self.assertIn("focus Orin first", debrief.last_attempt(self.s066[2]))   # (HEAD: no history to read)

    def test_the_solo_try_plans_from_the_last_full_fight(self):
        # 19:14: only S065's v1 records existed (no cap measurable in them), so the tip stays `focus Orin first`,
        # but from S065 11:50's fight of four, said as such
        la = debrief.last_attempt(self.s066[0], self.old + self.s066[:1])
        self.assertIn("Claude fought alone, so it measured little; the fuller attempt vs them (reloaded, was near_wipe, "
                      "S065 11:50): DIED: Karlach", la)
        self.assertIn("protect Wyll", la)
        # once any attempt measured the cap it holds for the solo try too
        la = debrief.last_attempt(self.s066[0], self.old + self.s066)
        self.assertNotIn("focus Orin first", la)
        self.assertIn("Orin often took 1 per hit", la)

    def test_a_full_attempt_reads_as_before(self):
        la = debrief.last_attempt(self.s066[1], self.old + self.s066[:2])
        self.assertNotIn("measured little", la)
        self.assertIn("Orin often took 1 per hit", la)


S069 = Path(__file__).parent / "golden" / "fights_s069.jsonl"   # real v2 record: S069 20:25 Sorcerous Sundries escape


class FledFights(unittest.TestCase):
    """vis V10: S069 20:25 the shop's guards turned hostile, all four Dashed for 3 rounds toward the door and the
    agent reloaded mid-escape; the LAST ATTEMPT said `focus Water Elemental first` for a fight nobody fought."""

    def setUp(self):
        self.rec = json.loads(S069.read_text().splitlines()[0])
        # as server.py does after reading the unused consumables
        self.rec["key_facts"] = debrief.key_facts(self.rec)
        self.rec["summary"] = debrief.summary(self.rec)

    def test_the_s069_escape_reads_as_a_flight(self):
        self.assertTrue(debrief.fled(self.rec))
        self.assertEqual(self.rec["key_facts"], [
            "the party fled (Dash only, 3 rounds, dealt 0, killed 0/11)",
            "hit hardest while fleeing: Water Elemental (Slam, Multiattack) 115",
            "Claude surrounded by 3 (r1)", "took most: Karlach 139"])
        self.assertTrue(self.rec["summary"].startswith("reloaded (was costly) after fleeing vs Animated Armour x4"))
        self.assertTrue(debrief.render(self.rec).startswith("FIGHT DEBRIEF (reloaded, fled, 3 rounds, 3m46s) vs "))

    def test_its_last_attempt_has_no_kill_order(self):
        la = debrief.last_attempt(self.rec, [self.rec])
        self.assertNotIn("focus", la)
        self.assertIn("an escape, not a fight: plan the way out or avoid it: avoid it: don't repeat what started it", la)
        self.assertIn("leave by a way out of Water Elemental's reach", la)
        self.assertIn("Disengage first when in melee", la)
        self.assertIn("an unused scroll/potion may cover the escape (in combat a scroll costs its spell's action)", la)

    def test_a_thin_attempt_does_not_plan_from_a_flight(self):
        test = dict(self.rec, rounds=1, abilities=[{"caster": "Claude", "name": "Main Hand Attack", "casts": 1,
                                                    "hits": 1, "misses": 0, "saves": 0, "roll": "attack",
                                                    "targets_max": 1, "dmg": 9}],
                    party=[dict(p, dealt=9 if p["name"] == "Claude" else 0) for p in self.rec["party"]])
        self.assertFalse(debrief.fled(test))
        self.assertEqual(debrief.thin(test), "it lasted 1 round")
        self.assertIsNone(debrief.fuller(test, [self.rec, test]))

    def test_a_tracked_flight(self):
        tr = debrief.Tracker()
        tr.feed([ev("combat_started"), ev("round_started", round=1), cast(C, "Claude", "Dash"),
                 dmg(SO, "Justiciar Soulhunter", C, "Claude", 40, "Piercing"), ev("round_started", round=2),
                 cast(W, "Wyll", "Cunning Action: Disengage"), cast(C, "Claude", "Dash"),
                 dmg(SO, "Justiciar Soulhunter", W, "Wyll", 12, "Piercing")], PARTY, now=100.0)
        rec = tr.close(ROWS, [], {"session": "S999"}, reloaded=True, now=160.0)
        self.assertTrue(debrief.fled(rec))
        self.assertEqual(rec["key_facts"][0], "the party fled (Cunning Action: Disengage + Dash only, 2 rounds, "
                                              "dealt 0, killed 0/1)")
        self.assertNotIn("focus", debrief.last_attempt(rec))

    def test_a_fight_with_a_dash_in_it_is_not_a_flight(self):
        tr = debrief.Tracker()
        evs = fight_events()
        tr.feed(evs[:2] + [cast(C, "Claude", "Dash")] + evs[2:], PARTY, now=100.0)
        rec = tr.close(ROWS, [], {"session": "S999"}, now=160.0)
        self.assertFalse(debrief.fled(rec))
        self.assertIn("focus Justiciar Soulhunter first", debrief.last_attempt(rec))


S074_REC = Path(__file__).parent / "golden" / "fights_s074.jsonl"   # the real v2 record of Cazador try 1 (S074 23:04)
G, CZ, GH, GH2 = "u-gale", "u-cazador", "u-gur-1", "u-gur-2"
SPAWN = {"u-leon": "Leon", "u-petras": "Pale Petras", "u-aurelia": "Aurelia", "u-dalyria": "Dalyria",
         "u-violet": "Violet", "u-yousen": "Yousen"}
PARTY_G = PARTY | {K, G}
ROWS_G = ROWS4 + [{"uuid": G, "name": "Gale", "hp": 11, "max_hp": 158}]


class S074Cazador(unittest.TestCase):
    """vis V16: Cazador try 1 (S074 23:04-23:10). The debrief booked Gale's Scorching Ray (49) and Magic Missile under
    Shadowheart's heals, counted the six ritual victims as enemies and two of them as kills (`killed 3/27`), and read
    one 3-damage Psychic hit as "switch damage types" (plus a WARNING in try 2). Shapes from the transcript."""

    def cazador_rows(self):
        rows = [{"uuid": CZ, "name": "Cazador Szarr", "relation": "hostile", "race": "Vampire", "hp_pct": 100},
                {"uuid": GH, "name": "Fallen Gur Hunter", "relation": "hostile"},
                {"uuid": GH2, "name": "Fallen Gur Hunter", "relation": "hostile"}]
        rows += [{"uuid": u, "name": n, "relation": "hostile", "race": "Vampire Spawn", "hp_pct": 100}
                 for u, n in SPAWN.items()]   # [Sacrificial Lamb, Temporarily Hostile]
        return {"order": rows}

    def spawn_turns(self):
        return [ev("turn_started", **ref(u, n)) for u, n in SPAWN.items()]

    def fight(self):
        tr = debrief.Tracker()
        tr.feed([ev("combat_started"), ev("round_started", round=1)], PARTY_G, now=0.0)
        tr.observe(self.cazador_rows(), PARTY_G)
        evs = [ev("turn_started", **ref(CZ, "Cazador Szarr")),
               dmg(CZ, "Cazador Szarr", G, "Gale", 31, "Lightning"), dmg(CZ, "Cazador Szarr", G, "Gale", 47, "Necrotic"),
               cast(CZ, "Cazador Szarr", "Call Lightning"),
               ev("turn_started", **ref(SH, "Shadowheart")), cast(SH, "Shadowheart", "Healing Word"),
               ev("turn_started", **ref(G, "Gale"))]
        evs += hit(G, "Gale", GH, "Fallen Gur Hunter", ("Force", 10)) + [ev("died", **ref(GH, "Fallen Gur Hunter"))]
        evs += [dmg(G, "Gale", CZ, "Cazador Szarr", a, "Force") for a in (9, 9, 9)] + [cast(G, "Gale", "Magic Missile")]
        evs += [ev("turn_started", **ref(C, "Claude")), cast(C, "Claude", "Dash"),
                dmg(C, "Claude", GH2, "Fallen Gur Hunter", 8, "Slashing"), cast(C, "Claude", "Main Hand Attack")]
        evs += self.spawn_turns()
        evs += [ev("round_started", round=2), ev("turn_started", **ref(SH, "Shadowheart")),
                cast(SH, "Shadowheart", "Mass Cure Wounds"), ev("turn_started", **ref(G, "Gale"))]
        evs += [dmg(G, "Gale", CZ, "Cazador Szarr", a, "Fire") for a in (12, 11, 12, 14)]
        evs += [cast(G, "Gale", "Scorching Ray")] + self.spawn_turns()
        # r3: Cazador's Call Lightning on Gale; Gale's 3 Psychic back at him (no cast of ours: a retaliation)
        evs += [ev("round_started", round=3), ev("turn_started", **ref(CZ, "Cazador Szarr")),
                dmg(CZ, "Cazador Szarr", G, "Gale", 7, "Lightning"), dmg(G, "Gale", CZ, "Cazador Szarr", 3, "Psychic"),
                cast(CZ, "Cazador Szarr", "Activate Call Lightning")] + self.spawn_turns()
        evs += [ev("round_started", round=4), ev("turn_started", **ref(CZ, "Cazador Szarr")),
                cast(CZ, "Cazador Szarr", "Ascension"), ev("died", **ref("u-leon", "Leon")),
                ev("died", **ref("u-petras", "Pale Petras"))]
        tr.feed(evs, PARTY_G, now=60.0)
        tr.observe(self.cazador_rows(), PARTY_G)
        return tr, tr.close(ROWS_G, ["Scroll of Sunbeam"], {"session": "S074"}, reloaded=True, now=369.0)

    def test_heals_get_no_damage_and_each_hit_goes_to_its_own_cast(self):
        _, rec = self.fight()
        ab = {(a["caster"], a["name"]): a for a in rec["abilities"]}
        for heal in ("Healing Word", "Mass Cure Wounds"):                  # old code: 37 and 49 dmg
            self.assertEqual((ab[("Shadowheart", heal)]["hits"], ab[("Shadowheart", heal)]["dmg"]), (0, 0))
        # 4 rays, plus the r3 Psychic: no cast claimed it, so the round's end gives it to Gale's latest cast
        self.assertEqual((ab[("Gale", "Scorching Ray")]["hits"], ab[("Gale", "Scorching Ray")]["dmg"]), (5, 49 + 3))
        self.assertEqual((ab[("Gale", "Magic Missile")]["hits"], ab[("Gale", "Magic Missile")]["dmg"]), (4, 37))
        self.assertEqual((ab[("Claude", "Dash")]["hits"], ab[("Claude", "Main Hand Attack")]["dmg"]), (0, 8))
        txt = debrief.render(rec)
        self.assertNotIn("Mass Cure Wounds", txt)
        self.assertIn("Gale Scorching Ray x1: 5 hit/0 miss, 52 dmg", txt)

    def test_ritual_victims_are_no_enemies_and_their_deaths_no_kills(self):
        _, rec = self.fight()
        self.assertEqual({e["name"] for e in rec["enemies"]}, {"Cazador Szarr", "Fallen Gur Hunter"})
        self.assertEqual(rec["kills"], [{"name": "Fallen Gur Hunter", "round": 1}])
        self.assertEqual(rec["idle"][:2], [{"name": "Leon", "died": 4}, {"name": "Pale Petras", "died": 4}])
        txt = debrief.render(rec)
        self.assertIn("killed 1/3 (Fallen Gur Hunter r1)", txt)                # old code: killed 3/9
        self.assertIn("hostile but idle, not counted: Leon (died r4), Pale Petras (died r4), Aurelia, Dalyria +2", txt)
        self.assertNotIn("Leon r4", txt)

    def test_an_enemy_without_a_turn_yet_is_still_a_foe(self):
        tr = debrief.Tracker()
        tr.feed([ev("combat_started"), ev("round_started", round=1)], PARTY_G, now=0.0)
        tr.observe(self.cazador_rows(), PARTY_G)
        tr.feed([dmg(CZ, "Cazador Szarr", G, "Gale", 31, "Lightning")], PARTY_G, now=5.0)
        rec = tr.close(ROWS_G, [], {}, reloaded=True, now=10.0)   # reloaded before anyone else's turn
        self.assertIn("Leon", {e["name"] for e in rec["enemies"]})
        self.assertNotIn("idle", rec)

    def test_one_small_hit_of_a_type_is_no_resistance(self):
        _, rec = self.fight()
        self.assertFalse([t for t in rec["dmg_types"] if t["low"]], rec["dmg_types"])   # old code: Psychic (1 hit)
        self.assertNotIn("did little", " ".join(rec["key_facts"]))
        self.assertNotIn("switch damage types", debrief.last_attempt(rec))
        self.assertNotIn("low damage", debrief.render(rec))

    def test_the_stored_s074_record_warns_no_more(self):
        hist = [json.loads(x) for x in S074_REC.read_text().splitlines()]
        self.assertIn("Psychic did little to Cazador Szarr (1 hit, avg 3.0)", hist[0]["key_facts"])   # as stored
        tr = debrief.Tracker(hist)
        cs = {"order": [{"name": "Cazador Szarr", "relation": "hostile"}, {"name": "Bat", "relation": "hostile"}]}
        tr.feed([ev("combat_started"), ev("round_started", round=1)], PARTY_G)
        line = tr.first_turn_warnings(cs)[0]
        self.assertTrue(line.startswith("LAST TIME vs Bat, Cazador Szarr (reloaded, was costly, S074 23:04): Gale went "
                                        "down first (r3, by Predator Werewolf); most damage to us: Cazador Szarr"), line)
        self.assertNotIn("did little", line)
        self.assertNotIn("Psychic", tr.history[0]["summary"])
        self.assertEqual(tr.history[0]["enemies"][3]["resisted"], [])
        w, _ = tr.feed([dmg(G, "Gale", CZ, "Cazador Szarr", 4, "Psychic")], PARTY_G)   # try 2, 23:22:06
        self.assertEqual(w, [])
        self.assertNotIn("switch damage types", debrief.last_attempt(tr.history[0]))

    def test_a_three_hit_low_type_still_counts(self):
        # the same attack doing under half to one enemy what it did to another (V18b: 3 compared hits, was 2)
        tr = debrief.Tracker()
        evs = [ev("combat_started"), ev("round_started", round=1)]
        for tgt, nm_t, amt in ((GH, "Fallen Gur Hunter", 9), (GH2, "Fallen Gur Hunter", 10), (CZ, "Cazador Szarr", 2),
                               (CZ, "Cazador Szarr", 3), (CZ, "Cazador Szarr", 2)):
            evs += hit(G, "Gale", tgt, nm_t, ("Psychic", amt)) + [cast(G, "Gale", "Mind Sliver")]
        tr.feed(evs, PARTY_G)
        rec = tr.close(ROWS_G, [], {}, now=60.0)
        self.assertEqual([(t["enemy"], t["type"], t["hits"]) for t in rec["dmg_types"] if t["low"]],
                         [("Cazador Szarr", "Psychic", 3)])

    def test_a_down_is_named_by_the_cast_that_follows_it(self):
        # the mod's order: damage, downed, then the enemy's cast (S074 r3: Gale downed by a werewolf's Multiattack)
        tr = debrief.Tracker()
        tr.feed([ev("combat_started"), ev("round_started", round=3), ev("turn_started", **ref("u-ww", "Predator Werewolf")),
                 dmg("u-ww", "Predator Werewolf", G, "Gale", 17, "Slashing"), ev("downed", **ref(G, "Gale")),
                 cast("u-ww", "Predator Werewolf", "Multiattack"), ev("turn_started", **ref(C, "Claude")),
                 cast("u-ww", "Predator Werewolf", "Claws")], PARTY_G)
        rec = tr.close(ROWS_G, [], {}, now=60.0)
        self.assertEqual(rec["downed"], [{"name": "Gale", "round": 3, "by": "Predator Werewolf", "ability": "Multiattack"}])


S076_REC = Path(__file__).parent / "golden" / "fights_s076.jsonl"   # real v2 records: Cazador tries 6-7 (S076)
S077_REC = Path(__file__).parent / "golden" / "fights_s077.jsonl"   # real v2 record: Cazador try 9, won (S077)
BATS = [f"u-bat-{i}" for i in range(6)]
WW, WW2, CT = "u-ww-1", "u-ww-2", "u-chatterteeth"


class S077DidLittle(unittest.TestCase):
    """vis V18b: S077's mid-fight WARNINGs came from S076's v2 records (each x2): `Piercing did little to Bat (2 hits,
    avg 5.0)` = killing blows on 1-HP bats, `Piercing did little to Cazador Szarr (2 hits, avg 7.5)` = two hits,
    `Radiant did little to Predator Werewolf (7 hits, avg 11.7)` = Sacred Flame / Guiding Bolt next to Fireball's 33
    (the same Radiant did 14.5 on the bats). S077's own record flagged Magic Missile's Force on the bats and on the
    Chatterteeth (3 darts each) against Fireball. A low type now needs >= 3 hits that are no killing blow, each
    compared with the same attack on other enemies."""

    def history(self):
        return [json.loads(ln) for p in (S076_REC, S077_REC) for ln in p.read_text().splitlines()]

    def test_the_real_records_raise_no_warning(self):
        hist = self.history()
        self.assertEqual({(t["enemy"], t["type"]) for r in hist for t in r["dmg_types"] if t["low"]},
                         {("Predator Werewolf", "Piercing"), ("Bat", "Piercing"), ("Cazador Szarr", "Psychic"),
                          ("Cazador Szarr", "Piercing"), ("Predator Werewolf", "Radiant"),
                          ("Fallen Gur Hunter", "Piercing"), ("Bat", "Force"), ("Chatterteeth", "Force")})  # as recorded
        tr = debrief.Tracker(hist)
        tr.feed([ev("combat_started"), ev("round_started", round=1)], PARTY_G)
        w, _ = tr.feed(hit(K, "Karlach", BATS[0], "Bat", ("Piercing", 5)) + [ev("died", **ref(BATS[0], "Bat"))]
                       + hit(C, "Claude", CZ, "Cazador Szarr", ("Piercing", 8)) + [cast(C, "Claude", "Main Hand Attack")]
                       + hit(SH, "Shadowheart", WW, "Predator Werewolf", ("Radiant", 12))
                       + [cast(SH, "Shadowheart", "Sacred Flame")], PARTY_G)
        self.assertEqual(w, [])   # old code: the three S077 WARNINGs
        self.assertNotIn("did little", debrief.last_attempt(tr.history[-1]))

    def fight(self):
        tr = debrief.Tracker()
        evs = [ev("combat_started"), ev("round_started", round=1)]
        # Fireball: 1-HP bats die (each hit a killing blow), Cazador and a werewolf take 30-33
        evs += [x for b in BATS[:3] for x in hit(G, "Gale", b, "Bat", ("Fire", 28)) + [ev("died", **ref(b, "Bat"))]]
        evs += hit(G, "Gale", CZ, "Cazador Szarr", ("Fire", 30)) + hit(G, "Gale", WW, "Predator Werewolf", ("Fire", 33))
        evs += [cast(G, "Gale", "Fireball")]
        # Magic Missile: 3 darts on the Chatterteeth, 3 killing darts on bats: nothing to compare with
        evs += hit(G, "Gale", CT, "Chatterteeth", ("Force", 9)) * 3
        evs += [x for b in BATS[3:] for x in hit(G, "Gale", b, "Bat", ("Force", 8)) + [ev("died", **ref(b, "Bat"))]]
        evs += [cast(G, "Gale", "Magic Missile")]
        # Sacred Flame: 11-12 on the werewolves, 13-14 on Cazador (no resistance: comparable)
        for tgt, tn, amt in ((WW, "Predator Werewolf", 11), (WW2, "Predator Werewolf", 12), (WW, "Predator Werewolf", 12),
                             (CZ, "Cazador Szarr", 14), (CZ, "Cazador Szarr", 13)):
            evs += hit(SH, "Shadowheart", tgt, tn, ("Radiant", amt)) + [cast(SH, "Shadowheart", "Sacred Flame")]
        # Karlach's Piercing on Cazador: 2 hits only
        evs += hit(K, "Karlach", CZ, "Cazador Szarr", ("Piercing", 7)) + hit(K, "Karlach", CZ, "Cazador Szarr", ("Piercing", 8))
        evs += [cast(K, "Karlach", "Main Hand Attack"), ev("combat_ended")]
        tr.feed(evs, PARTY_G)
        return tr.close(ROWS_G, [], {"session": "S077"}, now=600.0)

    def test_no_low_type_from_kills_few_hits_or_other_attacks(self):
        rec = self.fight()
        self.assertEqual([(t["enemy"], t["type"]) for t in rec["dmg_types"] if t["low"]], [])
        rad = next(t for t in rec["dmg_types"] if (t["enemy"], t["type"]) == ("Predator Werewolf", "Radiant"))
        self.assertEqual((rad["compared"], rad["got"], rad["expected"]), (3, 11.7, 13.5))
        bat = [t for t in rec["dmg_types"] if t["enemy"] == "Bat"]
        self.assertTrue(bat and all("compared" not in t for t in bat), bat)   # killing blows: never compared
        self.assertNotIn("did little", debrief.render(rec))

    def test_a_real_resistance_still_shows_and_warns_next_time(self):
        # the same Sacred Flame at a quarter on one werewolf, 3 hits: low, and the next fight's first hit warns
        tr = debrief.Tracker()
        evs = [ev("combat_started"), ev("round_started", round=1)]
        for tgt, tn, amt in ((CZ, "Cazador Szarr", 14), (CZ, "Cazador Szarr", 13), (WW, "Predator Werewolf", 3),
                             (WW, "Predator Werewolf", 4), (WW, "Predator Werewolf", 3)):
            evs += hit(SH, "Shadowheart", tgt, tn, ("Radiant", amt)) + [cast(SH, "Shadowheart", "Sacred Flame")]
        tr.feed(evs + [ev("combat_ended")], PARTY_G)
        rec = tr.close(ROWS_G, [], {"session": "S999"}, now=60.0)
        self.assertIn("Radiant did little to Predator Werewolf (3 hits, avg 3.3 vs 13.5 from the same attacks on "
                      "other enemies)", rec["key_facts"])
        tr2 = debrief.Tracker([rec])
        tr2.feed([ev("combat_started"), ev("round_started", round=1)], PARTY_G)
        w, _ = tr2.feed(hit(SH, "Shadowheart", WW2, "Predator Werewolf", ("Radiant", 4)), PARTY_G)
        self.assertEqual(w, [("WARNING: Radiant did little to Predator Werewolf last time too (3 hits, avg 3.3 vs 13.5 "
                              "from the same attacks on other enemies, S999): use another damage type on it")])


CB = "u-cambion"


class S078Summon(unittest.TestCase):
    """vis V19: S078-r1 Viconia try 1 read `no visible source 623 of 564`: in the Sharran Darkness every hit came from
    "Someone", also those on Claude's Planar Ally Cambion (e520, a summon: in the party set the events are read with,
    not in the party list whose "taken" is the denominator)."""

    def fight(self, cambion_hits):
        tr = debrief.Tracker()
        evs = [ev("combat_started"), ev("round_started", round=1)]
        evs += [unseen(u, n, a, t) for u, n, a, t in ((K, "Karlach", 17, "Necrotic"), (K, "Karlach", 6, "Slashing"),
                                                    (G, "Gale", 16, "Necrotic"), (SH, "Shadowheart", 2, "Necrotic"))]
        evs += [unseen(CB, "Cambion", a, t) for a, t in cambion_hits]
        evs += [dmg(NU, "Viconia DeVir", C, "Claude", 8, "Piercing"), ev("combat_ended")]
        tr.observe({"order": [{"uuid": CB, "name": "Cambion", "relation": "party", "summon": True}]}, PARTY_G | {CB})
        tr.feed(evs, PARTY_G | {CB}, now=0.0)
        return tr.close(ROWS_G, [], {"session": "S078"}, now=60.0)

    def test_a_summons_unseen_hits_are_not_ours(self):
        rec = self.fight([(19, "Necrotic"), (24, "Necrotic"), (10, "Necrotic"), (3, "Bludgeoning")])
        taken = sum(p["taken"] for p in rec["party"])
        self.assertEqual(taken, 49)
        self.assertEqual(rec["unseen"], {"dmg": 41, "types": [{"type": "Necrotic", "hits": 3, "avg": 11.7},
                                                              {"type": "Slashing", "hits": 1, "avg": 6.0}]})
        self.assertIn("most damage to us: no visible source 41 of 49 (Necrotic 3 hits avg 11.7, Slashing 1 hit avg 6.0)"
                      ": traps, surfaces or unseen attackers", rec["key_facts"])   # old code: 97 of 49
        self.assertEqual(rec["grade"], "clean")

    def test_only_a_summon_hit_unseen_leaves_no_unseen(self):
        tr = debrief.Tracker()
        tr.feed([ev("combat_started"), ev("round_started", round=1), unseen(CB, "Cambion", 24, "Necrotic"),
                 dmg(NU, "Viconia DeVir", C, "Claude", 8, "Piercing"), ev("combat_ended")], PARTY_G | {CB}, now=0.0)
        rec = tr.close(ROWS_G, [], {"session": "S078"}, now=60.0)
        self.assertNotIn("unseen", rec)
        self.assertNotIn("no visible source", debrief.render(rec))

    def test_a_summons_damage_does_not_make_the_fight_costly(self):
        # 30 % of the members' max HP (190+104+90+76+158 = 618) is 185.4: the Cambion's 200 used to count
        rec = self.fight([(50, "Necrotic")] * 4)
        self.assertEqual(rec["grade"], "clean")


VI, OW = "u-viconia", "u-owltalon"
MINOR = [("u-hyrald", "Sharran Fidelian Hyrald", 1), ("u-nightmist", "Justiciar Crusader Nightmist", 1),
         ("u-fredrichk", "Sentry Fredrichk", 2), ("u-gydd", "Sharran Fidelian Gydd", 3)]


class S080Viconia(unittest.TestCase):
    """vis V21: S080 Viconia try 7 killed Viconia r4 (the 5th kill), then the cult wiped the party in r5. The debrief's
    kill order showed the first 4 kills (`…`), so she was cut off, and LAST ATTEMPT said `focus Viconia DeVir first`
    after her death. Try 9 knocked her out (`now neutral (was hostile), HP 65->0/86, +Knocked Out`: no died event)."""

    def rows(self, vi_pct=60, vi_rel="hostile"):
        return {"order": [{"uuid": VI, "name": "Viconia DeVir", "relation": vi_rel, "hp_pct": vi_pct},
                          {"uuid": OW, "name": "Justiciar Crusader Owltalon", "relation": "hostile", "hp_pct": 80}]
                + [{"uuid": u, "name": n, "relation": "hostile", "hp_pct": 100} for u, n, _ in MINOR]}

    def fight(self, viconia_dies=True, knocked_out_rows=()):
        tr = debrief.Tracker()
        tr.feed([ev("combat_started"), ev("round_started", round=1)], PARTY_G, now=0.0)
        tr.observe(self.rows(100), PARTY_G)
        evs = [dmg(VI, "Viconia DeVir", W, "Wyll", 60, "Radiant"), cast(VI, "Viconia DeVir", "Spirit Guardians"),
               dmg(OW, "Justiciar Crusader Owltalon", G, "Gale", 30, "Slashing")]
        for r in (1, 2, 3, 4):
            if r > 1:
                evs.append(ev("round_started", round=r))
            for u, n, kr in MINOR:
                if kr == r:
                    evs += [dmg(u, n, C, "Claude", 5, "Piercing"), dmg(C, "Claude", u, n, 20, "Slashing"),
                            cast(C, "Claude", "Main Hand Attack"), ev("died", **ref(u, n))]
        evs += [dmg(G, "Gale", VI, "Viconia DeVir", 40, "Force"), cast(G, "Gale", "Magic Missile")]
        if viconia_dies:
            evs.append(ev("died", **ref(VI, "Viconia DeVir")))
        tr.feed(evs, PARTY_G, now=60.0)
        for rows in knocked_out_rows:
            tr.observe(rows, PARTY_G)
        tr.feed([ev("round_started", round=5), ev("died", **ref(W, "Wyll"))], PARTY_G, now=90.0)
        return tr.close(ROWS_G, [], {"session": "S080"}, reloaded=True, now=120.0)

    def test_every_kill_is_listed(self):
        rec = self.fight()
        self.assertEqual([k["name"] for k in rec["kills"]][-1], "Viconia DeVir")
        self.assertIn("killed 5/6 (Sharran Fidelian Hyrald r1, Justiciar Crusader Nightmist r1, Sentry Fredrichk r2, "
                      "Sharran Fidelian Gydd r3, Viconia DeVir r4)", debrief.render(rec))   # old code: …, … (4)

    def test_kills_of_one_name_in_a_round_read_compactly(self):
        rec = {"grade": "clean", "duration_s": 60, "rounds": 2, "enemies": [{"name": "Bat", "count": 3, "killed": 3}],
               "kills": [{"name": "Bat", "round": 2}] * 3, "party": []}
        self.assertIn("killed 3/3 (Bat x3 r2)", debrief.render(rec))
        old = {**rec, "enemies": [{"name": "Bat", "count": 14, "killed": 14}], "kills": [{"name": "Bat", "round": 2}] * 12}
        self.assertIn("killed 14/14 (Bat x12 r2, …)", debrief.render(old))   # a record from before V21 kept 12

    def test_no_focus_on_an_enemy_the_record_shows_dead(self):
        rec = self.fight()
        la = debrief.last_attempt(rec)
        self.assertNotIn("focus Viconia DeVir first", la)
        self.assertIn("after Viconia DeVir (killed r4), focus Justiciar Crusader Owltalon first", la)
        self.assertIn("most damage to us: Viconia DeVir (Spirit Guardians) 60, killed r4", rec["key_facts"])

    def test_the_stored_s080_try_7_record(self):
        """The record as S080.fights.jsonl stored it (kills all 5, enemies' killed counts): read again, no focus on
        Viconia; try 8's (she survived at 21 %) keeps it."""
        try7 = {"grade": "reloaded", "summary": "reloaded (was wipe) vs Viconia DeVir", "rounds": 5,
                "enemies": [{"name": "Viconia DeVir", "count": 1, "killed": 1, "dmg_to_us": 308},
                            {"name": "Justiciar Crusader Owltalon", "count": 1, "killed": 0, "dmg_to_us": 122},
                            {"name": "Sharran Sentry Hendly", "count": 1, "killed": 0, "dmg_to_us": 68}],
                "kills": [{"name": "Sharran Fidelian Hyrald", "round": 1}, {"name": "Viconia DeVir", "round": 4}],
                "party": [{"name": "Gale", "dealt": 172, "taken": 217}, {"name": "Wyll", "dealt": 67, "taken": 262}]}
        la = debrief.last_attempt(try7)
        self.assertNotIn("focus Viconia DeVir first", la)
        self.assertIn("focus Justiciar Crusader Owltalon first", la)
        try8 = {**try7, "enemies": [{**try7["enemies"][0], "killed": 0}] + try7["enemies"][1:],
                "kills": try7["kills"][:1], "left": [{"name": "Viconia DeVir", "pct": 21, "low_pct": 21}]}
        self.assertIn("focus Viconia DeVir first", debrief.last_attempt(try8))
        self.assertIn("most damage to us: Viconia DeVir 308, left at 21%", debrief.key_facts(try8))

    def test_a_knocked_out_enemy_is_down(self):
        rec = self.fight(viconia_dies=False, knocked_out_rows=[self.rows(0, "neutral")])
        self.assertEqual(rec["out"], [{"name": "Viconia DeVir", "round": 4}])
        self.assertNotIn("Viconia DeVir", {x["name"] for x in rec.get("left") or []})   # old: left at 60 %
        txt = debrief.render(rec)
        self.assertIn("killed 4/6 (", txt)
        self.assertIn(" + knocked out Viconia DeVir r4", txt)
        la = debrief.last_attempt(rec)
        self.assertNotIn("focus Viconia DeVir first", la)
        self.assertIn("after Viconia DeVir (knocked out r4), focus Justiciar Crusader Owltalon first", la)

    def test_a_knocked_out_enemy_back_up_counts_again(self):
        rec = self.fight(viconia_dies=False, knocked_out_rows=[self.rows(0, "neutral"), self.rows(30)])
        self.assertNotIn("out", rec)
        self.assertIn("focus Viconia DeVir first", debrief.last_attempt(rec))

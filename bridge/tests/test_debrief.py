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
    return [
        ev("combat_started"), ev("round_started", round=1),
        cast(SH, "Shadowheart", "Sacred Flame"), dmg(SH, "Shadowheart", AV, "Justiciar Avenger", 20, "Radiant"),
        cast(W, "Wyll", "Eldritch Blast"), dmg(W, "Wyll", AV, "Justiciar Avenger", 0, ""),
        dmg(W, "Wyll", AV, "Justiciar Avenger", 12, "Force"),
        cast(SH, "Shadowheart", "Spirit Guardians"), dmg(SH, "Shadowheart", TR, "Umbral Tremor", 2, "Necrotic"),
        dmg(SH, "Shadowheart", AV, "Justiciar Avenger", 9, "Necrotic"),
        cast(SO, "Justiciar Soulhunter", "Living Shadow"),
        ev("status_applied", target=ref(W, "Wyll"), source=ref(SO, "Justiciar Soulhunter"), name="Blinded"),
        dmg(SO, "Justiciar Soulhunter", W, "Wyll", 30, "Piercing"),
        ev("round_started", round=2),
        dmg(SO, "Justiciar Soulhunter", W, "Wyll", 46, "Piercing"), ev("downed", **ref(W, "Wyll")),
        dmg(AV, "Justiciar Avenger", C, "Claude", 20, "Slashing"),
        ev("used", char=ref(C, "Claude"), item=ref("u-pot", "Potion of Healing"), success=True),
        cast(C, "Claude", "Main Hand Attack"), dmg(C, "Claude", TR, "Umbral Tremor", 10, "Bludgeoning"),
        dmg(C, "Claude", TR, "Umbral Tremor", 11, "Bludgeoning"),
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
        self.assertEqual(sg["targets_max"], 2)
        low = [t for t in rec["dmg_types"] if t["low"]]
        self.assertEqual([(t["enemy"], t["type"]) for t in low], [("Umbral Tremor", "Necrotic")])
        self.assertEqual(rec["pos"]["surrounded"], {"name": "Claude", "n": 3, "round": 1})
        self.assertEqual(rec["pos"]["elev_m"], 3.0)
        self.assertEqual(rec["position"], ["surrounded", "enemy_high_ground"])
        self.assertEqual(rec["group"], "justiciar avenger|justiciar soulhunter|umbral tremor")
        self.assertEqual(names["Umbral Tremor"]["resisted"], ["Necrotic"])
        self.assertEqual(names["Justiciar Soulhunter"]["n"], 1)
        self.assertEqual(rec["unused"], ["Scroll of Fireball"])
        self.assertEqual(rec["consumables"], {"used": ["Potion of Healing"], "unused": ["Scroll of Fireball"]})
        party = {p["name"]: p for p in rec["party"]}
        self.assertEqual(party["Shadowheart"]["dealt"], 31)
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
        self.assertIn("use the unused scroll/potion", la)
        note = debrief.fight_note(rec)
        self.assertTrue(note.startswith("costly @SCL vs Justiciar Soulhunter, Justiciar Avenger (Human), Umbral Tremor | "), note)
        self.assertIn("| rounds 2 | downed: Wyll by Justiciar Soulhunter | dealt 64 / taken 96 |", note)
        self.assertIn("| resisted: Umbral Tremor:Necrotic | unused: Scroll of Fireball | Wyll went down first", note)

    def test_saves_summons_and_mirror_names(self):
        # S035 Self-Same Trial: mirror copies named like the party, Toll the Dead saved (0 Necrotic) x3, the
        # Spiritual Weapon's hits arrive with Shadowheart as the source; the copies vanish (no died events).
        tr = debrief.Tracker()
        SW, MW, MS = "u-sw", "u-mirror-wyll", "u-mirror-sh"
        evs = [ev("combat_started"), ev("round_started", round=1),
               ev("cast", caster=ref(SH, "Shadowheart"), spell="Target_TollTheDead", name="Toll the Dead", roll="save"),
               dmg(SH, "Shadowheart", MS, "Shadowheart", 0, "Necrotic"),
               ev("cast", caster=ref(SH, "Shadowheart"), spell="Target_TollTheDead", name="Toll the Dead", roll="save"),
               dmg(SH, "Shadowheart", MW, "Wyll", 0, "Necrotic"),
               ev("cast", caster=ref(SW, "Spiritual Weapon"), spell="x", name="Greataxe Slash", roll="attack"),
               dmg(SH, "Shadowheart", MW, "Wyll", 11, "Slashing"),
               dmg(MW, "Wyll", C, "Claude", 9, "Force"),
               cast(W, "Wyll", "Eldritch Blast"), dmg(W, "Wyll", MW, "Wyll", 10, "Force"),
               ev("combat_ended")]
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
        self.assertEqual(rec["key_facts"][0], "DIED: Shadowheart")
        self.assertIn("DIED: Shadowheart — revive now", debrief.render(rec).splitlines()[1])

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

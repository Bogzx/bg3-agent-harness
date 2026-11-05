"""Offline tests: the EVENTS tail keeps what the agent can't read again (w11 L4, Balthazar: one 44.6 s end_turn +
wait_for showed '(+56 earlier events)' and the Cloudkill spell_info line was among them, pushed out by 12 zero-damage
rows of Cloudkill on its immune skeletons). Info-preserving only: no event is dropped.

Run: cd bridge && uv run python -m unittest tests.test_event_tail
"""

from __future__ import annotations

import unittest
from unittest import mock

from test_economy import Handles  # sets BG3AI_RUNTIME before importing bg3ai

# isort: split
from bg3ai import render
from bg3ai.tools import core, reporting, turn_plan


def _u(n: int) -> str:
    return f"00000000-0000-0000-0000-{n:012d}"


BALTHAZAR = {"uuid": _u(7), "name": "Balthazar"}


def ref(n: int, name: str) -> dict:
    return {"uuid": _u(n), "name": name}


def dmg(target: dict, amount: int = 0, typ: str = "") -> dict:
    # key order as the L4 result printed it
    return {"kind": "damage", "data": {"source": BALTHAZAR, "type": typ, "amount": amount, "target": target}}


CLOUDKILL_INFO = (
    "Cloudkill: Create a cloud of poison. (level 5, concentration, range 18m, area 6m, Constitution save, "
    "5d8 Poison damage, lasts 10 turns)"
)
SKELETONS = [
    ref(9, "Apostate Defender"),
    ref(12, "Apostate Defender"),
    ref(12, "Apostate Defender"),
    ref(15, "Apostate Defender"),
    ref(15, "Apostate Defender"),
    ref(17, "Apostate Defender"),
    ref(17, "Apostate Defender"),
    ref(8, "Apostate Destroyer"),
    ref(8, "Apostate Destroyer"),
]


def l4_batch() -> list[dict]:
    """The L4 call's events: 56 earlier ones (the cast of Cloudkill's spell_info among them, then earlier rounds'
    hits) and the 25 the result showed, as in v010w11/l4-spellinfo.txt."""
    early = [
        {"kind": "cast", "data": {"caster": BALTHAZAR, "name": "Cloudkill"}},
        {"kind": "spell_info", "data": CLOUDKILL_INFO},
    ]
    early += [dmg(ref(30 + i % 6, f"Apostate Sentinel {i % 6}")) for i in range(3)]  # zero rows earlier too
    early += [
        {"kind": "status_applied", "data": {"name": f"Status {i}", "target": ref(40 + i, f"Ghoul {i}")}}
        for i in range(51)
    ]
    tail = [dmg(t) for t in SKELETONS]
    tail += [
        dmg(ref(6, "Claude"), 13, "Poison"),
        dmg(ref(4, "Karlach"), 12, "Poison"),
        dmg(ref(3, "Wyll"), 25, "Poison"),
        dmg(ref(5, "Shadowheart"), 12, "Poison"),
        {"kind": "status_applied", "data": {"name": "Covert Critical", "target": ref(5, "Shadowheart")}},
        {"kind": "status_applied", "data": {"name": "Darkveil Precision", "target": ref(3, "Wyll")}},
        {"kind": "cast", "data": {"caster": BALTHAZAR, "name": "Cloudkill"}},
        dmg(ref(3, "Wyll")),
        {"kind": "cast", "data": {"roll": "attack", "caster": BALTHAZAR, "name": "Ray of Sickness"}},
        {
            "kind": "status_applied",
            "data": {"source": BALTHAZAR, "name": "Undead Ward", "target": ref(21, "Apostate Sentinel")},
        },
        {"kind": "cast", "data": {"caster": BALTHAZAR, "name": "Misty Step"}},
        {"kind": "spell_info", "data": "Misty Step: Surrounded by silver mist, you teleport. (level 2, range 18m)"},
        {"kind": "turn_started", "data": ref(6, "Claude")},
        {"kind": "turn_started", "data": ref(5, "Shadowheart")},
        {"kind": "turn_ended", "data": BALTHAZAR},
        {"kind": "roll_result", "data": {"success": True, "roller": ref(6, "Claude"), "critical": "success"}},
    ]
    assert len(early) == 56 and len(tail) == 25, (len(early), len(tail))
    for i, e in enumerate(early + tail):
        e["seq"] = i
    return early + tail


class EventTail(unittest.TestCase):
    def test_a_cut_spell_info_still_shows(self):
        out = render.render_events(l4_batch(), Handles())
        self.assertIn("spell_info " + CLOUDKILL_INFO, out)
        self.assertIn("spell_info Misty Step", out)

    def test_back_to_back_zero_damage_rows_share_a_row_naming_every_target_and_count(self):
        out = render.render_events(l4_batch(), Handles())
        rows = [ln.strip() for ln in out.split("\n")]
        zero = [r for r in rows if r.startswith("damage") and "amount=0" in r]
        # the 9 skeleton rows in a row -> one; the zero row on Wyll after the Cloudkill cast keeps its own place
        self.assertEqual(len(zero), 2, zero)
        self.assertEqual(
            zero[0],
            # handles e2-e4 went to the three earlier zero rows' targets
            "damage source={e1 name=Balthazar} type= amount=0 x9 on 5: Apostate Defender e5, Apostate Defender e6 x2,"
            " Apostate Defender e7 x2, Apostate Defender e8 x2, Apostate Destroyer e9 x2",
        )
        i = rows.index(zero[1])
        self.assertTrue(rows[i - 1].startswith("cast caster={e1 name=Balthazar} name=Cloudkill"), rows[i - 1])
        self.assertTrue(rows[i + 1].startswith("cast roll=attack"), rows[i + 1])
        # the non-zero hits keep a row each
        self.assertEqual(sum(1 for r in rows if r.startswith("damage") and "type=Poison" in r), 4)

    def test_nothing_is_dropped_the_count_is_what_is_not_shown(self):
        out = render.render_events(l4_batch(), Handles())
        rows = [ln.strip() for ln in out.split("\n")[1:]]
        # 81 events: the 3 earlier zero rows -> 1, the 9 skeleton rows -> 1 (71); the 25 newest are shown plus the
        # earlier cast and spell_info (the two turn_started share one line)
        self.assertEqual(rows[0], "(+44 earlier events)")
        self.assertEqual(len(rows), 1 + 2 + 25 - 1)

    def test_zero_rows_apart_or_of_another_source_stay_apart(self):
        other = dmg(ref(9, "Apostate Defender"))
        other["data"]["source"] = ref(11, "Apostate Defender")
        evs = [dmg(ref(6, "Claude")), other, dmg(ref(5, "Shadowheart"))]
        out = render.render_events(evs, Handles())
        self.assertEqual(out.count("amount=0 target="), 3, out)

    def test_under_the_cap_nothing_changes_but_the_zero_grouping(self):
        evs = [{"seq": 1, "kind": "spell_info", "data": CLOUDKILL_INFO}, dmg(ref(9, "Apostate Defender"))]
        out = render.render_events(evs, Handles())
        self.assertNotIn("earlier events", out)
        self.assertIn("amount=0 target=", out)  # one zero hit: its own plain row


PARTY = {_u(1), _u(2)}


def npc_rows(n: int, start: int = 100) -> list[dict]:
    """n plain rows no pin covers: a status on a non-party character each (distinct names: no status fold)."""
    return [
        {"kind": "status_applied", "data": {"name": f"Status {i}", "target": ref(start + i, f"Ghoul {i}")}}
        for i in range(n)
    ]


def pinned_rows() -> list[dict]:
    """A cast, a hit on a party member and a party member's roll: REVIEW-14's pins on top of spell_info."""
    return [
        {"kind": "cast", "data": {"caster": ref(150, "Ghoul Boss"), "name": "Bone Chill"}},
        {
            "kind": "damage",
            "data": {"source": ref(150, "Ghoul Boss"), "type": "Necrotic", "amount": 9, "target": ref(1, "Claude")},
        },
        {"kind": "roll_result", "data": {"success": True, "roller": ref(2, "Wyll")}},
    ]


class BatchCap(unittest.TestCase):
    """REVIEW-14: in a fight the whole drained batch is shown (up to EVENT_CEILING rows after the folds); out of one
    the newest 25, with casts and party-targeted rows pinned. Through core._events_text, as every result renders."""

    def setUp(self):
        reporting._FIGHTS["tr"] = None  # a fresh fight tracker: no fight open
        self.addCleanup(lambda: reporting._FIGHTS.update(tr=None))
        self.H = Handles()
        for p in (
            mock.patch.object(core, "H", self.H),
            mock.patch.object(turn_plan, "_party_cached", lambda: PARTY),
            mock.patch.object(render, "FLAP", render.StatusFlap()),
            mock.patch.object(render, "ECHO", render.EventEcho()),
        ):
            p.start()
            self.addCleanup(p.stop)

    def text(self, evs: list[dict]) -> str:
        for i, e in enumerate(evs):
            e["seq"] = i

        class G:
            def drain_events(self, ctx="server", limit=300):
                return list(evs) if ctx == "server" else []

            def try_rpc(self, *a, **k):
                return None

        with mock.patch.object(core, "game", G()):
            return core._events_text()

    def rows(self, out: str) -> list[str]:
        return [ln.strip() for ln in out.split("FIGHT")[0].split("\n") if ln.startswith("  ")]

    def test_in_a_fight_a_60_row_batch_is_shown_whole(self):
        evs = [{"kind": "turn_started", "data": ref(150, "Ghoul Boss")}, *npc_rows(59)]
        out = self.text(evs)
        self.assertNotIn("earlier events", out)
        self.assertEqual(len(self.rows(out)), 60, out)
        # the fight stays open for the next batch, which has no fight event of its own
        out = self.text(
            [
                *npc_rows(59, start=300),
                {"kind": "cast", "data": {"caster": ref(150, "Ghoul Boss"), "name": "Bone Chill"}},
            ]
        )
        self.assertNotIn("earlier events", out)
        self.assertEqual(len(self.rows(out)), 60)

    def test_out_of_a_fight_60_rows_keep_the_cap_with_casts_and_party_rows_pinned(self):
        out = self.text(pinned_rows() + npc_rows(57))
        rows = self.rows(out)
        self.assertEqual(rows[0], "(+32 earlier events)")  # 60 - 25 newest - 3 pinned
        self.assertEqual(len(rows), 1 + 3 + 25)
        self.assertTrue(rows[1].startswith("cast caster={"), rows[1])
        self.assertIn("amount=9 target={e", rows[2])
        self.assertTrue(rows[3].startswith("roll_result"), rows[3])

    def test_over_the_ceiling_in_a_fight_the_pins_stay(self):
        evs = [{"kind": "round_started", "data": {"round": 3}}, *pinned_rows(), *npc_rows(246)]  # 250 rows
        rows = self.rows(self.text(evs))
        self.assertEqual(rows[0], f"(+{250 - render.EVENT_CEILING - 3} earlier events)")
        self.assertEqual(len(rows), 1 + 3 + render.EVENT_CEILING)
        self.assertTrue(rows[1].startswith("cast caster={"), rows[1])
        self.assertTrue(rows[3].startswith("roll_result"), rows[3])

    def test_a_folded_row_with_a_party_target_is_pinned(self):
        boss = ref(150, "Ghoul Boss")
        zero = [
            {"kind": "damage", "data": {"source": boss, "type": "", "amount": 0, "target": t}}
            for t in (ref(1, "Claude"), ref(120, "Ghoul"))
        ]
        rows = self.rows(self.text(zero + npc_rows(40)))
        self.assertEqual(rows[0], "(+15 earlier events)")  # 41 rows after the fold - 25 - 1 pinned
        self.assertIn("amount=0 x2 on 2: Claude e", rows[1])


if __name__ == "__main__":
    unittest.main()

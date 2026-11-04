"""Offline tests: the EVENTS tail keeps what the agent can't read again (w11 L4, Balthazar: one 44.6 s end_turn +
wait_for showed '(+56 earlier events)' and the Cloudkill spell_info line was among them, pushed out by 12 zero-damage
rows of Cloudkill on its immune skeletons). Info-preserving only: no event is dropped.

Run: cd bridge && uv run python -m unittest tests.test_event_tail
"""

from __future__ import annotations

import unittest

from test_economy import Handles  # sets BG3AI_RUNTIME before importing bg3ai

# isort: split
from bg3ai import render


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
        # earlier spell_info (the two turn_started share one line)
        self.assertEqual(rows[0], "(+45 earlier events)")
        self.assertEqual(len(rows), 1 + 1 + 25 - 1)

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


if __name__ == "__main__":
    unittest.main()

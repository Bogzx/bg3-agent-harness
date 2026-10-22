"""Offline tests: the harness readers for the hidden referee / roll rings (bg3ai.referee), and the fairness rule that
no agent-facing code reaches them.

Run: cd bridge && uv run python -m unittest tests/test_referee.py
"""

from __future__ import annotations

import json
import re
import tempfile
import unittest
from pathlib import Path
from typing import ClassVar

import jsonschema
from bg3ai import referee

BRIDGE = Path(__file__).resolve().parents[1]
REPO = BRIDGE.parent
FACTS_SCHEMA = json.loads((REPO / "packages/harness/schemas/facts.v1.schema.json").read_text())
HIDDEN_OPS = ("ref.since", "ref.snapshot", "roll.since", "roll.snapshot", "scn.")


class FakeRing:
    """The mod's ring op: records with a monotonic `t` (ms), a boot id, a cap that drops the oldest."""

    def __init__(self, boot: str = "b1", cap: int = 1000):
        self.boot, self.cap, self.now, self.seq = boot, cap, 50_000.0, 0
        self.ring: list[dict] = []
        self.calls: list[dict] = []

    def add(self, ev: str, **data) -> None:
        self.seq += 1
        self.ring.append(dict(data, seq=self.seq, t=self.now, gt=12.5, ev=ev))
        del self.ring[: max(0, len(self.ring) - self.cap)]

    def load(self, boot: str) -> None:
        self.boot, self.seq, self.ring = boot, 0, []

    def __call__(self, op: str, args: dict) -> dict:
        self.calls.append(dict(args, op=op))
        out = [r for r in self.ring if r["seq"] > args["seq"]][: args["limit"]]
        first = self.ring[0]["seq"] if self.ring else self.seq + 1
        return {"boot": self.boot, "now": self.now, "latest": self.seq, "first": first, "events": out}


class RingReaderTest(unittest.TestCase):
    def setUp(self):
        self.ring = FakeRing()
        self.wall = [1_800_000_000.0]
        self.r = referee.RingReader(self.ring, "ref.since", wall=lambda: self.wall[0], limit=3)

    def test_wall_clock_and_cursor(self):
        self.ring.add("trade_open", npc="Arron")
        self.ring.now += 2500
        self.ring.add("trade_end", npc="Arron")
        self.ring.now += 500
        got = self.r.poll()
        self.assertEqual([g["ev"] for g in got], ["trade_open", "trade_end"])
        self.assertAlmostEqual(got[0]["t"], 1_800_000_000.0 - 3.0)
        self.assertAlmostEqual(got[1]["t"], 1_800_000_000.0 - 0.5)
        self.assertEqual(self.r.poll(), [])
        self.assertEqual(self.ring.calls[-1]["seq"], 2)

    def test_pages_past_the_limit(self):
        for i in range(7):
            self.ring.add("gold", gold=i, delta=1)
        self.assertEqual([g["gold"] for g in self.r.poll()], list(range(7)))

    def test_load_restarts_the_cursor(self):
        self.ring.add("kill", victim="Goblin")
        self.r.poll()
        self.ring.load("b2")
        self.ring.add("died", name="Tav", party=True)
        got = self.r.poll()
        self.assertEqual([g["ev"] for g in got], ["died"])
        self.assertEqual((self.r.boot, self.r.seq, self.r.lost), ("b2", 1, 0))

    def test_cap_losses_are_counted(self):
        self.ring.cap = 2
        for _ in range(5):
            self.ring.add("crime", type="Assault")
        self.assertEqual(len(self.r.poll()), 2)
        self.assertEqual(self.r.lost, 3)

    def test_sync_skips_what_is_there(self):
        self.ring.add("kill", victim="Goblin")
        self.r.sync()
        self.ring.add("kill", victim="Ogre")
        self.assertEqual([g["victim"] for g in self.r.poll()], ["Ogre"])


class FactsTest(unittest.TestCase):
    # One record per kind the referee writes, shaped as Server/Referee.lua writes them (test_referee_lua.lua).
    RECORDS: ClassVar[list[dict]] = [
        {"ev": "trade_open", "npc": "Arron", "npc_uuid": "e1fd", "char": "Tav"},
        {
            "ev": "trade_end",
            "npc": "Arron",
            "sold": [{"name": "Tablet", "count": 1}],
            "bought": [],
            "gold_in": 5,
            "gold_out": 0,
        },
        {"ev": "gold", "gold": 105, "delta": 5, "holder": "Tav"},
        {
            "ev": "item_added",
            "name": "Gold",
            "count": 5,
            "gold": True,
            "tpl": "1c3c",
            "from": "Goblin",
            "add_type": "Regular",
        },
        {"ev": "pickpocket", "ok": False, "npc": "Arron", "char": "Tav"},
        {"ev": "pickpocket", "ok": True, "npc": "Arron", "item": "Ring", "count": 1},
        {"ev": "stole", "char": "Tav", "item": "Ring", "tpl": "dddd", "owner": "Arron"},
        {"ev": "crime", "type": "PickPocketFailed", "id": 126, "victim": "Arron", "criminal": "Tav"},
        {"ev": "kill", "victim": "Goblin", "killer": "Tav", "owner": "Tav", "by_party": True, "trader": False},
        {"ev": "died", "name": "Goblin", "party": False, "uuid": "b1e2"},
        {"ev": "loot", "from": "corpse", "item": "Gold", "src": "Goblin"},
        {"ev": "combat_started", "combat": "d24e"},
        {"ev": "combat_ended", "combat": "d24e"},
    ]

    def test_every_record_is_a_valid_fact(self):
        ring = FakeRing()
        for rec in self.RECORDS:
            ring.add(**rec)
        facts = [referee.to_fact(r) for r in referee.referee(ring, wall=lambda: 1_800_000_000.0).poll()]
        self.assertEqual(len(facts), len(self.RECORDS))
        for f in facts:
            jsonschema.validate(f, FACTS_SCHEMA)
            self.assertNotIn("seq", f)
            self.assertEqual(f["gt"], 12.5)

    def test_a_bad_record_fails_the_schema(self):
        bad = referee.to_fact({"seq": 1, "t": 1.0, "ev": "pickpocket", "npc": "Arron"})  # no ok
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate(bad, FACTS_SCHEMA)

    def test_selfcheck(self):
        snap = {"fired": {"KilledBy/4": 3, "CharacterStoleItem/10": 0, "AddedTo/3": 9}}
        self.assertEqual(
            referee.selfcheck(snap), {"fired": {"AddedTo/3": 9, "KilledBy/4": 3}, "never": ["CharacterStoleItem/10"]}
        )


class RollsTest(unittest.TestCase):
    def test_roll_rows(self):
        ring = FakeRing()
        ring.add(
            "",
            roller="14e6",
            party=True,
            success=True,
            natural=14,
            dc=10,
            srcs=["finished", "osi"],
            streams={"Generic": 2},
            crit=None,
        )
        reader = referee.rolls(ring, wall=lambda: 100.0)
        rows = [referee.to_roll(r) for r in reader.poll()]
        self.assertEqual(ring.calls[0]["op"], "roll.since")
        self.assertEqual(rows[0]["v"], 1)
        self.assertEqual((rows[0]["natural"], rows[0]["dc"], rows[0]["streams"]), (14, 10, {"Generic": 2}))
        self.assertNotIn("crit", rows[0])
        self.assertNotIn("seq", rows[0])


def hidden_op_offenders(src: Path) -> list[str]:
    """Every module under `src` (subpackages too) except the top-level referee.py that names a hidden op or
    imports referee."""
    offenders = []
    for path in sorted(src.rglob("*.py")):
        if path == src / "referee.py":
            continue
        text = path.read_text()
        rel = path.relative_to(src).as_posix()
        for op in HIDDEN_OPS:
            if re.search(r"""["']""" + re.escape(op), text):
                offenders.append(f"{rel}: {op}")
        if re.search(r"\breferee\b", text) and re.search(r"import[^\n]*\breferee\b", text):
            offenders.append(f"{rel}: imports referee")
    return offenders


class FairnessTest(unittest.TestCase):
    """The hidden rings reach the harness only: no other bridge module names their ops or imports this reader, so no
    MCP tool can return them. (A tool that needs them is a fairness bug: route it through the harness instead.)"""

    def test_no_bridge_module_reaches_the_hidden_ops(self):
        self.assertEqual(hidden_op_offenders(BRIDGE / "src" / "bg3ai"), [])

    def test_the_scan_reaches_subpackages(self):
        # bg3ai.tools.* holds the MCP tools: a planted offender there must be caught, as one at the top level is
        with tempfile.TemporaryDirectory() as td:
            src = Path(td)
            (src / "tools").mkdir()
            (src / "referee.py").write_text('OP = "ref.since"\n')
            (src / "tools" / "referee.py").write_text('OP = "roll.since"\n')
            (src / "tools" / "sneaky.py").write_text('from bg3ai import referee\nx = game.rpc("ref.since", {})\n')
            self.assertEqual(
                hidden_op_offenders(src),
                ["tools/referee.py: roll.since", "tools/sneaky.py: ref.since", "tools/sneaky.py: imports referee"],
            )

    def test_no_tool_is_named_for_them(self):
        from bg3ai.tools import core

        names = [t.name for t in core.mcp._tool_manager.list_tools()]
        self.assertTrue(names)
        self.assertEqual([n for n in names if re.match(r"(ref_|referee|scn_|scenario|roll_log|rolls)", n)], [])


if __name__ == "__main__":
    unittest.main()

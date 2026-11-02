"""Offline tests for tools/dev/make_arena_save.py: the pairs spec (arena-mirror.v1) and the symmetry check against a
fake rpc. Run: uv run pytest -q tools/dev/test_make_arena_save.py"""

from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import make_arena_save as mas

SPEC: dict[str, Any] = {
    "schema": "arena-mirror.v1",
    "name": "mirror_crash_site",
    "level": 4,
    "location": {"level": "WLD_Main_A", "spawns": {"A": [229.8, 5.1, 268.2], "B": [229.8, 5.6, 278.7]}},
    "pairs": [
        {"class": "Fighter", "template": None, "names": {"A": "Ash", "B": "Birch"}, "items": []},
        {"class": "Barbarian", "template": None, "names": {"A": "Alder", "B": "Bramble"}},
        {"class": "Rogue", "template": "Hireling_Rogue_x", "names": {"A": "Aspen", "B": "Beech"}, "items": ["WPN_X"]},
        {"class": "Sorcerer", "template": None, "names": {"A": "Acer", "B": "Briar"}},
    ],
}


def spec(**over: Any) -> dict[str, Any]:
    s = copy.deepcopy(SPEC)
    s.update(over)
    return s


class SpecTest(unittest.TestCase):
    def test_valid_spec_with_null_templates(self):
        self.assertIs(mas.check_spec(spec())["name"], "mirror_crash_site")

    def test_building_needs_every_template(self):
        with self.assertRaisesRegex(mas.SpecError, "pair 1: no template yet"):
            mas.check_spec(spec(), need_templates=True)

    def test_each_problem_is_named(self):
        bad: list[tuple[dict[str, Any], str]] = [
            (spec(schema="arena.v1"), "schema must be"),
            (spec(name="Mirror"), "name must be"),
            (spec(level=0), "level must be"),
            (spec(level=True), "level must be"),
            (spec(location={"level": "WLD_Main_A", "spawns": {"A": [1, 2, 3]}}), "location.spawns.B"),
            (spec(pairs=SPEC["pairs"][:3]), "pairs must list 4"),
        ]
        druid = spec()
        druid["pairs"][3]["class"] = "Druid"  # wildshape (Halsin broke q6)
        bad.append((druid, "pair 4: class must be one of"))
        twice = spec()
        twice["pairs"][2]["names"]["B"] = "ASH"
        bad.append((twice, "pair 3: the name ASH is used twice"))
        lower = spec()
        lower["pairs"][0]["names"]["A"] = "ash"
        bad.append((lower, "pair 1: names.A must be a display name"))
        items = spec()
        items["pairs"][1]["items"] = [""]
        bad.append((items, "pair 2: items must list"))
        for s, want in bad:
            with self.subTest(want=want), self.assertRaisesRegex(mas.SpecError, want):
                mas.check_spec(s)

    def test_load_spec_reads_a_file(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "s.json"
            p.write_text(json.dumps(SPEC))
            self.assertEqual(mas.load_spec(p)["level"], 4)
            p.write_text("{")
            with self.assertRaisesRegex(mas.SpecError, "cannot read"):
                mas.load_spec(p)

    def test_arena_teams_pair_i_at_index_i(self):
        uu = {"A": ["a1", "a2", "a3", "a4"], "B": ["b1", "b2", "b3", "b4"]}
        t = mas.arena_teams(spec(), uu)
        self.assertEqual(t["A"][0], {"name": "Ash", "uuid": "a1"})
        self.assertEqual(t["B"][3], {"name": "Briar", "uuid": "b4"})
        with self.assertRaisesRegex(mas.SpecError, "team B needs 4"):
            mas.arena_teams(spec(), {"A": uu["A"], "B": uu["B"][:3]})


class SymmetryTest(unittest.TestCase):
    def test_ok_passes_the_answer_through(self):
        sent: list[tuple[str, dict[str, Any]]] = []

        def rpc(op: str, args: dict[str, Any]) -> Any:
            sent.append((op, args))
            return {"ok": True, "pairs": [{}, {}], "problems": []}

        self.assertTrue(mas.check_symmetry(rpc, [("a1", "b1"), ("a2", "b2")])["ok"])
        self.assertEqual(sent, [("arena.symmetry", {"pairs": [["a1", "b1"], ["a2", "b2"]]})])

    def test_any_problem_refuses(self):
        def rpc(op: str, args: dict[str, Any]) -> Any:
            return {"ok": False, "problems": ["pair 1 (Ash, Birch): race differs", "Birch: ac doesn't read"]}

        with self.assertRaisesRegex(mas.AsymmetryError, r"race differs; Birch: ac doesn't read"):
            mas.check_symmetry(rpc, [("a1", "b1")])

    def test_an_odd_answer_refuses(self):
        with self.assertRaisesRegex(mas.AsymmetryError, "answered None"):
            mas.check_symmetry(lambda op, a: None, [("a1", "b1")])

    def test_cli_validate_and_bad_pair_argument(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "s.json"
            p.write_text(json.dumps(SPEC))
            self.assertEqual(mas.main(["validate", str(p)]), 0)
            p.write_text(json.dumps(spec(level=99)))
            self.assertEqual(mas.main(["validate", str(p)]), 1)
        with self.assertRaises(SystemExit):
            mas.main(["symmetry", "a1b1"])


if __name__ == "__main__":
    unittest.main()

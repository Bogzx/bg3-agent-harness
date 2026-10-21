import unittest

from bg3play.memory import memlib, snapshots

from tests.memory._testutil import MemoryTestCase

GUID_A = "11111111-2222-3333-4444-555555555555"
GUID_B = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"

STATUS = {
    "region": "Moonrise Towers",
    "subregion": "First Floor",
    "level": "SCL_Main_A",
    "gold": 412,
    "active": GUID_A,
    "in_combat": False,
    "party": [
        {
            "uuid": GUID_A,
            "name": "Tav",
            "hp": 31,
            "max_hp": 38,
            "level": 7,
            "classes": [{"name": "Cleric", "level": 7}],
            "pos": [1.0, 2.0, 3.0],
            "statuses": ["BLESSED"],
        },
        {"uuid": GUID_B, "name": "Lae'zel", "hp": 49, "max_hp": 70, "level": 7, "downed": True},
    ],
}


def fake_rpc(table):
    def rpc(op, args):
        v = table[op]
        if isinstance(v, Exception):
            raise v
        return v(args) if callable(v) else v

    return rpc


class RenderTest(unittest.TestCase):
    def test_status_has_no_guids_or_handles(self):
        out = snapshots.render_status(STATUS)
        self.assertIn("Moonrise Towers", out)
        self.assertIn("Tav", out)
        self.assertIn("HP 31/38", out)
        self.assertIn("downed", out)
        self.assertNotRegex(out, memlib.UUID_LIKE)
        self.assertNotRegex(out, r"\be\d+\b")

    def test_inventory_grouped_by_owner_name(self):
        inv = [
            {"uuid": GUID_B, "name": "Potion of Healing", "count": 3, "owner": GUID_A},
            {"uuid": GUID_A, "name": "Longsword", "equipped_slot": "MainHand", "owner": GUID_B},
        ]
        out = snapshots.render_inventory(inv, {GUID_A: "Tav", GUID_B: "Lae'zel"})
        self.assertIn("## Tav", out)
        self.assertIn("## Lae'zel", out)
        self.assertIn("Potion of Healing", out)
        self.assertNotRegex(out, memlib.UUID_LIKE)

    def test_inventory_unknown_owner_guid_hidden(self):
        out = snapshots.render_inventory([{"name": "Rope", "owner": GUID_B}], {})
        self.assertIn("## other", out)
        self.assertNotRegex(out, memlib.UUID_LIKE)

    def test_journal(self):
        out = snapshots.render_journal(
            [{"title": "Save the Refugees", "steps": ["Kill the leaders"]}, {"title": "Old quest", "done": True}]
        )
        active, done = out.split("## Done")
        self.assertIn("Save the Refugees", active)
        self.assertIn("  - Kill the leaders", active)
        self.assertIn("Old quest", done)

    def test_journal_shows_last_steps_only(self):
        out = snapshots.render_journal([{"title": "Long", "steps": [f"step {i}" for i in range(6)]}])
        self.assertIn("(+3 earlier entries", out)
        self.assertIn("step 5", out)
        self.assertNotIn("step 2", out)

    def test_class_shape_from_mod(self):
        st = dict(
            STATUS,
            party=[
                {
                    "uuid": GUID_A,
                    "name": "Tav",
                    "hp": 1,
                    "max_hp": 2,
                    "classes": [{"class": "Ranger", "subclass": "Hunter", "level": 6}],
                }
            ],
        )
        out = snapshots.render_status(st)
        self.assertIn("Ranger 6 (Hunter)", out)
        self.assertNotIn("{", out)

    def test_cosmetic_items_skipped(self):
        out = snapshots.render_inventory(
            [
                {"name": "Hat", "equipped_slot": "VanityBody", "owner": GUID_A},
                {"name": "Sword", "equipped_slot": "MeleeMainHand", "owner": GUID_A},
            ],
            {GUID_A: "Tav"},
        )
        self.assertNotIn("Hat", out)
        self.assertIn("Sword [equipped MeleeMainHand]", out)

    def test_map(self):
        out = snapshots.render_map({"region": "Moonrise", "waypoints": [{"id": "w1", "name": "Last Light Inn"}]})
        self.assertIn("- Last Light Inn", out)
        self.assertIn("Current: Moonrise", out)

    def test_unexpected_shapes_fall_back(self):
        self.assertTrue(snapshots.render_journal("weird").strip())
        self.assertTrue(snapshots.render_map(["x"]).strip())
        self.assertTrue(snapshots.render_inventory({"stuff": 1}).strip())


class CollectTest(unittest.TestCase):
    def test_all_ops(self):
        table = {
            "state.status": STATUS,
            "state.character": lambda a: {"uuid": a["uuid"], "ac": 18, "abilities": {"WIS": 18}},
            "state.inventory": {
                "items": [{"name": "Rope", "owner": GUID_A, "uuid": GUID_B + "x"}],
                "supplies": 12,
                "members": {},
            },
            "state.journal": [{"title": "Q"}],
            "state.map": {"region": "R", "waypoints": []},
        }
        docs, notes = snapshots.collect(fake_rpc(table), set(table))
        self.assertEqual(set(docs), set(snapshots.SNAP_FILES))
        self.assertEqual(notes, [])
        self.assertIn("## Tav", docs["party"])
        self.assertIn("- ac: 18", docs["party"])
        self.assertNotRegex("".join(docs.values()), memlib.UUID_LIKE)

    def test_gear_check_written(self):
        inv = {
            "items": [
                {
                    "uuid": "s1",
                    "name": "Sentinel Shield",
                    "owner": GUID_A,
                    "slot_type": "MeleeOffHand",
                    "ac": 2,
                    "shield": True,
                    "rarity": "rare",
                },
                {"uuid": "c1", "name": "Cup", "owner": GUID_A, "cat": "JunkArtificial", "tab": "Misc", "value": 1},
            ],
            "supplies": 27,
            "members": {GUID_A: {"prof": ["Shields"]}},
        }
        out = snapshots.render_gear(inv, {GUID_A.lower(): "Tav"})
        self.assertIn("- Tav: equip Sentinel Shield in MeleeOffHand", out)
        self.assertIn("junk to sell: 1 items", out)
        self.assertIn("camp supplies: 27/40 carried", out)
        self.assertIn("nothing to change", snapshots.render_gear({"items": [], "members": {}}))

    def test_missing_ops_degrade(self):
        docs, notes = snapshots.collect(fake_rpc({"state.status": STATUS}), {"state.status"})
        self.assertEqual(set(docs), {"status"})
        self.assertTrue(any("state.character not implemented" in n for n in notes))
        self.assertTrue(any(n.startswith("journal: skipped") for n in notes))

    def test_failing_op_is_skipped_not_fatal(self):
        table = {"state.status": RuntimeError("boom"), "state.map": {"region": "R"}}
        docs, notes = snapshots.collect(fake_rpc(table), set(table))
        self.assertEqual(set(docs), {"map"})
        self.assertTrue(any("boom" in n for n in notes))


class WriteTest(MemoryTestCase):
    def test_write_with_header_and_session(self):
        self.open_session(4)
        written = snapshots.write_snapshots(self.mem, {"status": "WHERE x\n"}, snapshots.current_session(self.mem))
        self.assertEqual(written, ["status.md"])
        text = self.read("snapshots/status.md")
        self.assertTrue(text.startswith("<!-- generated by bg3ai snapshots.py"))
        self.assertIn("session S004", text)

    def test_unreachable_game_writes_nothing(self):
        written, notes = snapshots.run(self.mem, port=1)  # nothing listens on port 1
        self.assertEqual(written, [])
        self.assertIn("not reachable", notes[0])
        self.assertFalse((self.mem / "snapshots" / "status.md").exists())


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""runner/facts.py: facts.jsonl v1 from progress.read_game-shaped reads (status/journal/ui/explored/events, as the
mod's state.status, state.journal and events.since answer) and state.inventory replies.

    python3 -m unittest runner/test_facts.py
"""

from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bridge" / "src"))
import facts as F

SCHEMA = Path(__file__).resolve().parents[1] / "packages" / "harness" / "schemas" / "facts.v1.schema.json"


def member(name, level=1, hp=10, **kw):
    return {
        "uuid": f"{name}_0000",
        "name": name,
        "hp": hp,
        "max_hp": 12,
        "level": level,
        "pos": [1, 2, 3],
        "statuses": [],
        "region": "TUT_Avernus_C",
        **kw,
    }


READ = {
    "status": {
        "party": [member("Tav", active=True), member("Shadowheart")],
        "level": "TUT_Avernus_C",
        "region": "Nautiloid",
        "in_combat": False,
        "gold": 10,
        "camp": [],
    },
    "journal": {"quests": [{"title": "Escape the Nautiloid", "steps": ["Find a way out"], "done": False}]},
    "map": None,
    "markers": None,
    "explored": {"subregion": "Ruined Battlefield"},
    "events": None,
    "ui": {"screen": "game", "region": "Ruined Battlefield"},
}
INV = {
    "rev": 7,
    "gold": 10,
    "items": [
        {"uuid": "a", "name": "Gold", "owner": "Tav_0000", "count": 10, "gold": True},
        {"uuid": "b", "name": "Torch", "owner": "Tav_0000"},
    ],
}


class FakeRing:
    """ref.since / roll.since as the mod answers them: the ring holds records from before the run (seq 1-3)."""

    def __init__(self):
        self.ref = [{"seq": n, "t": 100 * n, "ev": "crime", "type": "old"} for n in (1, 2, 3)]
        self.rolls = [{"seq": 1, "t": 50, "roller": "old"}]
        self.fail = False

    def __call__(self, op, args):
        if self.fail:
            raise OSError("no game")
        ring = self.ref if op == "ref.since" else self.rolls
        evs = [r for r in ring if r["seq"] > args["seq"]][: args["limit"]]
        return {"boot": "b1", "now": 10_000, "first": 1, "latest": ring[-1]["seq"], "events": evs}


class Facts(unittest.TestCase):
    def setUp(self):
        self.path = Path(tempfile.mkdtemp(prefix="bg3ai-facts-")) / "facts.jsonl"
        self.addCleanup(lambda: (self.path.unlink(missing_ok=True), self.path.parent.rmdir()))
        self.w = F.FactsWriter(self.path, snapshot_s=300)

    def records(self) -> list[dict]:
        return [json.loads(ln) for ln in self.path.read_text().splitlines()]

    def play(self) -> None:
        self.w.mark("run_start", {"playthrough": "x"}, now=1000.0)
        self.w.observe(READ, INV, now=1010.0)  # baseline: a snapshot, no events
        r = copy.deepcopy(READ)
        st = r["status"]
        st["in_combat"], st["gold"] = True, 35
        st["party"][1].update(downed=True, hp=0)
        st["party"].append(member("Lae'zel", level=2))
        r["events"] = [
            {"seq": 5, "kind": "died", "data": {"uuid": "g1", "name": "Imp"}},
            {"seq": 6, "kind": "died", "data": {"name": "Shadowheart"}},
            {"seq": 7, "kind": "dialogue_started", "data": {}},
        ]
        r["journal"]["quests"].append({"title": "Rescue the Illithid's Captive", "steps": ["a"], "done": True})
        r["ui"]["region"] = "Helm"
        inv = copy.deepcopy(INV)
        inv["items"][0]["count"] = 35
        inv["items"].append({"uuid": "c", "name": "Scimitar", "owner": "Tav_0000"})
        self.w.observe(r, inv, now=1025.0)
        r2 = copy.deepcopy(r)
        r2["status"]["in_combat"] = False
        r2["status"]["party"][1].update(downed=False, hp=4)
        r2["status"]["party"][0]["level"] = 3
        r2["status"]["region"] = "Ravaged Beach"
        r2["events"] = []
        self.w.observe(r2, {"unchanged": True, "rev": 8, "gold": 35}, now=1400.0)  # a cached reply: no items
        self.w.mark("run_end", {"outcome": "reached"}, now=1500.0)

    def test_records(self):
        self.play()
        recs = self.records()
        self.assertEqual([r["kind"] for r in recs][:2], ["mark", "snapshot"])
        self.assertTrue(all(r["v"] == 1 and "game_t" in r for r in recs))
        self.assertEqual([r["game_t"] for r in recs if r["kind"] == "mark"], [0.0, 500.0])
        evs = [
            {k: v for k, v in r.items() if k not in ("v", "kind", "t", "game_t")} for r in recs if r["kind"] == "event"
        ]
        self.assertEqual(
            evs,
            [
                {"ev": "area", "name": "Helm"},
                {"ev": "combat_started"},
                {"ev": "downed", "name": "Shadowheart", "party": True},
                {"ev": "joined", "name": "Lae'zel"},
                {"ev": "gold", "gold": 35, "delta": 25},
                {"ev": "item_added", "name": "Gold", "count": 25, "gold": True},
                {"ev": "item_added", "name": "Scimitar", "count": 1},
                {"ev": "quest_added", "title": "Rescue the Illithid's Captive"},
                {"ev": "quest_done", "title": "Rescue the Illithid's Captive"},
                {"ev": "died", "name": "Imp", "party": False},
                {"ev": "region", "name": "Ravaged Beach"},
                {"ev": "combat_ended"},
                {"ev": "level_up", "name": "Tav", "level": 2},
                {"ev": "level_up", "name": "Tav", "level": 3},
                {"ev": "revived", "name": "Shadowheart", "party": True},
            ],
        )
        snaps = [r for r in recs if r["kind"] == "snapshot"]
        self.assertEqual(len(snaps), 2)  # the first read, then 300 s later
        last = snaps[-1]
        self.assertEqual(last["areas"], ["Ruined Battlefield", "Helm"])
        self.assertEqual(last["quests"], {"Escape the Nautiloid": False, "Rescue the Illithid's Captive": True})
        self.assertEqual(len(last["inv"]), 3)  # the cached reply kept the last items
        self.assertEqual(
            last["party"][0], {"name": "Tav", "level": 3, "dead": False, "downed": False, "hp": 10, "max_hp": 12}
        )
        ts = [r["t"] for r in recs]
        self.assertEqual(ts, sorted(ts))

    def test_load_forces_a_snapshot_and_a_new_process_keeps_time_zero(self):
        self.play()
        w = F.FactsWriter(self.path)
        self.assertEqual(w.t0, 1000.0)
        w.mark("load", "Tav-1__S002", now=1600.0)
        w.observe(READ, INV, now=1601.0)  # a new process: baseline only
        self.assertEqual([r["kind"] for r in self.records()][-2:], ["mark", "snapshot"])
        self.assertEqual(self.records()[-1]["game_t"], 601.0)

    def test_no_session_and_failed_reads(self):
        self.assertEqual(self.w.observe(None, None, now=1.0), [])
        self.assertEqual(self.w.observe({"status": None, "ui": {"screen": "credits"}}, None, now=2.0), [])
        self.w.observe(READ, None, now=3.0)  # no inventory read yet: []
        out = self.w.observe(READ, INV, now=4.0)  # its first read isn't new items
        self.assertEqual([r["kind"] for r in out], [])
        self.assertIsNone(self.records()[0]["game_t"])  # before run_start: null

    def test_read_inventory(self):
        self.assertEqual(
            F.read_inventory(lambda c, ctx: json.dumps([{"ok": True, "result": INV}]), lambda ops: ""), INV
        )
        self.assertIsNone(F.read_inventory(lambda c, ctx: json.dumps([{"ok": False}]), lambda ops: ""))
        self.assertIsNone(F.read_inventory(lambda c, ctx: 1 / 0, lambda ops: ""))

    def test_ring_feed(self):
        ring = FakeRing()
        feed = F.RingFeed(ring, self.w, self.path.with_name("rolls.jsonl"), wall=lambda: 2000.0)
        self.w.mark("run_start", now=1000.0)
        self.assertEqual(feed.poll(), (0, 0))  # synced: older ring records aren't this run's
        self.assertEqual(self.w.referee_live, True)
        ring.ref.append({"seq": 4, "t": 9_000, "ev": "trade_end", "npc": "Arron", "sold": [], "gold_in": 5})
        ring.ref.append({"seq": 5, "t": 9_500, "ev": "gold", "gold": 40, "delta": 5, "holder": None})
        ring.rolls.append({"seq": 2, "t": 9_800, "roller": "Tav", "party": True, "success": True, "natural": 17})
        self.w.observe(READ, INV, now=2005.0)
        self.assertEqual(feed.poll(), (2, 1))
        # the poll diff no longer writes gold while the referee answers
        r = copy.deepcopy(READ)
        r["status"]["gold"] = 40
        self.assertEqual([x["ev"] for x in self.w.observe(r, INV, now=2010.0)], [])
        recs = self.records()
        ev = [x for x in recs if x.get("ev") in ("trade_end", "gold")]
        self.assertEqual([x["ev"] for x in ev], ["trade_end", "gold"])
        self.assertEqual(ev[0]["t"], 2005.0)  # older than the last record: kept in order
        self.assertEqual(ev[0]["at"], 1999.0)  # its own time (mod now 10 s, record 9 s)
        self.assertEqual(ev[0]["game_t"], 1005.0)
        self.assertNotIn("holder", ev[1])
        rolls = [json.loads(x) for x in self.path.with_name("rolls.jsonl").read_text().splitlines()]
        self.assertEqual(rolls, [{"v": 1, "t": 1999.8, "roller": "Tav", "party": True, "success": True, "natural": 17}])
        self.path.with_name("rolls.jsonl").unlink()
        ring.fail = True  # an old mod / a loading screen
        self.assertEqual(feed.poll(), (0, 0))
        self.assertFalse(self.w.referee_live)
        r["status"]["gold"] = 50
        self.assertEqual([x["ev"] for x in self.w.observe(r, INV, now=2100.0)], ["gold"])

    def test_schema(self):
        try:
            import jsonschema
        except ImportError:
            self.skipTest("jsonschema not installed (uv run --with jsonschema python -m unittest runner/test_facts.py)")
        self.play()
        self.w.mark("settings", {"resolution": "1920x1080"}, now=1501.0)
        self.w.timed_events(
            [
                {
                    "t": 1400.0,
                    "ev": "choice",
                    "scene": "Shadowheart: Allow me.",
                    "options": [{"i": 1, "text": "Go on."}],
                    "picked": 1,
                    "reason": "trust her",
                }
            ]
        )
        v = jsonschema.Draft202012Validator(json.loads(SCHEMA.read_text()))
        for r in self.records():
            v.validate(r)


if __name__ == "__main__":
    unittest.main()

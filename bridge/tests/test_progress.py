"""Offline tests: story progress events, weights, timeline, stuck signal, summary (no game needed).

Run: cd bridge && uv run python -m unittest discover -s tests
"""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from bg3ai import progress as P


def world() -> dict:
    return {
        "status": {"region": "Nautiloid", "party": [{"name": "Claude", "level": 1}]},
        "ui": {"screen": "game", "region": "Ship Hold"},
        "map": {"region": "Nautiloid", "waypoints": []},
        "journal": [{"title": "Escape the Nautiloid", "steps": ["Find the helm."]}],
        "markers": {"markers": [{"label": "Find the helm", "quest": "Escape the Nautiloid"}]},
    }


def kinds(evs: list[dict]) -> list[str]:
    return [e["kind"] for e in evs]


class Diff(unittest.TestCase):
    def test_baseline_is_silent(self):
        seen, evs = P.diff(None, world())
        self.assertEqual(evs, [])
        self.assertTrue(seen["baseline"])
        self.assertEqual(seen["regions"], ["Nautiloid"])

    def test_events_and_weights(self):
        seen, _ = P.diff(None, world())
        w = world()
        w["status"]["region"] = "Wilderness"
        w["ui"]["region"] = "Roadside Cliffs"
        w["status"]["party"] = [{"name": "Claude", "level": 3}, {"name": "Shadowheart", "level": 2}]
        w["map"]["waypoints"] = [{"id": 1, "name": "Roadside Cliffs"}]
        w["journal"] = [{"title": "Escape the Nautiloid", "steps": ["Find the helm.", "Reach the helm."], "done": True},
                        {"title": "Find a Cure", "steps": ["Find a healer."]}]
        w["markers"] = {"markers": [{"label": "Find a healer", "quest": "Find a Cure"}]}
        seen, evs = P.diff(seen, w)
        self.assertEqual(sorted(kinds(evs)), sorted(["region", "area", "level_up", "level_up", "companion", "waypoint",
                                                     "quest_step", "quest_done", "quest_new", "objective"]))
        self.assertEqual(sum(e["points"] for e in evs), 15 + 3 + 8 * 2 + 5 + 5 + 2 + 10 + 3 + 2)
        self.assertIn("Claude -> level 3", [e["what"] for e in evs])
        self.assertIn("Escape the Nautiloid: Reach the helm.", [e["what"] for e in evs])

    def test_reload_does_not_score_twice(self):
        seen, _ = P.diff(None, world())
        later = world()
        later["journal"][0]["steps"].append("Reach the helm.")
        later["markers"] = {"markers": []}
        seen, evs = P.diff(seen, later)
        self.assertEqual(sorted(kinds(evs)), ["objective", "quest_step"])
        seen, evs = P.diff(seen, world())  # load an earlier save: marker back, step gone
        self.assertEqual(evs, [])
        _, evs = P.diff(seen, later)  # and redo it
        self.assertEqual(evs, [])

    def test_missing_parts_do_not_score_or_clear(self):
        seen, _ = P.diff(None, world())
        w = world()
        w["journal"] = None
        w["markers"] = None
        w["ui"] = {"screen": "dialogue", "region": "Somewhere Else"}  # not a minimap read
        seen2, evs = P.diff(seen, w)
        self.assertEqual(evs, [])
        self.assertEqual(seen2["objectives_active"], seen["objectives_active"])

    def test_same_subregion_name_in_another_region_counts(self):
        seen, _ = P.diff(None, world())
        w = world()
        w["status"]["region"] = "Wilderness"
        _, evs = P.diff(seen, w)
        self.assertEqual(sorted(kinds(evs)), ["area", "region"])


class TimelineTest(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp(prefix="bg3ai-prog-"))

    def test_records_progress_and_persists_seen(self):
        tl = P.Timeline(self.d / "S001.progress.jsonl", self.d / "state.json", started=1000.0)
        tl.observe(world(), now=1015)
        w = world()
        w["journal"].append({"title": "Find a Cure", "steps": ["x"]})
        evs = tl.observe(w, now=1030)
        self.assertEqual(kinds(evs), ["quest_new"])
        recs = P.load(self.d / "S001.progress.jsonl")
        self.assertEqual([r["type"] for r in recs], ["poll", "progress"])
        # a new session of the same playthrough starts from the saved seen-state: nothing re-scored
        tl2 = P.Timeline(self.d / "S002.progress.jsonl", self.d / "state.json", started=2000.0)
        self.assertEqual(tl2.observe(w, now=2015), [])
        self.assertIsNone(tl2.observe(None, now=2030) or None)
        self.assertEqual(P.load(self.d / "S002.progress.jsonl")[-1], {"type": "poll", "t": 2030, "ok": False})

    def test_stuck_and_unstuck(self):
        tl = P.Timeline(self.d / "S.jsonl", self.d / "st.json", started=0.0)
        tl.observe(world(), now=10)
        busy = lambda t: 25  # noqa: E731
        idle = lambda t: 2  # noqa: E731
        tl.observe(world(), now=P.STUCK_MIN * 60 + 5, actions_since=idle)
        self.assertIsNone(tl.stuck_since, "an agent that isn't acting isn't stuck")
        tl.observe(world(), now=P.STUCK_MIN * 60 + 20, actions_since=busy)
        self.assertEqual(tl.stuck_since, P.STUCK_MIN * 60 + 20)
        tl.observe(world(), now=P.STUCK_MIN * 60 + 35, actions_since=busy)  # no second stuck record
        w = world()
        w["map"]["waypoints"] = ["Ship"]
        tl.observe(w, now=P.STUCK_MIN * 60 + 120)
        self.assertIsNone(tl.stuck_since)
        recs = P.load(self.d / "S.jsonl")
        self.assertEqual([r["type"] for r in recs if r["type"] != "poll"], ["stuck", "progress", "unstuck"])
        stuck = next(r for r in recs if r["type"] == "stuck")
        self.assertEqual(stuck["actions"], 25)

        s = P.summarize(recs, 0.0, P.STUCK_MIN * 60 + 180)
        self.assertEqual(s["points"], 5)
        self.assertEqual(s["stuck_episodes"], 1)
        self.assertAlmostEqual(s["stuck_min"], (P.STUCK_MIN * 60 + 120) / 60, places=1)
        self.assertAlmostEqual(s["longest_no_progress_min"], (P.STUCK_MIN * 60 + 120) / 60, places=1)

    def test_actions_in_counts_top_level_calls(self):
        m = self.d / "m.jsonl"
        m.write_text("\n".join(json.dumps(r) for r in [
            {"type": "tool", "tool": "move_to", "t0": 5}, {"type": "tool", "tool": "look", "t0": 15},
            {"type": "tool", "tool": "move_to", "t0": 16, "nested": True}, {"type": "sample", "t": 17}]) + "\n")
        self.assertEqual(P.actions_in(m)(10), 1)
        self.assertIsNone(P.actions_in(self.d / "nope")(0))


class SummaryAndLive(unittest.TestCase):
    RECS = [{"type": "progress", "t": 600.0, "kind": "quest_new", "what": "A", "points": 3},
            {"type": "progress", "t": 1200.0, "kind": "region", "what": "Wilderness", "points": 15},
            {"type": "stuck", "t": 2400.0, "since": 1200.0, "idle_min": 20, "actions": 30}]

    def test_summary(self):
        s = P.summarize(self.RECS, 0.0, 3600.0)
        self.assertEqual(s["points"], 18)
        self.assertEqual(s["per_hour"], 18.0)
        self.assertEqual(s["by_kind"], {"quest_new": 1, "region": 1})
        self.assertEqual(s["longest_no_progress_min"], 40.0)
        self.assertEqual(s["stuck_episodes"], 1)
        self.assertEqual(s["stuck_min"], 40.0)  # open episode: counted to the end, from the last progress

    def test_live_view_and_history(self):
        v = P.live_view(self.RECS, 0.0, 1800.0)
        self.assertEqual(v["points"], 18)
        self.assertEqual(v["per_hour"], 36.0)
        self.assertTrue(v["stuck"])
        self.assertEqual(v["since_progress_min"], 10.0)
        self.assertEqual(v["recent"][0]["what"], "Wilderness")
        d = Path(tempfile.mkdtemp())
        P.write_summary(P.summarize(self.RECS, 0.0, 3600.0), d / "S1.progress.json", d / "hist.jsonl", "S001")
        row = json.loads((d / "hist.jsonl").read_text())
        self.assertEqual((row["session"], row["points"]), ("S001", 18))
        self.assertNotIn("last", row)


class ReadGame(unittest.TestCase):
    def test_two_evals(self):
        calls = []
        w = world()

        def lua_eval(code: str, ctx: str) -> str:
            calls.append((code, ctx))
            if ctx == "server":
                return json.dumps([{"ok": True, "result": w["status"]}, {"ok": True, "result": w["journal"]},
                                   {"ok": False, "error": "x"}, {"ok": True, "result": w["markers"]}])
            return json.dumps([{"ok": True, "result": w["ui"]}])

        got = P.read_game(lua_eval)
        self.assertEqual([c for _, c in calls], ["server", "client"])
        self.assertIn('R([=[{"op":"state.status","args":{}}]=])', calls[0][0])
        self.assertIsNone(got["map"])
        self.assertEqual(got["ui"]["region"], "Ship Hold")
        self.assertEqual(P.diff(None, got)[1], [])

    def test_no_session(self):
        self.assertIsNone(P.read_game(lambda code, ctx: json.dumps([{"ok": False, "error": "no session"}] * 4)))

    def test_weights_documented(self):
        doc = P.__doc__
        for k, v in P.WEIGHTS.items():
            self.assertRegex(doc, rf"\b{k}\s+{v}\b")


if __name__ == "__main__":
    unittest.main()

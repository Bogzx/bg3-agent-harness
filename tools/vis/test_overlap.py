import json, sys, tempfile, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import overlap


def write_session(d: Path, steps: list[tuple]) -> Path:
    """steps: (tool, input, result text, think s, call s) -> S001.jsonl + S001.turns.jsonl in d; returns the stem."""
    stream, turns, t = [], [], 1000.0
    for i, (tool, inp, text, think, dur) in enumerate(steps):
        tid = f"toolu_{i}"
        t += think
        stream.append({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": tid, "name": f"mcp__bg3__{tool}", "input": inp}]}})
        turns.append({"type": "use", "t": t, "id": tid, "name": tool})
        t += dur
        stream.append({"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": tid, "content": [{"type": "text", "text": text}]}]}})
        turns.append({"type": "result", "t": t, "id": tid})
    for name, rows in (("S001.jsonl", stream), ("S001.turns.jsonl", turns)):
        (d / name).write_text("".join(json.dumps(r) + "\n" for r in rows))
    (d / "S001.fights.jsonl").write_text("")  # not a session: no S001.fights.turns.jsonl
    return d / "S001"


# naut2 run 1's transponder dash, shortened: a refused combat_turn, a refused use_ability, a read, then the retry
RUN = [
    ("travel_to", {"objective": "o1"}, "travel_to objective o1: arrived (30m)", 0.0, 6.0),
    ("get_map", {}, "No map change (Nautiloid; 1 objectives, 0 markers). full=true lists them.", 1.5, 0.1),
    ("combat_turn", {"plan": {"move": "o3"}},
     "FAILED: o3 is the objective marker 'Transponder', not something in view by that name: route(objective='o3')",
     2.0, 0.1),
    ("use_ability", {"ability": "Shout_Dash"}, "use_ability: FAILED — it is not this character's turn", 2.5, 0.1),
    ("note", {"text": "dash first"}, "noted", 1.0, 0.0),
    ("get_combat_state", {}, "ROUND 1 ...", 1.5, 0.1),
    ("use_ability", {"ability": "Shout_Dash", "who": "e1"}, "use_ability: done — Shout_Dash", 2.0, 0.5),
    ("combat_turn", {"plan": {}}, "TURN e1 (round 2) — waited 9.8s for the turn\n"
     "action: interact e127: FAILED — not available during combat → Shout_Dash: done", 1.0, 12.0),
    ("wait_for", {}, "walk: early wake, lead_s=2): plan the next step now", 2.0, 5.0),
    ("wait_for", {"lead_s": 0}, "walk: arrived", 2.2, 1.0),
    ("interact", {"target": "e56"}, "interact e56: climbed the ladder", 1.0, 4.0),
    ("travel_to", {"objective": "o2"}, "travel_to objective o2: started (40m)", 1.6, 0.2),
]


class Overlap(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.stem = write_session(Path(self.tmp.name), RUN)
        self.calls = overlap.load(self.stem)

    def tearDown(self):
        self.tmp.cleanup()

    def test_session_dir_lists_sessions_only(self):
        self.assertEqual(overlap.stems(self.tmp.name), [self.stem])
        self.assertEqual(overlap.stems(str(self.stem) + ".jsonl"), [self.stem])

    def test_refusal_is_the_calls_own_answer_not_a_step_with_a_fallback(self):
        self.assertTrue(self.calls[2]["refusal"].startswith("FAILED: o3 is the objective marker"))
        self.assertTrue(self.calls[3]["refusal"].startswith("FAILED: it is not this character's turn"))
        self.assertIsNone(self.calls[7]["refusal"])  # "action: …: FAILED → Shout_Dash: done" ran its fallback
        self.assertEqual(overlap.refusal_key("use_ability", self.calls[3]["refusal"]),
                         "use_ability: FAILED it is not this character's turn")
        self.assertIsNone(overlap.refusal({"text": "travel_to objective o1: arrived"}))
        self.assertEqual(overlap.refusal({"text": "travel_to objective Helm: REFUSED: the goal is 11m above you"}),
                         "REFUSED: the goal is 11m above you")

    def test_unneeded_turns(self):
        un = overlap.unneeded(self.calls)
        self.assertEqual([u["cat"] for u in un], ["no-change", "refusal", "re-wait", "chainable"])
        ep = un[1]
        # refused combat_turn + refused use_ability + the read; the note is independent work; closed by the retry
        self.assertEqual([c["name"] for c in ep["calls"]], ["combat_turn", "use_ability", "get_combat_state"])
        self.assertAlmostEqual(ep["think"], 2.0 + 2.5 + 1.5)
        self.assertEqual(ep["closed_by"], "use_ability")
        self.assertEqual(len(ep["refusals"]), 2)

    def test_overlap_by_tool(self):
        by, rows = overlap.overlap(self.calls)
        self.assertEqual(by["combat_turn"][2:4], [1, 12.0])   # one blocking call, 12 s
        self.assertEqual(by["combat_turn"][5], 0.0)            # enemy turns: the next decision needs them
        self.assertAlmostEqual(by["interact"][5], 1.6)          # climb tail: the trip was queueable
        self.assertAlmostEqual(by["travel_to"][4], 1.5)         # upper = next think

    def test_report_and_top_refusals(self):
        lines, un = overlap.report("t/S001", self.calls)
        self.assertIn("  unneeded model turns: 6 calls, 11.3 s model", "\n".join(lines))
        top = overlap.top_refusals({"r1": un, "r2": un})
        self.assertIn("    2     1 / 1   12.0s  combat_turn: FAILED #id is the objective marker", top[1])


if __name__ == "__main__":
    unittest.main()

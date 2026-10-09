"""Unit tests for the dashboard's pure helpers:  python3 -m unittest discover -s runner/dashboard"""

import json
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import feeds as F  # noqa: E402

GUID = "11111111-2222-3333-4444-555555555555"


class TmpCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="bg3dash-"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


class ReadNewTest(TmpCase):
    def test_missing_file(self):
        self.assertEqual(F.read_new(self.tmp / "nope", -1), (0, "", False))

    def test_initial_tail_starts_at_line_boundary(self):
        p = self.tmp / "a.log"
        p.write_text("".join(f"line {i}\n" for i in range(1000)))
        off, text, reset = F.read_new(p, -1, initial_bytes=100)
        self.assertTrue(reset)
        self.assertEqual(off, p.stat().st_size)
        lines = text.splitlines()
        self.assertEqual(lines[-1], "line 999")
        self.assertTrue(all(ln.startswith("line ") for ln in lines))  # no partial first line

    def test_incremental_and_partial_lines(self):
        p = self.tmp / "a.log"
        p.write_text("one\ntw")
        off, text, _ = F.read_new(p, 0)
        self.assertEqual((text, off), ("one\n", 4))
        with p.open("a") as f:
            f.write("o\nthree\n")
        off, text, reset = F.read_new(p, off)
        self.assertEqual(text, "two\nthree\n")
        self.assertFalse(reset)
        self.assertEqual(F.read_new(p, off), (off, "", False))

    def test_truncation_resets(self):
        p = self.tmp / "a.log"
        p.write_text("x" * 50 + "\n")
        off, _, _ = F.read_new(p, 0)
        p.write_text("new\n")
        off2, text, reset = F.read_new(p, off)
        self.assertTrue(reset)
        self.assertEqual(text, "new\n")
        self.assertEqual(off2, 4)

    def test_giant_line_does_not_stall(self):
        p = self.tmp / "a.log"
        p.write_text("y" * 5000)
        off, text, _ = F.read_new(p, 0, max_bytes=1000)
        self.assertEqual((off, text), (1000, ""))


class ActionsTest(unittest.TestCase):
    def test_parse(self):
        lines = "\n".join([
            json.dumps({"t": 1.0, "tool": "get_status", "args": {}, "ms": 65, "result": "WHERE x"}),
            "garbage",
            json.dumps({"t": 2.0, "tool": "move_to", "args": {"target": "e3"}, "ms": 900,
                        "result": "FAILED: blocked"}),
            json.dumps({"t": 3.0, "tool": "screenshot", "args": {}, "ms": 0, "result": "z" * 5000}),
        ])
        items = F.parse_actions(lines, result_limit=100)
        self.assertEqual([i["tool"] for i in items], ["get_status", "move_to", "screenshot"])
        self.assertEqual([i["kind"] for i in items], ["read", "act", "vision"])
        self.assertEqual([i["ok"] for i in items], [True, False, True])
        self.assertEqual(len(items[2]["result"]), 101)

    def test_tool_kind(self):
        self.assertEqual(F.tool_kind("mcp__bg3__save_game"), "save")
        self.assertEqual(F.tool_kind("list_saves"), "read")
        self.assertEqual(F.tool_kind("use_ability"), "act")


class ClockTest(unittest.TestCase):
    def test_runner_deadline_wins(self):
        now = 1000.0
        c = F.session_clock({"deadline": now + 1200, "started": now - 2400, "hard_stop": now + 1800},
                            {"deadline": now + 5}, {}, now=now)
        self.assertEqual((c["source"], c["left_s"], c["phase"]), ("runner", 1200, "play"))
        self.assertEqual(c["hard_stop"], now + 1800)

    def test_session_json_fallback_and_phases(self):
        now = 1000.0
        c = F.session_clock({}, {"BG3AI_SESSION_DEADLINE": str(now + 300)}, {}, now=now)
        self.assertEqual((c["source"], c["phase"]), ("session.json", "wrapup"))
        c = F.session_clock({}, {"deadline": now - 1}, {}, now=now)
        self.assertEqual(c["phase"], "over")

    def test_memory_fallback_and_idle(self):
        now = 10000.0
        c = F.session_clock({}, {}, {"open": True, "started_epoch": now - 600}, now=now)
        self.assertEqual((c["source"], c["left_s"]), ("memory", 3000))
        self.assertEqual(F.session_clock({}, {}, {}, now=now)["phase"], "idle")


class StatusShapeTest(unittest.TestCase):
    def test_shape_and_no_guids(self):
        st = {"region": "Shadow-Cursed Lands", "gold": 3500, "active": GUID, "in_combat": True,
              "combat": {"round": 2, "current": GUID},
              "party": [{"uuid": GUID, "name": "Tav", "hp": 5, "max_hp": 10, "downed": False,
                         "classes": [{"class": "Ranger", "subclass": "Hunter", "level": 6}],
                         "statuses": [{"name": "Blessed", "id": "BLESS"}, "Hasted"]}]}
        s = F.shape_status(st)
        self.assertEqual(s["mode"], "combat")
        self.assertEqual(s["party"][0]["classes"], "Ranger 6 (Hunter)")
        self.assertEqual(s["party"][0]["statuses"], ["Blessed", "Hasted"])
        self.assertTrue(s["party"][0]["active"])
        self.assertEqual(s["combat"]["current"], "Tav")
        self.assertNotIn(GUID, json.dumps(s))

    def test_modes(self):
        self.assertEqual(F.shape_status({"in_dialogue": True})["mode"], "dialogue")
        self.assertEqual(F.shape_status({})["mode"], "exploring")

    def test_scrub_and_event_text(self):
        self.assertNotIn(GUID, json.dumps(F.scrub({"uuid": GUID, "x": [f"hit {GUID}"]})))
        txt = F.event_text({"kind": "damage", "data": {"target": GUID, "amount": 7, "uuid": GUID}})
        self.assertTrue(txt.startswith("damage "))
        self.assertIn("amount=7", txt)
        self.assertNotIn(GUID, txt)


class MemoryViewTest(TmpCase):
    def test_view(self):
        mem = self.tmp / "memory"
        (mem / "sessions").mkdir(parents=True)
        (mem / "HANDOFF.md").write_text("# Handoff S002 → S003\n<!-- hidden -->\nSave to load: x\n")
        (mem / "objectives.md").write_text("# Objectives\n- O-1 x\n")
        (mem / "sessions" / "S002.md").write_text("# S002\n" + "".join(f"- {i}\n" for i in range(40)))
        (mem / "sessions" / "S003.md").write_text("# S003\n- current\n")
        (mem / ".session.json").write_text(json.dumps({"current": 3, "open": True}))
        v = F.memory_view(mem, log_lines=5)
        self.assertTrue(v["exists"])
        self.assertEqual((v["session"], v["log_name"]), ("S003", "S003.md"))
        self.assertNotIn("hidden", v["handoff"])
        self.assertEqual(v["log_tail"], ["# S003", "- current"])

    def test_latest_log_without_state(self):
        mem = self.tmp / "memory"
        (mem / "sessions").mkdir(parents=True)
        for n in (9, 10):
            (mem / "sessions" / f"S{n:03d}.md").write_text(f"# S{n}\n")
        self.assertEqual(F.current_session_log(mem).name, "S010.md")

    def test_missing_memory(self):
        v = F.memory_view(self.tmp / "nope")
        self.assertFalse(v["exists"])
        self.assertIsNone(v["handoff"])

    def test_resolvers(self):
        repo = self.tmp
        (repo / "playthroughs" / "run1" / "memory").mkdir(parents=True)
        self.assertEqual(F.resolve_memory(repo, {"playthrough": "run1"}), repo / "playthroughs" / "run1" / "memory")
        self.assertEqual(F.resolve_memory(repo, {}), repo / "play" / "memory")
        self.assertEqual(F.resolve_memory(repo, {}, "/x"), Path("/x"))
        rt = self.tmp / "runtime"
        d = rt / "sessions" / "run1"
        d.mkdir(parents=True)
        (d / "S001.log").write_text("a")
        time.sleep(0.01)
        (d / "S002.log").write_text("b")
        self.assertEqual(F.resolve_agent_log(rt, {"playthrough": "run1"}).name, "S002.log")
        self.assertEqual(F.resolve_agent_log(rt, {"log": str(d / "S001.log")}).name, "S001.log")
        self.assertIsNone(F.resolve_agent_log(rt, {"playthrough": "none"}))


if __name__ == "__main__":
    unittest.main()

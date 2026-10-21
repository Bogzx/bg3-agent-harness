"""Unit tests for bg3play.live (was runner/dashboard/feeds.py) and the tail/parse helpers it uses."""

import json
import shutil
import tempfile
import time
import unittest
from pathlib import Path

from bg3play import live as F
from bg3play.parse import tool_kind
from bg3play.tail import read_new

GUID = "11111111-2222-3333-4444-555555555555"


class TmpCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="bg3dash-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


class ReadNewTest(TmpCase):
    def test_missing_file(self):
        self.assertEqual(read_new(self.tmp / "nope", -1), (0, "", False))

    def test_initial_tail_starts_at_line_boundary(self):
        p = self.tmp / "a.log"
        p.write_text("".join(f"line {i}\n" for i in range(1000)))
        off, text, reset = read_new(p, -1, initial_bytes=100)
        self.assertTrue(reset)
        self.assertEqual(off, p.stat().st_size)
        lines = text.splitlines()
        self.assertEqual(lines[-1], "line 999")
        self.assertTrue(all(ln.startswith("line ") for ln in lines))  # no partial first line

    def test_incremental_and_partial_lines(self):
        p = self.tmp / "a.log"
        p.write_text("one\ntw")
        off, text, _ = read_new(p, 0)
        self.assertEqual((text, off), ("one\n", 4))
        with p.open("a") as f:
            f.write("o\nthree\n")
        off, text, reset = read_new(p, off)
        self.assertEqual(text, "two\nthree\n")
        self.assertFalse(reset)
        self.assertEqual(read_new(p, off), (off, "", False))

    def test_truncation_resets(self):
        p = self.tmp / "a.log"
        p.write_text("x" * 50 + "\n")
        off, _, _ = read_new(p, 0)
        p.write_text("new\n")
        off2, text, reset = read_new(p, off)
        self.assertTrue(reset)
        self.assertEqual(text, "new\n")
        self.assertEqual(off2, 4)

    def test_giant_line_does_not_stall(self):
        p = self.tmp / "a.log"
        p.write_text("y" * 5000)
        off, text, _ = read_new(p, 0, max_bytes=1000)
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
        self.assertEqual(tool_kind("mcp__bg3__save_game"), "save")
        self.assertEqual(tool_kind("list_saves"), "read")
        self.assertEqual(tool_kind("use_ability"), "act")


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






class LatencyTest(TmpCase):
    def _write(self, path, recs):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(r) + "\n" for r in recs))

    def test_no_files(self):
        r = F.latency_payload(self.tmp, {})
        self.assertIsNone(r["live"])
        self.assertEqual(r["history"], [])

    def test_live_and_history(self):
        now = 1_760_000_100.0
        sess = self.tmp / "sessions" / "pt"
        m = sess / "S003.metrics.jsonl"
        self._write(m, [
            {"type": "tool", "tool": "move_to", "t0": now - 30, "t1": now - 25, "ms": 5000, "job_ms": 4800, "rpc_ms": 30, "rpc_n": 2},
            {"type": "tool", "tool": "get_status", "t0": now - 24, "t1": now - 23.95, "ms": 50, "rpc_ms": 40, "rpc_n": 1},
            {"type": "tool", "tool": "interact", "t0": now - 22, "t1": now - 20, "ms": 2000, "job_ms": 1900},
            {"type": "sample", "t": now - 27, "moving": True, "job": 1, "dlg": False, "combat": False},
            {"type": "sample", "t": now - 5, "moving": False, "job": 0, "dlg": False, "combat": False},
        ])
        self._write(sess / "latency_history.jsonl", [{"session": "S001", "apm": 3.2}, {"session": "S002", "apm": 4.0}])
        r = F.latency_payload(self.tmp, {"playthrough": "pt", "metrics": str(m), "turns": str(sess / "S003.turns.jsonl")}, now=now)
        live = r["live"]
        self.assertEqual(live["current_idle_s"], 20.0)
        self.assertEqual(live["idle_median_s"], 3.0)
        self.assertTrue(live["targets"]["bridge_rt_median_ms"]["pass"])
        self.assertEqual([h["session"] for h in r["history"]], ["S001", "S002"])
        self.assertEqual(r["files"]["metrics"], "S003.metrics.jsonl")
        self.assertIsNone(r["files"]["turns"])

    def test_fallback_runtime_metrics(self):
        self._write(self.tmp / "metrics.jsonl", [{"type": "tool", "tool": "get_status", "t0": 1.0, "t1": 1.05, "ms": 50}])
        r = F.latency_payload(self.tmp, {}, now=10.0)
        self.assertEqual(r["files"]["metrics"], "metrics.jsonl")
        self.assertIsNotNone(r["live"])

    def test_tail_skips_partial_line(self):
        p = self.tmp / "big.jsonl"
        self._write(p, [{"i": i} for i in range(1000)])
        out = F._tail_jsonl(p, max_bytes=200)
        self.assertEqual(out[-1], {"i": 999})
        self.assertTrue(all("i" in x for x in out))

class ProgressFeedTest(TmpCase):
    def _write(self, path, recs):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(r) + "\n" for r in recs))

    def test_no_files(self):
        r = F.progress_payload(self.tmp, {})
        self.assertIsNone(r["live"])
        self.assertEqual(r["history"], [])
        self.assertIn("region", r["weights"])

    def test_live_and_history(self):
        sess = self.tmp / "sessions" / "pt"
        p = sess / "S003.progress.jsonl"
        self._write(p, [{"type": "progress", "t": 1000.0 + 600, "kind": "quest_new", "what": "Find a Cure", "points": 3}])
        self._write(sess / "progress_history.jsonl", [{"session": "S001", "points": 40}, {"session": "S002", "points": 12}])
        r = F.progress_payload(self.tmp, {"playthrough": "pt", "progress": str(p), "session": "S003", "started": 1000.0},
                               now=1000.0 + 1800)
        live = r["live"]
        self.assertEqual((live["points"], live["per_hour"], live["session"], live["running"]), (3, 6.0, "S003", True))
        self.assertEqual(live["since_progress_min"], 20.0)
        self.assertEqual(live["recent"][0]["what"], "Find a Cure")
        self.assertEqual([h["session"] for h in r["history"]], ["S001", "S002"])
        ended = F.progress_payload(self.tmp, {"playthrough": "pt", "progress": str(p), "started": 1000.0,
                                              "session_ended": 1000.0 + 900}, now=99999.0)["live"]
        self.assertFalse(ended["running"])
        self.assertEqual(ended["minutes"], 15.0)


class GoalsTest(TmpCase):
    """/api/goals: per-session scorecard from Sxxx.latency.json / .progress.json, live and stub sessions."""

    def setUp(self):
        super().setUp()
        self.d = self.tmp / "sessions" / "pt"
        self.d.mkdir(parents=True)
        lat = {"start": 1000.0, "end": 1840.0, "minutes": 14.0, "apm": 10.3, "actions": 140, "busy_pct": 61.0,
               "pipelining_pct": 35.0, "think_overlap_pct": 40.0, "targets": {
                   "median_idle_explore_s": {"value": 1.6, "target": 2.0, "pass": True},
                   "long_gaps_non_dialogue": {"value": 4, "target": 0, "pass": False},
                   "bridge_rt_median_ms": {"value": 94.0, "target": 100.0, "pass": True}}}
        (self.d / "S001.latency.json").write_text(json.dumps(lat))
        (self.d / "S001.progress.json").write_text(json.dumps({"minutes": 14.0, "points": 70, "per_hour": 300.0}))
        (self.d / "S001.progress.jsonl").write_text(json.dumps(
            {"type": "progress", "t": 1500, "kind": "quest_done", "what": "Escape the Nautiloid", "points": 10}) + "\n")
        (self.d / "S002.metrics.jsonl").write_text("")                       # a runner-restart stub
        (self.d / "S003.progress.jsonl").write_text(json.dumps(              # live, no summaries yet
            {"type": "progress", "t": 2950, "kind": "area", "what": "Campsite", "points": 3}) + "\n")
        (self.d / "S003-r1.progress.jsonl").write_text("")
        (self.d / "S003-fix.metrics.jsonl").write_text("")                  # not a play session
        (self.d / "progress_state.json").write_text(json.dumps(
            {"regions": ["Nautiloid", "Wilderness"], "areas": ["Wilderness|Campsite"], "levels": {"Claude": 1}}))

    def test_scorecard(self):
        st = {"playthrough": "pt", "session": "S003", "started": 2900.0, "session_ended": None}
        g = F.goals_payload(self.tmp, st, now=3500.0)
        self.assertEqual([s["session"] for s in g["sessions"]], ["S001", "S002", "S003", "S003-r1"])
        s1, s2, s3 = g["sessions"][:3]
        self.assertEqual(s1["values"], {"idle": 1.6, "gaps": 4, "rt": 94.0, "apm": 10.3, "busy": 61.0,
                                        "pipe": 35.0, "overlap": 40.0, "pts": 300.0})
        self.assertEqual(s1["pass"], {"idle": True, "gaps": False, "rt": True, "apm": True, "busy": True,
                                      "pipe": True, "overlap": False, "pts": True})
        self.assertEqual((s1["passed"], s1["judged"]), (6, 8))
        self.assertTrue(s2["short"])
        self.assertTrue(s3["live"])
        self.assertEqual(s3["values"]["pts"], 18.0)                        # 3 points in 10 live minutes
        goals = {x["key"]: x for x in g["goals"]}
        self.assertEqual(goals["gaps"]["pass_rate"], 0)                     # only S001 counts (S002 stub, S003 live)
        self.assertEqual(goals["pts"]["series"][:3], [300.0, None, 18.0])
        ms = g["milestones"]
        self.assertIn("First camp", ms["line"])
        nx = {i["id"]: i["session"] for i in ms["items"] if i["reached"]}
        self.assertEqual(nx["nautiloid_escaped"], "S001")
        self.assertEqual(nx["first_camp"], "S003")

    def test_no_playthrough(self):
        self.assertEqual(F.goals_payload(self.tmp, {}, now=1.0)["sessions"], [])



class UnattendedTest(TmpCase):
    """/api/goals "unattended": runs from runner_events.jsonl, faults and recoveries."""

    def test_runs_faults_recoveries(self):
        d = self.tmp / "sessions" / "pt"
        d.mkdir(parents=True)
        evs = [{"t": 0, "kind": "runner_start"},
               {"t": 1800, "kind": "runner_stop", "why": "stopped"},                    # 0.5 h
               {"t": 3600, "kind": "runner_start"},
               {"t": 4000, "kind": "fault", "fault": "crash", "detail": "game process exited (status -9)"},
               {"t": 4100, "kind": "recovered", "fault": "crash", "seconds": 100, "how": "game relaunched"},
               {"t": 5000, "kind": "fault", "fault": "agent_hang", "detail": "no claude output for 4.0 min"},
               {"t": 5040, "kind": "recovered", "fault": "agent_hang", "seconds": 40, "how": "claude restarted"},
               {"t": 6000, "kind": "steam_refused", "why": "online"},
               {"t": 7200, "kind": "runner_start"},                                    # killed hard before
               {"t": 7300, "kind": "fault", "fault": "hang", "detail": "evals time out"},
               {"t": 7400, "kind": "recovery_failed", "fault": "hang", "error": "boom"}]
        (d / "runner_events.jsonl").write_text("\n".join(json.dumps(e) for e in evs) + "\n")
        u = F.unattended_payload(d, {"runner_pid": 123, "steam_offline": True}, now=7200 + 3 * 3600,
                                 pid_alive=lambda p: True)
        self.assertTrue(u["live"])
        self.assertEqual(u["current_h"], 3.0)
        self.assertEqual(u["longest_h"], 3.0)
        self.assertEqual(u["runs"], 3)
        self.assertAlmostEqual(u["total_h"], 0.5 + 2400 / 3600 + 3.0, places=2)   # the killed run ends at its last event
        self.assertEqual((u["faults"]["crash"], u["faults"]["agent_hang"], u["faults"]["hang"]), (1, 1, 1))
        self.assertEqual((u["fault_total"], u["recovered"], u["failed"], u["steam_refused"]), (3, 2, 1, 1))
        self.assertEqual((u["recovery_s_median"], u["recovery_s_max"]), (100, 100))
        self.assertEqual(u["events"][-1]["kind"], "recovery_failed")
        # the runner is gone without a stop event: ends at its heartbeat, not live
        u = F.unattended_payload(d, {"runner_pid": 123, "heartbeat": 9000}, now=20000, pid_alive=lambda p: False)
        self.assertFalse(u["live"])
        self.assertIsNone(u["current_h"])
        self.assertEqual(u["last_stop"], "no stop event (killed?)")
        self.assertIsNone(F.unattended_payload(self.tmp, {}, now=0))

    def test_crash_loop_alert(self):
        d = self.tmp / "sessions" / "pt"
        d.mkdir(parents=True)
        evs = [{"t": 0, "kind": "runner_start"},
               {"t": 10, "kind": "launch_failed", "attempt": 1, "save": "C-2__S014-start", "error": "crash"},
               {"t": 30, "kind": "launch_failed", "attempt": 2, "save": "C-2__S014-start", "error": "crash"},
               {"t": 80, "kind": "launch_failed", "attempt": 3, "save": "C-1__S013-end", "error": "crash"},
               {"t": 81, "kind": "launch_gave_up", "error": "3 launches in a row died"},
               {"t": 81, "kind": "runner_stop", "why": "gave up: the game crashes on every launch"}]
        (d / "runner_events.jsonl").write_text("\n".join(json.dumps(e) for e in evs) + "\n")
        alert = {"t": 81, "kind": "launch_gave_up", "msg": "runner stopped: the game crashes on every launch"}
        u = F.unattended_payload(d, {"alert": alert}, now=100, pid_alive=lambda p: False)
        self.assertEqual((u["launch_failed"], u["alert"]), (3, alert))
        self.assertEqual([e["kind"] for e in u["events"]][-2:], ["launch_gave_up", "runner_stop"])
        self.assertIsNone(F.unattended_payload(d, {"alert": None}, now=100)["alert"])

if __name__ == "__main__":
    unittest.main()

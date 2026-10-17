#!/usr/bin/env python3
"""Unit tests for runner/play.py's pure logic. Never launches claude or the game.

    python3 runner/test_play.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
# RUN-9: play.RUNTIME is read at import; a test must never append to the real runtime/runner.log (the phantom
# "0.01 s after SIGTERM" kill bursts the orchestrator chased). Tests that need a runtime set their own.
os.environ.setdefault("BG3AI_RUNTIME", tempfile.mkdtemp(prefix="bg3ai-test-runtime-"))
os.environ.setdefault("BG3AI_KNOWLEDGE_LOCK", "0")   # never chmod the real play/knowledge (Knowledge tests patch it)
import play as P  # noqa: E402


class SessionNumbering(unittest.TestCase):
    def test_first_session(self):
        self.assertEqual(P.predict_session_number([], None), 1)

    def test_after_logs(self):
        self.assertEqual(P.predict_session_number([1, 2, 3], 3), 4)

    def test_state_ahead_of_logs(self):
        # a session whose log was never written still advances the counter (session.py does the same)
        self.assertEqual(P.predict_session_number([1, 2], 5), 6)

    def test_gaps(self):
        self.assertEqual(P.predict_session_number([1, 7, 3], None), 8)

    def test_ids(self):
        self.assertEqual(P.session_id(7), "S007")
        self.assertEqual(P.session_id(1234), "S1234")


class DeadlineMath(unittest.TestCase):
    def test_deadlines(self):
        d, hard = P.deadlines(1000.0, 60)
        self.assertEqual(d, 1000 + 3600)
        self.assertEqual(hard, d + 600)
        self.assertEqual(P.deadlines(0, 10, grace=30), (600, 630))

    def test_wrap_minutes(self):
        self.assertEqual(P.wrap_minutes(60), 57)
        self.assertEqual(P.wrap_minutes(30), 27)
        self.assertEqual(P.wrap_minutes(20), 17)
        self.assertEqual(P.wrap_minutes(6), 4)  # tiny harness sessions: wrap-up is a third of the session
        self.assertEqual(P.wrapup_minutes(90), 3)

    def test_early_exit_minutes(self):
        t0 = 1_000_000.0
        # claude-run-2 S002: exited cleanly after 12.6 of 20 min -> early exit with 7.4 min left
        self.assertEqual(P.early_exit_minutes("exited", t0, t0 + 12.6 * 60, 20), 7.4)
        self.assertIsNone(P.early_exit_minutes("exited", t0, t0 + 17.5 * 60, 20))  # normal wrap-up end
        self.assertIsNone(P.early_exit_minutes("exited", t0, t0 + 25 * 60, 20))    # overtime
        for hard in ("crash", "hang", "hard_stop", "aborted"):
            self.assertIsNone(P.early_exit_minutes(hard, t0, t0 + 60, 20))

    def test_short_sessions_refused(self):
        a = mock.Mock(session_minutes=15, allow_short_sessions=False)
        with mock.patch.object(P, "acquire_lock") as lock, mock.patch("sys.stderr"):
            self.assertEqual(P.cmd_run(a), 2)
        lock.assert_not_called()

    def test_budget_unbounded(self):
        self.assertEqual(P.session_budget(0, None, 60), 60)

    def test_budget_clipped_and_exhausted(self):
        now = 10_000.0
        self.assertEqual(P.session_budget(now, now + 3 * 3600, 60), 60)
        self.assertEqual(P.session_budget(now, now + 40 * 60, 60), 40)
        self.assertIsNone(P.session_budget(now, now + 14 * 60, 60))
        self.assertIsNone(P.session_budget(now, now - 5, 60))

    def test_kickoff_has_pace_preflight(self):
        k = P.build_kickoff("S004", "pt1", time.time() + 3600, None, [])
        self.assertIn("PACE PRE-FLIGHT", k)
        self.assertIn("Never pass wait=true", k)
        self.assertIn("stuck.md", k)
        self.assertNotIn(time.strftime("%Y-%m-%d"), P.PACE_PREFLIGHT)  # static: cache-friendly

    def test_kickoff_mentions_deadline_and_notes(self):
        t = time.mktime((2026, 10, 9, 21, 30, 0, 0, 0, -1))
        k = P.build_kickoff("S004", "pt1", t, "S003-end", ["NOTE A", "NOTE B"])
        self.assertIn("S004", k)
        self.assertIn("21:30", k)
        self.assertIn('"S003-end"', k)
        self.assertTrue(k.index("NOTE A") < k.index("NOTE B"))
        self.assertNotIn("already loaded", P.build_kickoff("S001", "pt1", t, None, []))

    def test_kickoff_leads_with_the_memory_briefing(self):
        t = time.time() + 3600
        k = P.build_kickoff("S004", "pt1", t, None, [], "=== BG3 PLAYTHROUGH MEMORY — session S004 ===\n...")
        self.assertTrue(k.startswith("=== BG3 PLAYTHROUGH MEMORY — session S004"))
        self.assertIn("briefing (PLAYTHROUGH.md and HANDOFF.md) is above", k)
        self.assertIn("end_session", k)
        self.assertIn("First call session_start", P.build_kickoff("S004", "pt1", t, None, []))


class HandoffAndSaves(unittest.TestCase):
    def test_load_menu_args(self):
        t = time.mktime((2026, 10, 9, 22, 39, 30, 0, 0, -1))
        saves = [P.Save("Claude-1__S001-start", "Claude", "S001-start", t - 400),
                 P.Save("Claude-2__S001-imps-done", "Claude", "S001-imps-done", t),
                 P.Save("Claude-3__S001-end", "Claude", "S001-end", t + 360),
                 P.Save("Tav-4__S002-end", "Tav", "S002-end", t + 900)]
        a = P.load_menu_args(saves[1], saves)
        self.assertEqual((a["action"], a["name"], a["owner"], a["index"]), ("load", "Claude-2__S001-imps-done", "Claude", 2))
        self.assertEqual(a["date"], {"y": 2026, "m": 10, "d": 9, "H": 22, "M": 39})

    def test_parse_handoff(self):
        self.assertEqual(P.parse_handoff_save('# Handoff\nSave to load: "S042-end" — party at the well\n'), "S042-end")
        self.assertEqual(P.parse_handoff_save("Save to load: S042-end — x"), "S042-end")
        self.assertEqual(P.parse_handoff_save("Save to load: `S007-pre-boss` (out of combat)"), "S007-pre-boss")
        self.assertIsNone(P.parse_handoff_save("Save to load: (none — new game; start from the main menu)"))
        self.assertIsNone(P.parse_handoff_save("no such line"))
        # the template's example inside an HTML comment must be ignored
        self.assertIsNone(P.parse_handoff_save('<!--\nSave to load: "S042-end"\n-->\nSave to load: (none)\n'))

    def test_parse_folder(self):
        s = P.parse_save_folder("pibooool-10812619937__QuickSave_42", 5.0)
        self.assertEqual((s.owner, s.label, s.mtime), ("pibooool", "QuickSave_42", 5.0))
        s = P.parse_save_folder("Tav-Name-52012618851__S003-pre-boss")
        self.assertEqual((s.owner, s.label), ("Tav-Name", "S003-pre-boss"))
        self.assertIsNone(P.parse_save_folder("garbage"))

    def test_is_ai_label(self):
        self.assertTrue(P.is_ai_label("S001-start", ""))
        self.assertFalse(P.is_ai_label("QuickSave_3", ""))
        self.assertFalse(P.is_ai_label("Dank Crypt - 3h 15m", ""))
        self.assertTrue(P.is_ai_label("pt1-S010-end", "pt1-"))
        self.assertFalse(P.is_ai_label("S010-end", "pt1-"))

    def _saves(self):
        f = P.parse_save_folder
        return [
            f("pibooool-1__Dank Crypt - 3h 15m", 900),      # the user's own playthrough
            f("pibooool-2__QuickSave_42", 950),
            f("Ilse-3__S001-start", 100),
            f("Ilse-4__S001-end", 200),
            f("Ilse-5__QuickSave_1", 300),                 # a runner quicksave
            f("Other-6__S001-end", 400),                   # another playthrough, same label
        ]

    def test_named_saves_with_a_folder_prefix(self):
        # BG3AI_SAVE_PREFIX=Claude- (docs/HANDOFF.md, the MCP server) names the folder, and the agent's
        # save_game labels are plain "S001-end": the session's named save must count (S001 quicksaved over it).
        saves = [P.parse_save_folder("Claude-10__S001-end", 200), P.parse_save_folder("Claude-11__QuickSave_0", 300),
                 P.parse_save_folder("Tav-12__S001-end", 250)]
        self.assertEqual([s.folder for s in P.saves_since(saves, 100, "Claude", "Claude-")], ["Claude-10__S001-end"])
        self.assertEqual(P.pick_save(saves, None, "Claude", set(), "Claude-").folder, "Claude-10__S001-end")
        # a label prefix still works, and a foreign character never matches
        self.assertTrue(P.is_ai_save(P.parse_save_folder("Ilse-1__pt1-S010-end"), "pt1-"))
        self.assertFalse(P.is_ai_save(P.parse_save_folder("Ilse-1__S010-end"), "pt1-"))
        self.assertFalse(P.is_ai_save(P.parse_save_folder("Tav-12__S001-end"), "Claude-"))

    def test_pick_skips_worker_saves(self):
        # 2026-10-10 live-test break: workers saved LIVETEST-* after S027-start; the playthrough's save stays the pick
        saves = [P.parse_save_folder("Claude-48__S027-start", 100), P.parse_save_folder("Claude-14__LIVETEST-act", 200),
                 P.parse_save_folder("Claude-12__LIVETEST-vis-open", 300), P.parse_save_folder("Claude-13__AutoSave_42", 250)]
        self.assertEqual(P.pick_save(saves, "S027-start", "Claude", set(), "Claude-").folder, "Claude-48__S027-start")

    def test_pick_only_saves_written_while_this_playthrough_played(self):
        # 2026-10-17: claude-run-2's newest save is S065-end (10-11 12:03), but replay-hoh saved S027-end and
        # S028-lastlight-prep as "Claude" on 10-15 and a worker saved S999-savetest on 10-13: without the play
        # windows the resume would have loaded Act 2's S028-lastlight-prep
        f = P.parse_save_folder
        saves = [f("Claude-1__S065-pre-orin", 1000.0), f("Claude-2__S065-end", 1500.0),
                 f("Claude-3__S999-savetest", 5000.0), f("Claude-4__S027-end", 9000.0),
                 f("Claude-5__S028-lastlight-prep", 9500.0), f("Claude-6__QuickSave_9", 9600.0)]
        windows = [(100.0, 600.0), (900.0, 1450.0)]
        self.assertEqual(P.pick_save(saves, None, "Claude", set(), "Claude-").label, "S028-lastlight-prep")
        self.assertEqual(P.pick_save(saves, None, "Claude", set(), "Claude-", windows).label, "S065-end")  # +50 s slack
        self.assertEqual(P.pick_save(saves, "S027-end", "Claude", set(), "Claude-", windows).label, "S065-end")
        # the runner's / agent's own quicksaves count by folder, whenever written; no windows = a new playthrough
        self.assertEqual(P.pick_save(saves, None, "Claude", {"Claude-6__QuickSave_9"}, "Claude-", windows).label,
                         "QuickSave_9")
        self.assertEqual(P.pick_save(saves, None, "Claude", set(), "Claude-", []).label, "S028-lastlight-prep")
        self.assertFalse(P.in_windows(1571.0, windows))
        self.assertTrue(P.in_windows(1570.0, windows))
        # a save older than the playthrough's first window is judged as before (a crash in the first session)
        older = [f("Claude-0__S001-start", -500.0)] + saves[3:4]
        self.assertEqual(P.pick_save(older, None, "Claude", set(), "Claude-", windows).label, "S001-start")
        self.assertFalse(P.played_elsewhere(-500.0, windows))
        self.assertTrue(P.played_elsewhere(750.0, windows))   # 600 + 120 s slack < 750 < 900 - 120

    def test_foreign_saves_for_the_mcp_server(self):
        f = P.parse_save_folder
        saves = [f("Claude-2__S065-end", 1500.0), f("Claude-3__S999-savetest", 5000.0),
                 f("Claude-5__S028-lastlight-prep", 9500.0), f("Claude-6__QuickSave_9", 9600.0),
                 f("Claude-7__LIVETEST-x", 9700.0), f("Tav-8__S001-end", 9800.0)]
        windows = [(900.0, 1450.0)]
        self.assertEqual(P.foreign_saves(saves, "Claude", set(), windows, "Claude-"),
                         ["Claude-3__S999-savetest", "Claude-5__S028-lastlight-prep"])   # not quicksaves/LIVETEST/Tav
        self.assertEqual(P.foreign_saves(saves, "Claude", {"Claude-3__S999-savetest"}, windows, "Claude-"),
                         ["Claude-5__S028-lastlight-prep"])
        self.assertEqual(P.foreign_saves(saves, "Claude", set(), [], "Claude-"), [])     # a new playthrough
        with tempfile.TemporaryDirectory() as d:
            r = P.Runner(P.Options("solo", None, 20, "opus", save_owner="Claude"))
            r.state.dir = Path(d)
            r.state["history"] = [{"kind": "play", "session": "S065", "started": 900.0, "ended": 1450.0}]
            with mock.patch.object(P, "list_saves", lambda: saves):
                self.assertEqual(r.save_env()["BG3AI_SAVES_EXCLUDE"],
                                 "Claude-3__S999-savetest\nClaude-5__S028-lastlight-prep")
                r.state["history"] = []
                self.assertNotIn("BG3AI_SAVES_EXCLUDE", r.save_env())

    def test_play_windows_include_sessions_the_runner_never_recorded(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            (d / "S001.turns.jsonl").write_text('{"type":"assistant","t":100.0}\n{"type":"use","t":150.5}\n'
                                                 '{"type":"result","t":290.0}\n')
            (d / "S002.turns.jsonl").write_text('not json\n{"type":"use","t":400.0}\n')   # crashed: one event
            (d / "S003.turns.jsonl").write_text("")
            hist = [{"kind": "play", "session": "S001", "started": 90.0, "ended": 300.0},
                    {"kind": "play", "session": "S000"}]                                       # no times: skipped
            self.assertEqual(P.play_windows(hist, d), [(90.0, 300.0), (100.0, 290.0), (400.0, 400.0)])
            self.assertEqual(P.play_windows(None, None), [])

    def test_foreign_use_since(self):
        best = P.parse_save_folder("Claude-48__S027-start", 100)
        mine = [best, P.parse_save_folder("Claude-13__AutoSave_42", 150), P.parse_save_folder("Claude-9__S026-end", 90)]
        f = lambda saves=mine, acts=(), started=None, rs=set(): P.foreign_use_since(best, saves, list(acts), started, rs, "Claude-")
        self.assertIsNone(f())
        self.assertIn("LIVETEST-setup", f(mine + [P.parse_save_folder("Claude-21__LIVETEST-setup", 120)]))
        self.assertIsNone(f(mine + [P.parse_save_folder("Claude-21__LIVETEST-setup", 80)]))   # older: not a sign
        self.assertIsNone(f(mine + [P.parse_save_folder("Claude-5__QuickSave_1", 130)], rs={"Claude-5__QuickSave_1"}))
        load = {"t": 130, "tool": "load_game", "args": {"save": "Claude-9__S026-end"}, "result": "load_game: done — loaded=S026-end"}
        self.assertIn("S026-end", f(acts=[load]))                  # a worker loaded an older playthrough save
        self.assertIsNone(f(acts=[dict(load, t=90)]))              # before our save: irrelevant
        self.assertIsNone(f(acts=[dict(load, result="load_game: done — loaded=S027-start")]))
        self.assertIn("relaunched", f(started=110))
        self.assertIsNone(f(started=95))

    def test_read_actions_since(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "actions.jsonl"
            p.write_text('{"t": 5, "tool": "a"}\nnot json\n{"t": 15, "tool": "load_game"}\n')
            self.assertEqual([r["tool"] for r in P.read_actions_since(10, p)], ["load_game"])
            self.assertEqual(P.read_actions_since(10, Path(d) / "missing"), [])

    def test_pick_never_takes_foreign_saves(self):
        saves = self._saves()
        self.assertIsNone(P.pick_save(saves, None, None, set(), ""))
        best = P.pick_save(saves, None, "Ilse", set(), "")
        self.assertEqual(best.folder, "Ilse-4__S001-end")
        self.assertNotIn("pibooool", best.folder)

    def test_pick_includes_runner_quicksave(self):
        best = P.pick_save(self._saves(), "S001-end", "Ilse", {"Ilse-5__QuickSave_1"}, "")
        self.assertEqual(best.folder, "Ilse-5__QuickSave_1")

    def test_pick_by_handoff_before_owner_known(self):
        best = P.pick_save(self._saves(), "S001-start", None, set(), "")
        self.assertEqual(best.folder, "Ilse-3__S001-start")
        # with an owner, the other playthrough's identical label is excluded
        best = P.pick_save(self._saves(), "S001-end", "Ilse", set(), "")
        self.assertEqual(best.folder, "Ilse-4__S001-end")

    def test_saves_since(self):
        new = P.saves_since(self._saves(), 150, "Ilse", "")
        self.assertEqual([s.folder for s in new], ["Ilse-4__S001-end"])
        self.assertEqual(P.saves_since(self._saves(), 1000, None, ""), [])


class StuckDetection(unittest.TestCase):
    status = {"level": "SCL_Main_A", "region": "Shadow-Cursed Lands", "subregion": "Last Light Inn",
              "party": [{"level": 6, "xp": 100}, {"level": 6, "xp": 90}]}
    journal = [{"title": "Find the Nightsong", "steps": ["a", "b"]}, {"title": "Lift the Curse", "steps": ["x"]}]

    def test_signature(self):
        sig = P.progress_signature(self.status, self.journal)
        self.assertEqual(sig["level"], "SCL_Main_A")
        self.assertEqual(sig["party_levels"], ["6", "6"])
        self.assertEqual(sig["xp"], 190)
        self.assertEqual(sig["journal_steps"], 3)
        self.assertIsNone(P.progress_signature(None, None))
        self.assertEqual(P.progress_signature(None, {"quests": self.journal})["journal_steps"], 3)

    def test_changes_count_as_progress(self):
        a = P.progress_signature(self.status, self.journal)
        self.assertFalse(P.made_progress(a, P.progress_signature(self.status, self.journal)))
        moved = dict(self.status, subregion="Reithwin Town")
        self.assertTrue(P.made_progress(a, P.progress_signature(moved, self.journal)))
        more = self.journal + [{"title": "New", "steps": ["s"]}]
        self.assertTrue(P.made_progress(a, P.progress_signature(self.status, more)))
        xp = dict(self.status, party=[{"level": 6, "xp": 150}, {"level": 6, "xp": 90}])
        self.assertTrue(P.made_progress(a, P.progress_signature(xp, self.journal)))
        self.assertIsNone(P.made_progress(None, a))

    def test_session_progress_counts_points_and_far_moves(self):
        a = P.progress_signature(self.status, self.journal)
        # S040: islands descent + Balthazar (32 pts), wipe, reload: same save signature, still progress
        self.assertEqual(P.session_progress(a, dict(a), 32), (True, "32 story points"))
        self.assertEqual(P.session_progress(a, dict(a), 2)[0], False)            # S039: 2 explore points
        here = dict(self.status, party=[{"level": 6, "xp": 100, "pos": [0, 0, 0]}, {"level": 6, "xp": 90}])
        near = dict(here, party=[{"level": 6, "xp": 100, "pos": [20, 0, 0]}, {"level": 6, "xp": 90}])
        far = dict(here, party=[{"level": 6, "xp": 100, "pos": [0, -30, 70]}, {"level": 6, "xp": 90}])
        s0 = P.progress_signature(here, self.journal)
        self.assertEqual(s0["pos"], [0.0, 0.0, 0.0])
        self.assertFalse(P.made_progress(s0, P.progress_signature(near, self.journal)))   # a step isn't progress
        self.assertEqual(P.session_progress(s0, P.progress_signature(near, self.journal), 0)[0], False)
        ok, why = P.session_progress(s0, P.progress_signature(far, self.journal), 0)
        self.assertTrue(ok)
        self.assertIn("moved 76 m", why)
        ok, why = P.session_progress(a, dict(a, subregion="Reithwin Town"), 0)
        self.assertEqual((ok, why), (True, "changed subregion"))
        self.assertEqual(P.session_progress(None, a, 12), (True, "12 story points"))   # points alone tell it
        self.assertEqual(P.session_progress(None, a, 3), (None, "unknown"))
        self.assertEqual(P.session_progress(a, a, None)[0], False)

    def test_boundary_s(self):
        h = [{"kind": "play", "ended": 1000.0}, {"kind": "fix", "ended": 1100.0}]
        self.assertEqual(P.boundary_s(h, 1045.0), 45.0)          # fix entries are no play boundary
        self.assertIsNone(P.boundary_s(h, 1000.0 + P.BOUNDARY_MAX_S + 1))   # the runner was stopped
        self.assertIsNone(P.boundary_s([], 5.0))

    def test_is_stuck(self):
        h = lambda p, kind="play": {"progress": p, "kind": kind}  # noqa: E731
        self.assertFalse(P.is_stuck([]))
        self.assertFalse(P.is_stuck([h(False)]))
        self.assertTrue(P.is_stuck([h(True), h(False), h(False)]))
        self.assertFalse(P.is_stuck([h(False), h(True)]))
        self.assertFalse(P.is_stuck([h(False), h(None)]))          # unknown (crash) isn't "stuck"
        self.assertTrue(P.is_stuck([h(False), h(False, "fix"), h(False)]))  # fix sessions don't count

    def test_directive_mentions_location(self):
        sig = P.progress_signature(self.status, self.journal)
        d = P.stuck_directive([{"progress": False, "end_sig": sig}] * 2)
        self.assertIn("CHANGE APPROACH", d)
        self.assertIn("Last Light Inn", d)


# ---------------------------------------------------------------- the claude path before runner/clients.py

def legacy_claude_cmd(r, prompt: str, claude_session: str, resume: bool, tag: str = "") -> list[str]:
    """play.py's Runner.claude_cmd as it was before the client interface (commit 1ca87d6), frozen: the claude
    command a session gets must stay byte-identical (ClaudeClient through Runner.play_request)."""
    model, effort = r.model_for(tag)
    cmd = ["claude", "-p", prompt, "--model", model, "--output-format", "stream-json",
           "--verbose", "--strict-mcp-config", "--mcp-config", str(P.PLAY_DIR / ".mcp.json"),
           "--exclude-dynamic-system-prompt-sections", "--setting-sources", "project,local",
           "--disable-slash-commands", "--tools", "ToolSearch"]
    if effort:
        cmd += ["--effort", effort]
    if r.opt.fallback_model:
        cmd += ["--fallback-model", r.opt.fallback_model]
    if tag != "-fix":
        state = getattr(r, "state", None)
        ac = (P.session_overrides(state.dir / "session-config.json") if state else {}).get(
            "autocompact", r.opt.autocompact)
        if ac:
            cmd += ["--autocompact", str(ac)]
    cmd += ["--resume", claude_session] if resume else ["--session-id", claude_session]
    return cmd


def _legacy_short(value, limit: int) -> str:
    s = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    s = s.replace("\n", " ⏎ ")
    return s if len(s) <= limit else s[: limit - 1] + "…"


def legacy_format_stream_event(ev: dict) -> list[str]:
    """play.py's format_stream_event before the client interface (commit 1ca87d6), frozen: the human log's lines
    for a claude stream must stay the same (clients.format_event over ClaudeClient.parse)."""
    t = ev.get("type")
    out = []
    if t == "system" and ev.get("subtype") == "init":
        servers = [f"{m.get('name')}:{m.get('status')}" for m in ev.get("mcp_servers") or [] if isinstance(m, dict)]
        out.append(f"[init] session={ev.get('session_id')} model={ev.get('model')} "
                   f"tools={len(ev.get('tools') or [])} mcp={servers}")
    elif t == "system" and ev.get("subtype") in ("compact_boundary",):
        out.append("[compact] context compacted")
    elif t == "assistant":
        for c in (ev.get("message") or {}).get("content") or []:
            if c.get("type") == "text" and c.get("text", "").strip():
                out.append(f"[say] {_legacy_short(c['text'].strip(), 2000)}")
            elif c.get("type") == "tool_use":
                name = c.get("name", "?").removeprefix("mcp__bg3__")
                out.append(f"[tool] {name} {_legacy_short(c.get('input') or {}, 300)}")
    elif t == "user":
        content = (ev.get("message") or {}).get("content")
        for c in content if isinstance(content, list) else []:
            if c.get("type") == "tool_result":
                body = c.get("content")
                if isinstance(body, list):
                    body = " ".join(b.get("text", f"<{b.get('type')}>") for b in body if isinstance(b, dict))
                tag = "[error]" if c.get("is_error") else "[result]"
                out.append(f"{tag} {_legacy_short(body or '', 400)}")
    elif t == "result":
        out.append(f"[end] {ev.get('subtype')} turns={ev.get('num_turns')} "
                   f"duration={round((ev.get('duration_ms') or 0) / 60000, 1)}min "
                   f"cost=${ev.get('total_cost_usd', 0) or 0:.2f}")
    return out


def _cmd(r, prompt: str, session: str, resume: bool, tag: str = "") -> list[str]:
    """The command a session gets (Runner.play_request → the client)."""
    return r.agent_cmd(r.play_request(prompt, session, resume, tag))


def _lines(ev: dict) -> list[str]:
    """The human log lines the runner writes for one claude stream event (ClaudeClient.parse → format_event)."""
    from clients import ClaudeClient, format_event
    return [x for e in ClaudeClient().parse(json.dumps(ev)) for x in format_event(e)]


class Misc(unittest.TestCase):
    def test_health_tracker(self):
        t = P.HealthTracker(3)
        for ok in (False, False):
            t.record(ok)
        self.assertFalse(t.hung)
        t.record(True)
        t.record(False)
        t.record(False)
        self.assertFalse(t.hung)
        t.record(False)
        self.assertTrue(t.hung)

    def test_stream_formatting(self):
        ev = {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "Heading to the windmill."},
            {"type": "tool_use", "name": "mcp__bg3__move_to", "input": {"target": "e12"}}]}}
        self.assertEqual(_lines(ev), ["[say] Heading to the windmill.",
                                                     '[tool] move_to {"target":"e12"}'])
        res = {"type": "user", "message": {"content": [
            {"type": "tool_result", "content": [{"type": "text", "text": "status: arrived"}]}]}}
        self.assertEqual(_lines(res), ["[result] status: arrived"])
        end = {"type": "result", "subtype": "success", "num_turns": 12, "duration_ms": 120000,
               "total_cost_usd": 1.5}
        self.assertEqual(_lines(end), ["[end] success turns=12 duration=2.0min cost=$1.50"])
        init = {"type": "system", "subtype": "init", "session_id": "abc", "model": "opus", "tools": [1, 2],
                "mcp_servers": [{"name": "bg3", "status": "connected"}]}
        self.assertIn("bg3:connected", _lines(init)[0])
        self.assertEqual(_lines({"type": "weird"}), [])

    def test_claude_command(self):
        r = P.Runner.__new__(P.Runner)
        r.opt = P.Options("pt", None, 60, "opus")
        cmd = _cmd(r, "go", "1234", resume=False)
        self.assertEqual(cmd[:3], ["claude", "-p", "go"])
        self.assertIn("--session-id", cmd)
        self.assertIn("stream-json", cmd)
        self.assertIn("--verbose", cmd)
        self.assertEqual(cmd[cmd.index("--model") + 1], "opus")
        self.assertIn("--resume", _cmd(r, "fix", "1234", resume=True))
        self.assertNotIn("--effort", cmd)  # Claude Code's default unless asked
        for flag in P.CLAUDE_FLAGS:
            self.assertIn(flag, cmd)

    def test_default_client_command_unchanged(self):
        # The whole Runner path (Options default client → play_request → agent_cmd) gives the pre-client command
        cases = [{}, {"effort": "high"}, {"fallback_model": "sonnet"}, {"autocompact": 350_000},
                 {"effort": "max", "fallback_model": "sonnet", "autocompact": 200_000, "fix_model": "haiku"}]
        for kw in cases:
            r = P.Runner.__new__(P.Runner)
            r.opt = P.Options("pt", None, 60, "opus", **kw)
            self.assertEqual(r.opt.client, "claude")
            for prompt, sid, resume, tag in (("go", "1234", False, ""), ("keep playing", "1234", True, ""),
                                             ("fix", "5678", False, "-fix"), ("fix", "5678", True, "-fix")):
                with self.subTest(opt=kw, resume=resume, tag=tag):
                    self.assertEqual(_cmd(r, prompt, sid, resume, tag),
                                     legacy_claude_cmd(r, prompt, sid, resume, tag))

    def test_builtin_tools_disabled(self):
        # The playing agent gets only the bg3 MCP tools (+ ToolSearch to load their schemas): files, memory and the
        # sandbox are the server's (memtools.py); no built-in file, shell, web or agent tool exists in the session.
        r = P.Runner.__new__(P.Runner)
        r.opt = P.Options("pt", None, 60, "opus")
        for cmd in (_cmd(r, "go", "1", resume=False), _cmd(r, "fix", "1", resume=True, tag="-fix")):
            tools = cmd[cmd.index("--tools") + 1]
            self.assertEqual(tools, "ToolSearch")
            self.assertIn("--strict-mcp-config", cmd)
            self.assertIn("--disable-slash-commands", cmd)
        settings = json.loads((P.PLAY_DIR / ".claude" / "settings.json").read_text())
        self.assertNotIn("hooks", settings)                  # nothing a client must run for correctness
        self.assertEqual(settings["permissions"]["allow"], ["mcp__bg3__*"])
        for tool in ("Bash", "Read", "Edit", "Write", "Glob", "Grep", "WebFetch", "Agent"):
            self.assertIn(tool, settings["permissions"]["deny"])

    def test_keeper_model_default_is_the_play_model(self):
        self.assertEqual(P.Options("pt", None, 60, "opus").keeper_model, "same")
        self.assertEqual(P.keeper_model("same", "opus"), "opus")
        self.assertEqual(P.keeper_model(None, "fable"), "fable")
        self.assertEqual(P.keeper_model("sonnet", "opus"), "sonnet")   # an explicit override
        with mock.patch.object(P, "cmd_run", lambda a: (a.keeper_model, a.no_memory_keeper)):
            self.assertEqual(P.main(["run", "--playthrough", "x"]), ("same", False))
            self.assertEqual(P.main(["run", "--playthrough", "x", "--no-keeper", "--keeper-model", "haiku"]),
                             ("haiku", True))
            self.assertEqual(P.main(["run", "--playthrough", "x", "--no-memory-keeper"])[1], True)

    def test_model_and_effort(self):
        import tempfile
        from types import SimpleNamespace
        r = P.Runner.__new__(P.Runner)
        r.opt = P.Options("pt", None, 60, "opus", effort="medium", fix_model="sonnet", fallback_model="sonnet")
        with tempfile.TemporaryDirectory() as d:
            r.state = SimpleNamespace(dir=Path(d))
            cmd = _cmd(r, "go", "1", resume=False)
            self.assertEqual(cmd[cmd.index("--effort") + 1], "medium")
            self.assertEqual(cmd[cmd.index("--fallback-model") + 1], "sonnet")
            fix = _cmd(r, "fix", "1", resume=True, tag="-fix")
            self.assertEqual((fix[fix.index("--model") + 1], fix[fix.index("--effort") + 1]), ("sonnet", "low"))
            # session-config.json overrides the CLI for play sessions, re-read every time; bad values ignored
            (Path(d) / "session-config.json").write_text('{"model": "fable", "effort": "high"}')
            self.assertEqual(r.model_for(), ("fable", "high"))
            (Path(d) / "session-config.json").write_text('{"effort": "turbo", "model": 3}')
            self.assertEqual(r.model_for(), ("opus", "medium"))
            (Path(d) / "session-config.json").write_text("not json")
            self.assertEqual(r.model_for(), ("opus", "medium"))
            self.assertEqual(r.model_for("-fix"), ("sonnet", "low"))

    def test_autocompact(self):
        import tempfile
        from types import SimpleNamespace
        r = P.Runner.__new__(P.Runner)
        r.opt = P.Options("pt", None, 30, "opus")
        with tempfile.TemporaryDirectory() as d:
            r.state = SimpleNamespace(dir=Path(d))
            self.assertNotIn("--autocompact", _cmd(r, "go", "1", resume=False))  # Claude Code's default
            r.opt = P.Options("pt", None, 30, "opus", autocompact=350_000)
            cmd = _cmd(r, "go", "1", resume=False)
            self.assertEqual(cmd[cmd.index("--autocompact") + 1], "350000")
            self.assertNotIn("--autocompact", _cmd(r, "fix", "1", resume=True, tag="-fix"))
            # session-config.json switches it live (A/B); out-of-range / non-int values are ignored
            (Path(d) / "session-config.json").write_text('{"autocompact": 200000}')
            cmd = _cmd(r, "go", "1", resume=False)
            self.assertEqual(cmd[cmd.index("--autocompact") + 1], "200000")
            for bad in ('{"autocompact": 5000}', '{"autocompact": "200k"}', '{"autocompact": true}'):
                (Path(d) / "session-config.json").write_text(bad)
                cmd = _cmd(r, "go", "1", resume=False)
                self.assertEqual(cmd[cmd.index("--autocompact") + 1], "350000")

    def test_cli_effort(self):
        with self.assertRaises(SystemExit):
            P.main(["run", "--playthrough", "x", "--effort", "turbo"])


os_getpgid = P.os.getpgid


class KillGame(unittest.TestCase):
    """kill_game with every OS call mocked: nothing is signalled for real."""

    def setUp(self):
        self.saved = (P.all_bg3_pids, P.port_open, P.os.kill, P.os.killpg, P.time.sleep, P.KILL_GRACE, P.KILL_CONFIRM,
                      P.RUNTIME, P.crash_reporter_pids, P.is_harness_pid, P.proc_start, P.proc_cmdline)
        self.tmp = tempfile.TemporaryDirectory()
        P.RUNTIME = Path(self.tmp.name)              # RUN-9: never the real runtime/runner.log
        P.crash_reporter_pids = lambda: []           # nor the machine's real crash reporters
        self.sent = []
        self.alive = {4242}       # our harness game
        self.foreign = set()      # a user's own Steam-launched BG3
        self.ignores_term = True
        # the launcher's group 777: launch_game.sh started at tick 555 (runner.json records it as pg_start)
        self.leader = {"start": 555, "cmd": b"/bin/bash\0/repo/runner/launch_game.sh\0"}

        def kill(pid, sig):
            self.sent.append((pid, sig))
            if sig == P.signal.SIGKILL or not self.ignores_term:
                self.alive.discard(pid)

        P.all_bg3_pids = lambda: sorted(self.alive | self.foreign)
        P.is_harness_pid = lambda pid: pid not in self.foreign
        P.proc_start = lambda pid: self.leader["start"] if pid == 777 else None
        P.proc_cmdline = lambda pid: self.leader["cmd"] if pid == 777 else b""
        P.port_open = lambda *a, **k: bool(self.alive)
        P.os.kill = kill
        P.os.killpg = lambda pg, sig: self.sent.append(("pg", pg, sig))
        P.time.sleep = lambda s: None
        P.KILL_GRACE = 0.01
        P.KILL_CONFIRM = 0.01
        self.r = P.Runner.__new__(P.Runner)
        self.r.game_proc = None
        self.r.state = type("S", (), {"data": {"game": {"pgid": 777, "pg_start": 555}}, "__getitem__": lambda s, k: s.data.get(k),
                                      "__setitem__": lambda s, k, v: s.data.__setitem__(k, v)})()

    def tearDown(self):
        (P.all_bg3_pids, P.port_open, P.os.kill, P.os.killpg, P.time.sleep, P.KILL_GRACE, P.KILL_CONFIRM,
         P.RUNTIME, P.crash_reporter_pids, P.is_harness_pid, P.proc_start, P.proc_cmdline) = self.saved
        self.tmp.cleanup()

    def test_logs_to_the_test_runtime(self):
        # RUN-9: the kill bursts these tests log go to a temp runtime, never REPO/runtime/runner.log
        self.r.kill_game()
        self.assertIn("killing the game", (P.RUNTIME / "runner.log").read_text())
        self.assertNotEqual(self.saved[7], P.REPO / "runtime")   # the module-level guard: BG3AI_RUNTIME is a tmp dir

    def test_escalates_to_sigkill_when_sigterm_ignored(self):
        self.r.kill_game()
        sigs = [x[-1] for x in self.sent]
        self.assertIn(P.signal.SIGTERM, sigs)
        self.assertIn(P.signal.SIGKILL, sigs)
        self.assertIn(("pg", 777, P.signal.SIGTERM), self.sent)   # the launcher's group too
        self.assertIn((4242, P.signal.SIGKILL), self.sent)        # and the bg3 process itself
        self.assertIsNone(self.r.state["game"])

    def test_no_sigkill_when_sigterm_works(self):
        self.ignores_term = False
        self.r.kill_game()
        self.assertNotIn(P.signal.SIGKILL, [x[-1] for x in self.sent])

    def test_survivor_raises(self):
        P.os.kill = lambda pid, sig: self.sent.append((pid, sig))   # nothing ever dies
        with self.assertRaises(RuntimeError):
            self.r.kill_game()

    def test_users_own_bg3_never_signalled(self):
        # RUN-4: a user's Steam-launched BG3 beside ours: ours dies, theirs gets no signal at all
        self.foreign = {5151}
        self.r.kill_game()
        self.assertIn((4242, P.signal.SIGKILL), self.sent)
        self.assertNotIn(5151, [x[0] for x in self.sent])
        self.assertIn("leaving bg3 pid(s) 5151 alone", (P.RUNTIME / "runner.log").read_text())

    def test_stale_group_not_signalled(self):
        # RUN-4: runner.json's pgid after a reboot (or a reused pid): another start time or another program
        for leader in ({"start": 999, "cmd": self.leader["cmd"]}, {"start": 555, "cmd": b"/usr/bin/firefox\0"}):
            with self.subTest(leader=leader):
                self.sent, self.alive = [], {4242}
                self.r.state["game"] = {"pgid": 777, "pg_start": 555}
                self.leader = leader
                self.assertIsNone(self.r.owned_group())
                self.r.kill_game()
                self.assertNotIn("pg", [x[0] for x in self.sent])
                self.assertIn((4242, P.signal.SIGKILL), self.sent)   # our game itself still goes

    def test_owned_group(self):
        self.assertEqual(self.r.owned_group(), 777)
        self.r.state["game"] = {"pgid": 777}                  # a record from before pg_start: the command line decides
        self.assertEqual(self.r.owned_group(), 777)
        self.leader = {"start": None, "cmd": b""}             # leader gone: ours only while it holds our game
        P.os.getpgid = lambda pid: 777 if pid == 4242 else 1
        try:
            self.assertEqual(self.r.owned_group(), 777)
            self.alive = set()
            self.assertIsNone(self.r.owned_group())
        finally:
            P.os.getpgid = os_getpgid

    def test_launch_refused_beside_a_foreign_bg3(self):
        self.foreign, self.alive = {5151}, set()
        self.r.event = lambda kind, **f: self.sent.append(("event", kind))
        saved = (P.steam_ready, P.bg3le_problems, P.subprocess.Popen)
        P.steam_ready, P.bg3le_problems = (lambda: (True, "ok")), (lambda: [])
        P.subprocess.Popen = None                              # launching would fail loudly
        try:
            with self.assertRaises(P.ForeignGameRunning):
                self.r.launch_game(None)
        finally:
            P.steam_ready, P.bg3le_problems, P.subprocess.Popen = saved
        self.assertIn(("event", "foreign_game"), self.sent)
        self.assertTrue(issubclass(P.ForeignGameRunning, P.LaunchBlocked))   # the run loop retries, never gives up

    def test_launch_refreshes_a_stale_xauthority(self):
        # naut3: Xwayland restarted (16:01 freeze) with a new -auth file; a game launched with the old one can't
        # open a window. Each launch re-checks it.
        from unittest import mock
        self.alive = set()
        envs: list[dict] = []

        class Proc:
            pid = 4343

        def popen(args, **kw):
            envs.append(kw["env"])
            return Proc()
        self.r.state = {}
        with mock.patch.object(P, "steam_ready", lambda: (True, "ok")), \
                mock.patch.object(P, "bg3le_problems", lambda: []), \
                mock.patch.object(P.subprocess, "Popen", popen), \
                mock.patch.object(P, "mod_tree_hashes", lambda: {}), \
                mock.patch.object(P, "write_status", lambda **kw: None), \
                mock.patch.object(P.display_env, "pgrep_xwayland",
                                  lambda: "9 /usr/bin/Xwayland :1 -auth /run/user/1000/xauth_new -rootless\n"), \
                mock.patch.dict(P.os.environ, {"DISPLAY": ":1", "XAUTHORITY": "/run/user/1000/xauth_old"}):
            self.r.launch_game(None)
        self.assertEqual(envs[0]["XAUTHORITY"], "/run/user/1000/xauth_new")
        self.assertIn("display env refreshed", (P.RUNTIME / "runner.log").read_text())


class ApiAndMcpFailures(unittest.TestCase):
    """RUN-7 / RUN-8 pure parts."""

    def test_classify(self):
        self.assertIsNone(P.classify_api_failure([], False))
        self.assertEqual(P.classify_api_failure(["Claude AI usage limit reached|1760000000"], False), "limit")
        self.assertEqual(P.classify_api_failure(["API Error: 529 Overloaded"], False), "limit")
        self.assertEqual(P.classify_api_failure(["API Error: 500 Internal server error"], False), "limit")
        self.assertEqual(P.classify_api_failure([], True), "limit")              # rate_limit_event rejected
        self.assertEqual(P.classify_api_failure(["Invalid API key · Please run /login"], False), "auth")
        self.assertEqual(P.classify_api_failure(["API Error: 404 model: claude-x not found"], False), "auth")
        self.assertEqual(P.classify_api_failure(["Credit balance is too low"], True), "auth")

    def test_backoff(self):
        now = 1000.0
        self.assertEqual(P.api_backoff_s(now + 600, now, 1), 600 + P.API_RESET_SLACK_S)   # its reset time
        self.assertEqual(P.api_backoff_s(now + 10 ** 6, now, 1), P.API_RESET_MAX_S)
        self.assertEqual([P.api_backoff_s(None, now, k) for k in (1, 2, 3)], [60, 120, 240])
        self.assertEqual(P.api_backoff_s(now - 5, now, 99), P.API_BACKOFF_MAX_S)        # a past reset: doubling

    def test_mcp_problem(self):
        self.assertIsNone(P.mcp_problem(["bg3:connected"], True))
        self.assertEqual(P.mcp_problem(["bg3:failed"], True), "the bg3 MCP server is failed")
        self.assertIn("missing", P.mcp_problem([], True))
        self.assertIsNone(P.mcp_problem([], False))         # codex/gemini: an absent list proves nothing
        self.assertIsNone(P.mcp_problem(None, True))
        self.assertEqual(P.mcp_problem(["bg3:pending"], False), "the bg3 MCP server is pending")


class AgentSaves(unittest.TestCase):
    """RUN-6: the agent's quicksaves (actions.jsonl save_game.bg records) are this playthrough's saves."""

    def test_agent_quicksaves(self):
        acts = [{"tool": "save_game.bg", "ok": True, "folder": "Claude-52151269953__QuickSave_7"},
                {"tool": "save_game.bg", "ok": True, "folder": "Claude-52151269953__S028-prep"},   # named: by label
                {"tool": "save_game.bg", "ok": False, "folder": None},
                {"tool": "save_game.bg", "ok": True, "folder": "Tav-1__QuickSave_3"},               # another owner
                {"tool": "save_game", "result": "QuickSave_9"},
                {"tool": "save_game.bg", "ok": True, "folder": "Claude-52151269953__QuickSave_7"}]  # once
        self.assertEqual(P.agent_quicksaves(acts, "claude"), ["Claude-52151269953__QuickSave_7"])
        self.assertEqual(P.agent_quicksaves(acts, None), [])

    def test_record_and_pick(self):
        r = P.Runner.__new__(P.Runner)
        r.opt = P.Options("pt", None, 20, "opus", save_owner="Claude")
        r.state = type("S", (), {"data": {"runner_saves": [], "save_owner": "Claude"},
                                 "__getitem__": lambda s, k: s.data.get(k),
                                 "__setitem__": lambda s, k, v: s.data.__setitem__(k, v)})()
        acts = [{"t": 5, "tool": "save_game.bg", "ok": True, "folder": f"Claude-1__QuickSave_{i}"} for i in range(60)]
        with mock.patch.object(P, "read_actions_since", lambda t, *a, **k: acts):
            r.record_agent_saves(0)
        self.assertEqual(len(r.state["agent_saves"]), P.AGENT_SAVES_KEPT)
        self.assertEqual(r.state["agent_saves"][-1], "Claude-1__QuickSave_59")
        saves = [P.parse_save_folder("Claude-1__S010-end", 10.0), P.parse_save_folder("Claude-1__QuickSave_59", 20.0),
                 P.parse_save_folder("Claude-1__QuickSave_99", 30.0)]
        self.assertEqual(P.pick_save(saves, None, "Claude", r.ours()).label, "QuickSave_59")

    def test_position_keys_are_not_progress(self):
        status = {"level": "L", "party": [{"name": "Claude", "pos": [1, 2, 3]}, {"name": "Gale", "pos": [4, 5, 6]}]}
        a = P.progress_signature(status, [])
        self.assertEqual((a["pos_name"], a["party_pos"]["Gale"]), ("Claude", [4.0, 5.0, 6.0]))
        b = P.progress_signature(dict(status, party=status["party"][::-1]), [])
        self.assertFalse(P.made_progress(a, b))
        self.assertIsNone(P.state_drift(a, b))           # same members, same places, other order


class HarnessPid(unittest.TestCase):
    """RUN-4: is_harness_pid/proc_start on real processes (sleep children; no game)."""

    def spawn(self, **env):
        import subprocess
        p = subprocess.Popen(["sleep", "30"], env={"PATH": "/usr/bin:/bin", **env})
        self.addCleanup(lambda: (p.kill(), p.wait()))
        time.sleep(0.1)
        return p.pid

    def test_markers(self):
        self.assertFalse(P.is_harness_pid(self.spawn()))                                  # a user's own process
        self.assertFalse(P.is_harness_pid(self.spawn(SteamAppId="1086940")))              # a Steam-launched BG3
        self.assertTrue(P.is_harness_pid(self.spawn(BG3AI_GAME_STAMP="20261016-120000")))  # launch_game.sh's
        self.assertTrue(P.is_harness_pid(self.spawn(                                       # a launch from before
            VK_LAYER_MESA_SCREENSHOT_CONFIG="comms;control=bg3ai-shot;output_dir=/x")))
        self.assertFalse(P.is_harness_pid(2 ** 22 + 7))                                   # no such process

    def test_proc_start_identity(self):
        a = self.spawn()
        self.assertIsInstance(P.proc_start(a), int)
        self.assertEqual(P.proc_start(a), P.proc_start(a))
        self.assertIsNone(P.proc_start(2 ** 22 + 7))
        self.assertIn(b"sleep", P.proc_cmdline(a))

    def test_launcher_marks_the_game(self):
        sh = (P.REPO / "runner" / "launch_game.sh").read_text()
        self.assertIn('"BG3AI_GAME_STAMP=$stamp"', sh)
        self.assertIn('SCOPE_UNIT="bg3ai-game-$stamp.scope"', sh)


class Relaunch(unittest.TestCase):
    """se's discriminator (2026-10-10 18:07-18:18): party ready checks break in a long-lived game process; the
    runner relaunches it between sessions (age ≥ GAME_MAX_AGE_H, or the last session logged a ready-check failure)."""

    def test_reason(self):
        now = 100_000.0
        self.assertIsNone(P.relaunch_reason(now - 3600, now, "fine", 2))
        self.assertIn("2.0 h old", P.relaunch_reason(now - 7200, now, "", 2))
        self.assertIsNone(P.relaunch_reason(now - 9 * 3600, now, "", 0))   # 0 = never by age
        why = P.relaunch_reason(now - 60, now, "EVENTS\n  rest_flow step=ready_failed:ReadyCheckContent_GoToNight\n", 2)
        self.assertIn("ready_failed:ReadyCheckContent_GoToNight", why)
        self.assertIn("end_day_failed", P.relaunch_reason(None, now, "rest_flow step=end_day_failed", 2))
        self.assertIsNone(P.relaunch_reason(None, now, None, 2))

    def test_pick_save_never_an_autosave(self):
        f = P.parse_save_folder
        saves = [f("Claude-1__S032-end", 10.0), f("Claude-2__AutoSave_59", 20.0), f("Claude-3__QuickSave_4", 30.0)]
        self.assertEqual(P.pick_save(saves, None, "Claude", set()).label, "S032-end")
        self.assertEqual(P.pick_save(saves, "AutoSave_59", "Claude", set()).label, "S032-end")   # not even by HANDOFF
        self.assertEqual(P.pick_save(saves, None, "Claude", {"Claude-3__QuickSave_4"}).label, "QuickSave_4")  # runner's own

    def _runner(self, d: Path, *, launched, log_text, save_mtime, last_started=50.0, steam=(True, "offline"),
                owns=True, first=False):
        self.saved = (P.port_open, P.steam_ready, P.list_saves, P.game_started_at, P.log)
        P.port_open = lambda *a, **k: True
        P.steam_ready = lambda: steam
        P.list_saves = lambda: [P.Save("Claude-1__S032-end", "Claude", "S032-end", save_mtime)]
        P.game_started_at = lambda: launched
        P.log = lambda m: self.logs.append(m)
        self.logs, self.killed, self.events = [], [], []
        r = P.Runner.__new__(P.Runner)
        r.opt = P.Options("pt", None, 30, "opus", save_owner="Claude", take_over=True)
        r.mem = d
        r.state = type("S", (), {"dir": d, "data": {"game": None, "save_owner": "Claude", "runner_saves": [],
                                                       "loaded_save": "x",
                                                       "history": [{"kind": "play", "session": "S032",
                                                                    "started": last_started}]},
                                  "__getitem__": lambda s, k: s.data.get(k),
                                  "__setitem__": lambda s, k, v: s.data.__setitem__(k, v)})()
        (d / "S032.log").write_text(log_text)
        r.owns_game = lambda: owns
        r.kill_game = lambda: self.killed.append(1)
        r.event = lambda kind, **f: self.events.append(kind)
        return r

    def tearDown(self):
        if hasattr(self, "saved"):
            (P.port_open, P.steam_ready, P.list_saves, P.game_started_at, P.log) = self.saved

    def test_relaunches_after_a_ready_check_failure(self):
        with tempfile.TemporaryDirectory() as td:
            r = self._runner(Path(td), launched=time.time() - 600, log_text="step=ready_failed:X", save_mtime=100.0)
            self.assertIn("ready_failed", r.maybe_relaunch(False))
            self.assertEqual(self.killed, [1])
            self.assertIsNone(r.state["loaded_save"])
            self.assertIn("game_relaunch", self.events)

    def test_relaunches_an_old_process(self):
        with tempfile.TemporaryDirectory() as td:
            r = self._runner(Path(td), launched=time.time() - 5 * 3600, log_text="ok", save_mtime=100.0)
            self.assertIn("h old", r.maybe_relaunch(False))
            self.assertEqual(self.killed, [1])

    def test_young_healthy_process_kept(self):
        with tempfile.TemporaryDirectory() as td:
            r = self._runner(Path(td), launched=time.time() - 600, log_text="ok", save_mtime=100.0)
            self.assertIsNone(r.maybe_relaunch(False))
            self.assertEqual(self.killed, [])

    def test_kept_when_steam_not_offline(self):
        with tempfile.TemporaryDirectory() as td:
            r = self._runner(Path(td), launched=time.time() - 600, log_text="end_day_failed", save_mtime=100.0,
                             steam=(False, "Steam is not running"))
            self.assertIsNone(r.maybe_relaunch(False))
            self.assertEqual(self.killed, [])
            self.assertIn("game_relaunch_skipped", self.events)

    def test_kept_when_the_newest_save_predates_the_last_session(self):
        with tempfile.TemporaryDirectory() as td:
            r = self._runner(Path(td), launched=time.time() - 600, log_text="ready_failed", save_mtime=10.0,
                             last_started=50.0)
            self.assertIsNone(r.maybe_relaunch(False))
            self.assertEqual(self.killed, [])
            self.assertTrue(any("lose play" in m for m in self.logs))


class Gates(unittest.TestCase):
    def setUp(self):
        self.saved = (P.rpc, P.lua_eval, P.log)
        self.calls = []

    def test_decision(self):
        self.assertTrue(P.gates_decision(None, None, True))               # new game
        self.assertTrue(P.gates_decision("Claude", "Claude", False))
        self.assertTrue(P.gates_decision(" claude ", "Claude", False))
        self.assertFalse(P.gates_decision("pibooool", "Claude", False))   # the user's own save
        self.assertIsNone(P.gates_decision(None, "Claude", False))        # name unreadable
        self.assertIsNone(P.gates_decision("Claude", None, False))        # owner unknown

    def _runner(self, new_game=False, name="Claude"):
        r = P.Runner.__new__(P.Runner)
        r.opt = P.Options("pt", None, 60, "opus", save_owner="Claude")
        r.state = type("S", (), {"__getitem__": lambda s, k: None})()
        r.new_game = new_game
        self.calls = []
        P.log = lambda m: None
        P.lua_eval = lambda *a, **k: name
        return r

    def tearDown(self):
        P.rpc, P.lua_eval, P.log = self.saved

    def test_settings_errors_never_raise(self):
        r = self._runner()

        def boom(op, args=None, context="server", timeout=0):
            self.calls.append((op, args))
            raise RuntimeError("game not answering")
        P.rpc = boom
        r.apply_play_settings()                                            # must not raise
        self.assertEqual([c[0] for c in self.calls[:3]], ["ui.config", "ui.settings", "ui.dismiss"])
        self.assertEqual(self.calls[0][1], {"autoskip": True})
        self.assertEqual(self.calls[1][1], {"show_tutorials": False})

    def test_gates_follow_character(self):
        for name, new_game, expected in (("Claude", False, True), ("pibooool", False, False),
                                         ("", True, True), ("", False, None)):
            r = self._runner(new_game, name)
            P.rpc = lambda op, args=None, context="server", timeout=0: self.calls.append((op, args))
            r.apply_play_settings()
            gates = [a for op, a in self.calls if op == "ui.config" and "save_enabled" in a]
            if expected is None:
                self.assertEqual(gates, [], name)
            else:
                self.assertEqual(gates, [{"save_enabled": expected, "load_enabled": expected}], name)
            # UI-2: the MCP server follows the same decision (its _enable_save_gates used to reopen closed gates)
            self.assertEqual(r.save_env().get("BG3AI_SAVE_GATES"), {True: "1", False: "0", None: None}[expected])
            P.rpc, P.lua_eval, P.log = self.saved


class SaveIdentity(unittest.TestCase):
    """RUN-5 / I1: the MCP server gets BG3AI_SAVE_OWNER and a BG3AI_SAVE_PREFIX derived from --save-owner alone (the
    README quick start sets no prefix; an empty one meant "No saves." and no reload after a wipe)."""

    def test_pure(self):
        self.assertEqual(P.save_identity("Claude", ""), ("Claude", "Claude-"))
        self.assertEqual(P.save_identity("Claude", "Claude-"), ("Claude", "Claude-"))
        self.assertEqual(P.save_identity("Claude", "pt1-"), ("Claude", "pt1-"))   # the operator's own prefix wins
        self.assertEqual(P.save_identity(None, ""), ("", ""))                     # new game, no name yet
        self.assertEqual(P.gates_env(None), {})

    def test_run_claude_env_from_save_owner_alone(self):
        tmp = Path(tempfile.mkdtemp(prefix="bg3ai-env-"))
        self.addCleanup(__import__("shutil").rmtree, tmp, True)
        envs = []

        def popen(cmd, **kw):
            envs.append(kw["env"])
            raise OSError("stop here")
        with mock.patch.multiple(P, RUNTIME=tmp, SAVE_PREFIX="", log=lambda m: None, list_saves=list), \
                mock.patch.object(P.subprocess, "Popen", popen), \
                mock.patch.dict(P.os.environ, {"BG3AI_SAVE_GATES": "1", "BG3AI_SAVE_OWNER": "Stale"}):
            r = P.Runner(P.Options("env-test", None, 60, "opus", save_owner="Claude"))
            r.gates = False
            with self.assertRaises(OSError):
                r.run_claude("S001", "go", 60, "c")
            P.os.environ.pop("BG3AI_SAVE_OWNER")
            r2 = P.Runner(P.Options("env-new", None, 60, "opus"))
            with self.assertRaises(OSError):
                r2.run_claude("S001", "go", 60, "c")
        self.assertEqual((envs[0]["BG3AI_SAVE_OWNER"], envs[0]["BG3AI_SAVE_PREFIX"], envs[0]["BG3AI_SAVE_GATES"]),
                         ("Claude", "Claude-", "0"))
        self.assertEqual((envs[1]["BG3AI_SAVE_OWNER"], envs[1]["BG3AI_SAVE_PREFIX"]), ("", ""))
        self.assertNotIn("BG3AI_SAVE_GATES", envs[1])                     # not decided: no stale value passes


class Profile(unittest.TestCase):
    """CHAR-1 / I4 / I5: profile.json and the phase 0 kickoff block."""

    def test_load(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "profile.json"
            self.assertEqual(P.load_profile(f), {})                                    # missing: today's flow
            f.write_text("{broken")
            self.assertEqual(P.load_profile(f), {})
            f.write_text(json.dumps({"character": {"brief": "  "}, "difficulty": "Hard"}))
            self.assertEqual(P.load_profile(f), {})                                    # no brief, bad difficulty
            f.write_text(json.dumps({"character": {"brief": "a dwarf cleric", "roleplay": "x"}, "rules": {}}))
            self.assertEqual(P.load_profile(f), {"character": {"brief": "a dwarf cleric", "roleplay": "strict"},
                                                 "rules": {}})                         # strict by default

    def test_update(self):
        p = P.update_profile({}, "a dwarf cleric")
        self.assertEqual(p, {"character": {"brief": "a dwarf cleric", "roleplay": "strict"}})
        p = P.update_profile(dict(p, character=dict(p["character"], name="Mira")), roleplay="light",
                             difficulty="Tactician")
        self.assertEqual(p, {"character": {"brief": "a dwarf cleric", "roleplay": "light", "name": "Mira"},
                             "difficulty": "Tactician"})
        self.assertEqual(P.update_profile({}, roleplay="light"), {})                   # no brief: no character

    def test_note(self):
        prof = {"character": {"brief": "pious\nshort-tempered", "constraints": "no Dark Urge", "roleplay": "strict"}}
        n = P.character_creation_note(prof, None)
        self.assertIn("character-creation.md", n)
        self.assertIn("> pious\n> short-tempered", n)
        self.assertIn("Constraints: no Dark Urge", n)
        self.assertIn("Roleplay: strict", n)
        self.assertIn('Difficulty: new_game("Explorer") (the default', n)
        self.assertIn("CHARACTER CREATION", n)                 # play/CLAUDE.md's playbook trigger
        self.assertIn('save_game(label="S001-start")', n)   # the fallback when create_character's save didn't start
        self.assertIn("make the first move at once, with no more reads", n)   # naut2: 12-14 s of reads after CC
        self.assertNotIn("read name/race/class back with get_status", n)
        self.assertIn("Name: yours to choose", n)
        self.assertIn('Name: exactly "Ilse"', P.character_creation_note(prof, "Ilse"))


class UntilMilestone(unittest.TestCase):
    """RUN-2: run --until <milestone> and its runs.jsonl record."""

    def test_until_reached(self):
        self.assertIsNone(P.until_reached(None, "nautiloid_escaped"))
        seen = {"regions": ["Nautiloid"], "quests": {"Escape the Nautiloid": {"done": False}}}
        self.assertIsNone(P.until_reached(seen, "nautiloid_escaped"))
        self.assertEqual(P.until_reached(seen, "nautiloid")["label"], "Aboard the Nautiloid")
        seen["quests"]["Escape the Nautiloid"]["done"] = True
        self.assertEqual(P.until_reached(seen, "nautiloid_escaped"),
                         {"id": "nautiloid_escaped", "label": "Nautiloid escaped"})

    def test_run_record(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            res = lambda cost, out: json.dumps({"type": "result", "num_turns": 2, "total_cost_usd": cost,
                                                "usage": {"output_tokens": out, "input_tokens": 1}})
            (d / "S004.jsonl").write_text('{"type":"assistant"}\n' + res(1.5, 10) + "\n")
            (d / "S004-r1.jsonl").write_text(res(0.5, 5) + "\n")
            (d / "S004-keeper.jsonl").write_text(res(0.1, 1) + "\n")
            (d / "S004.metrics.jsonl").write_text(res(99, 99) + "\n")                  # not a claude stream
            ev = [{"t": 50, "kind": "fault", "fault": "crash"}, {"t": 150, "kind": "fault", "fault": "crash"},
                  {"t": 160, "kind": "recovered"}, {"t": 170, "kind": "fault", "fault": "agent_hang"}]
            (d / "runner_events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in ev))
            hist = [{"kind": "play", "started": 90, "minutes": 30}, {"kind": "play", "started": 110, "minutes": 20}]
            with mock.patch.object(P, "read_actions_since", lambda t: [{"tool": "load_game"}, {"tool": "look"}]):
                rec = P.run_record(d, 100, 100 + 3600, {"id": "crash_site", "label": "Reached the surface",
                                                        "session": "S004", "t": 3000}, "pt", hist)
        self.assertEqual((rec["milestone"], rec["reached_in"], rec["wall_min"]), ("crash_site", "S004", 60.0))
        self.assertEqual((rec["sessions"], rec["play_min"]), (1, 20))
        self.assertEqual((rec["cost_usd"], rec["turns"], rec["tokens"]["output"]), (2.1, 6, 16))
        self.assertEqual((rec["crashes"], rec["agent_hangs"], rec["recoveries"], rec["reloads"]), (1, 1, 1, 1))


class ClaudeSaves(unittest.TestCase):
    def test_claude_owner(self):
        f = P.parse_save_folder
        saves = [f("Claude-11__S001-start", 10), f("Claude-12__S002-end", 20), f("pibooool-9__S002-end", 30),
                 f("Claude-13__QuickSave_4", 40)]
        best = P.pick_save(saves, "S002-end", "Claude", set(), "")
        self.assertEqual(best.folder, "Claude-12__S002-end")
        self.assertEqual(saves[0].owner, "Claude")




class Latency(unittest.TestCase):
    """Runner metrics: stream timing from a fake stream-json transcript + the end-of-session summary."""

    def test_stream_timing_and_summary(self):
        import json
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            base = d / "S001"
            timing = P.latency.StreamTiming(Path(f"{base}.turns.jsonl"))
            t0 = 1_760_000_000.0
            events = [
                (1.0, {"type": "assistant", "message": {"content": [
                    {"type": "tool_use", "id": "a", "name": "mcp__bg3__move_to", "input": {"target": "e1"}}]}}),
                (4.0, {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "a", "content": "done"}]}}),
                (5.5, {"type": "assistant", "message": {"content": [
                    {"type": "tool_use", "id": "b", "name": "mcp__bg3__interact", "input": {"target": "e2"}}]}}),
                (7.0, {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "b", "content": "done"}]}}),
            ]
            for t, ev in events:
                timing.on_event(ev, t0 + t)
            Path(f"{base}.metrics.jsonl").write_text(json.dumps(
                {"type": "tool", "tool": "get_status", "t0": t0, "t1": t0 + 0.04, "ms": 40, "rpc_ms": 30, "rpc_n": 1}) + "\n")

            class FakeRunner:
                state = type("S", (), {"dir": d})()

            s = P.Runner.write_latency(FakeRunner(), "S001", base, t0, t0 + 60)
            self.assertEqual(s["think_s"]["median"], 1.5)
            self.assertEqual(s["source"], "stream")       # no action records from the bridge: stream fallback
            self.assertEqual(s["idle_s"]["all"]["median"], 1.5)
            self.assertTrue(s["targets"]["median_idle_explore_s"]["pass"])
            self.assertTrue(s["targets"]["bridge_rt_median_ms"]["pass"])
            self.assertTrue(Path(f"{base}.latency.json").exists())
            hist = (d / "latency_history.jsonl").read_text().splitlines()
            self.assertEqual(json.loads(hist[0])["session"], "S001")

    def test_metrics_path_in_env(self):
        src = Path(P.__file__).read_text()
        self.assertIn('env["BG3AI_METRICS_PATH"] = f"{base}.metrics.jsonl"', src)

class Progress(unittest.TestCase):
    """Story progress: the session poller writes Sxxx.progress.jsonl, the end writes the summary + history."""

    def test_poller_and_summary(self):
        import json
        import tempfile
        import threading

        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            base = d / "S004"
            world = {"status": {"region": "Nautiloid", "party": [{"name": "Claude", "level": 1}]},
                     "journal": [{"title": "Escape", "steps": ["a"]}], "map": {"waypoints": []},
                     "markers": {"markers": []}, "ui": {"screen": "game", "region": "Hold"}}
            polls = []
            stop = threading.Event()

            def fake_read(_eval):
                polls.append(1)
                if len(polls) == 2:
                    world["journal"][0]["steps"].append("b")
                    world["status"]["region"] = "Wilderness"
                if len(polls) == 3:
                    raise OSError("loading screen")
                if len(polls) >= 4:
                    stop.set()
                return json.loads(json.dumps(world))

            class FakeRunner:
                state = type("S", (), {"dir": d})()
                check_until = lambda self, seen, session: None   # noqa: E731 - no run --until here

            orig_read, orig_poll = P.story.read_game, P.story.POLL_S
            P.story.read_game, P.story.POLL_S = fake_read, 0.001
            try:
                log = []
                human = type("H", (), {"write": lambda self, x: log.append(x)})()
                P.Runner.progress_loop(FakeRunner(), "S004", base, 0.0, stop, human)
            finally:
                P.story.read_game, P.story.POLL_S = orig_read, orig_poll
            self.assertTrue(any("+15 region: Wilderness" in x for x in log), log)
            self.assertTrue(any("+2 quest_step: Escape: b" in x for x in log), log)
            s = P.Runner.write_progress(FakeRunner(), "S004", base, 0.0, 1800.0)
            self.assertEqual(s["points"], 15 + 2 + 3)  # region, quest_step, area (Hold in a new region)
            self.assertEqual(s["per_hour"], 40.0)
            self.assertEqual(P.story_points(Path(f"{base}.progress.json")), 20)
            row = json.loads((d / "progress_history.jsonl").read_text())
            self.assertEqual(row["session"], "S004")
            self.assertTrue((d / "progress_state.json").exists())

    def test_wired_into_run_claude(self):
        src = Path(P.__file__).read_text()
        self.assertIn('progress=f"{base}.progress.jsonl"', src)
        self.assertIn('self.write_progress(f"{sid}{tag}", base, start, end)', src)


class AvatarName(unittest.TestCase):
    def test_reads_the_avatar_not_the_selection(self):
        seen = []
        saved = P.lua_eval
        try:
            P.lua_eval = lambda code, context="client", timeout=0: seen.append((code, context)) or " Claude \n"
            self.assertEqual(P.host_character_name(), "Claude")
        finally:
            P.lua_eval = saved
        code, ctx = seen[0]
        self.assertEqual(ctx, "server")
        self.assertIn("DB_Avatars", code)
        self.assertLess(code.index("DB_Avatars"), code.index("GetHostCharacter"))  # selection only as fallback

    def test_unreadable_is_none(self):
        saved = P.lua_eval
        try:
            P.lua_eval = lambda *a, **k: (_ for _ in ()).throw(OSError("no game"))
            self.assertIsNone(P.host_character_name())
        finally:
            P.lua_eval = saved


class HotReload(unittest.TestCase):
    def setUp(self):
        self.saved = (P.port_open, P.game_state, P.lua_eval, P.log, P.time.sleep, P.mod_tree_hashes)
        self.logs, self.evals = [], []
        self.back_after = 2
        P.port_open = lambda *a, **k: True
        P.game_state = lambda: "Running"
        P.log = self.logs.append
        P.time.sleep = lambda s: None

        def ev(code, context="client", timeout=0):
            self.evals.append((code, context))
            if "Reset" in code:
                raise ConnectionError("dropped")
            self.back_after -= 1
            return "true" if self.back_after <= 0 else "false"
        P.lua_eval = ev
        self.r = P.Runner.__new__(P.Runner)
        self.r.game_proc, self.r.abort = None, False
        self.r.state = type("S", (), {"data": {}, "__getitem__": lambda s, k: s.data.get(k),
                                      "__setitem__": lambda s, k, v: s.data.__setitem__(k, v)})()

    def tearDown(self):
        (P.port_open, P.game_state, P.lua_eval, P.log, P.time.sleep, P.mod_tree_hashes) = self.saved

    def test_hashes_and_diff(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "a").mkdir()
            (root / "a" / "x.lua").write_text("one")
            (root / "y.lua").write_text("two")
            h1 = P.mod_tree_hashes(root)
            self.assertEqual(set(h1), {"a/x.lua", "y.lua"})
            (root / "y.lua").write_text("2")
            (root / "z.lua").write_text("new")
            h2 = P.mod_tree_hashes(root)
            self.assertEqual(P.changed_files(h1, h2), ["y.lua", "z.lua"])
            self.assertEqual(P.changed_files(h2, h2), [])
            self.assertEqual(P.changed_files(None, h2), sorted(h2))
            (root / "a" / "x.lua").unlink()
            self.assertEqual(P.changed_files(h2, P.mod_tree_hashes(root)), ["a/x.lua"])

    def test_unchanged_does_nothing(self):
        P.mod_tree_hashes = lambda: {"a.lua": "1"}
        self.r.state["mod_hashes"] = {"a.lua": "1"}
        self.assertEqual(self.r.hot_reload_mod(), 0)
        self.assertEqual(self.evals, [])

    def test_changed_resets_and_waits_for_rpc(self):
        P.mod_tree_hashes = lambda: {"a.lua": "2", "b.lua": "1"}
        self.r.state["mod_hashes"] = {"a.lua": "1", "b.lua": "1"}
        self.assertEqual(self.r.hot_reload_mod(), 1)
        self.assertEqual(self.evals[0], ("Ext.Debug.Reset() return 'reset'", "server"))
        self.assertTrue(any("Mods.BG3AI.BG3AI.RPC" in c for c, _ in self.evals[1:]))
        self.assertEqual(self.r.state["mod_hashes"], {"a.lua": "2", "b.lua": "1"})
        self.assertTrue(any(m.startswith("mod hot-reloaded (1 files changed") for m in self.logs), self.logs)

    def test_mod_never_back_raises_and_keeps_old_hashes(self):
        P.mod_tree_hashes = lambda: {"a.lua": "2"}
        self.r.state["mod_hashes"] = {"a.lua": "1"}
        self.back_after = 10 ** 9
        saved = P.RELOAD_TIMEOUT
        P.RELOAD_TIMEOUT = 0.05
        try:
            with self.assertRaises(RuntimeError):
                self.r.hot_reload_mod()
        finally:
            P.RELOAD_TIMEOUT = saved
        self.assertEqual(self.r.state["mod_hashes"], {"a.lua": "1"})  # retried next time

    def test_skipped_while_loading(self):
        P.mod_tree_hashes = lambda: {"a.lua": "2"}
        P.game_state = lambda: "LoadLevel"
        self.assertEqual(self.r.hot_reload_mod(), 0)
        self.assertEqual(self.evals, [])

    def test_menu_uses_client_context(self):
        P.mod_tree_hashes = lambda: {"a.lua": "2"}
        P.game_state = lambda: "Menu"
        self.r.hot_reload_mod()
        self.assertEqual({c for _, c in self.evals}, {"client"})


class WaitWatchdog(unittest.TestCase):
    def test_reports_after_90s_then_every_30s(self):
        out = []
        w = P.WaitReporter("game state Running", 600, start=0.0, logf=out.append)
        fired = [t for t in range(0, 200, 5) if w.tick(float(t))]
        self.assertEqual(fired, [90, 120, 150, 180])
        self.assertIn("game state Running", out[0])
        self.assertIn("90 s so far, 510 s left", out[0])

    def test_short_wait_silent(self):
        out = []
        w = P.WaitReporter("x", 60, start=0.0, logf=out.append)
        self.assertFalse(any(w.tick(float(t)) for t in range(0, 61)))


class Triage(unittest.TestCase):
    def _session(self, d: Path) -> Path:
        base = d / "S009"
        use = lambda i, name, inp: {"type": "tool_use", "id": f"t{i}", "name": f"mcp__bg3__{name}", "input": inp}
        res = lambda i, text, err=False: {"type": "tool_result", "tool_use_id": f"t{i}", "is_error": err,
                                          "content": [{"type": "text", "text": json.dumps({"result": text})}]}
        calls = [
            (use(1, "move_to", {"x": 1}), res(1, "move_to: done")),
            (use(2, "use_ability", {"target": "e4"}), res(2, "use_ability: FAILED — target is out of range (2.2 m, range 0.0 m)")),
            (use(3, "do_actions", {"actions": "[]"}), res(3, "1. move_to: done\n2. use_ability: FAILED — target is out of range (1.9 m, range 0.0 m)\nSTOPPED: step 2 failed")),
            ({"type": "tool_use", "id": "t4", "name": "ToolSearch", "input": {"query": "select:mcp__bg3__click"}}, res(4, "ok")),
            (use(5, "click", {"x": 1, "y": 2}), res(5, "clicked")),
            (use(6, "click", {"x": 1, "y": 2}), res(6, "clicked")),
            (use(7, "click", {"x": 1, "y": 2}), res(7, "clicked")),
            (use(8, "screenshot", {}), res(8, "img")),
            (use(9, "move_to", {"x": 2}), res(9, "move_to: done")),
            (use(10, "travel_to", {"objective": "o1"}), {"type": "tool_result", "tool_use_id": "t10", "is_error": True,
                                                       "content": "<tool_use_error>Error: No such tool available: travel_to</tool_use_error>"}),
        ]
        stream, turns, t = [], [], 1000.0
        for i, (u, r) in enumerate(calls, 1):
            stream.append({"type": "assistant", "message": {"content": [u]}})
            stream.append({"type": "user", "message": {"content": [r]}})
            turns += [{"type": "use", "t": t, "id": f"t{i}", "name": u["name"]}, {"type": "result", "t": t + 1, "id": f"t{i}"}]
            t += 2 if i != 8 else 30  # a 30 s gap before the 2nd move_to
        stream.append({"type": "result", "result": "Reached the Helm. Saved as S009-end."})
        Path(f"{base}.jsonl").write_text("\n".join(map(json.dumps, stream)) + "\n")
        Path(f"{base}.turns.jsonl").write_text("\n".join(map(json.dumps, turns)) + "\n")
        metrics = [{"type": "tool", "tool": "move_to", "t0": 1000.0, "t1": 1001.0},
                   {"type": "tool", "tool": "move_to", "t0": 1044.0, "t1": 1045.0}]
        Path(f"{base}.metrics.jsonl").write_text("\n".join(map(json.dumps, metrics)) + "\n")
        Path(f"{base}.progress.jsonl").write_text(json.dumps({"type": "stuck", "t": 1500, "since": 1000,
                                                              "idle_min": 8, "actions": 40}) + "\n")
        return base

    def test_report(self):
        with tempfile.TemporaryDirectory() as d:
            base = self._session(Path(d))
            md = P.triage.write(base).read_text()
        self.assertIn("**use_ability** ×2 (via do_actions)", md)              # top-level + batch step, one group
        self.assertIn("range N m", md)
        self.assertIn("**travel_to** ×1", md)                                 # is_error
        self.assertIn("43.0 s", md)                                           # the long gap
        self.assertIn("during: ", md)
        self.assertIn("select:mcp__bg3__click", md)
        self.assertIn("screenshots: 1; clicks: 3", md)
        self.assertIn("`click` repeated 3×", md)
        self.assertIn("no story progress for 8 min", md)
        self.assertIn("Reached the Helm", md)

    def test_combat_gaps_split_own_turn(self):
        # combat: end_turn at 1001, enemies act 1002-1020 (pturn false), next action 1035: 34 s raw, 15 s own;
        # then a 25 s gap with a 19 s enemy turn: not long once enemies are excluded.
        with tempfile.TemporaryDirectory() as d:
            base = Path(d) / "S011"
            metrics = [{"type": "tool", "tool": "end_turn", "t0": 1000.0, "t1": 1001.0},
                       {"type": "tool", "tool": "use_ability", "t0": 1035.0, "t1": 1036.0},
                       {"type": "tool", "tool": "use_ability", "t0": 1061.0, "t1": 1062.0}]
            metrics += [{"type": "sample", "t": float(t), "moving": False, "job": 0, "dlg": False, "combat": True,
                         "pturn": not (1002 <= t <= 1020 or 1037 <= t <= 1055)} for t in range(1000, 1063)]
            Path(f"{base}.metrics.jsonl").write_text("\n".join(map(json.dumps, metrics)) + "\n")
            md = P.triage.write(base).read_text()
        self.assertIn("Idle gaps > 10 s between visible actions (1)", md)
        self.assertIn("**15.0 s** idle (combat; own turn 15.0 s of 34.0 s, enemy turns 19.0 s)", md)
        self.assertIn("not counted: 1 gap(s)", md)
        self.assertIn("25 s = 19 s enemy + 6 s ours", md)  # the parts, not "25 s with 19 s enemy"

    def test_bare_failed_header_takes_the_next_line(self):
        # S034/S037: "equip_best: FAILED\nFAILED: Claude: …" grouped as a bare "FAILED:" plus the real reason
        text = "equip_best: FAILED\nFAILED: Claude: e599 Boots (rare vs common): in a conversation\nMAIN HANDS x"
        self.assertEqual(P.triage.failures("equip_best", {}, text),
                         [("equip_best", "FAILED: Claude: e599 Boots (rare vs common): in a conversation")])

    def test_missing_files_still_write(self):
        with tempfile.TemporaryDirectory() as d:
            md = P.triage.write(Path(d) / "S001").read_text()
        self.assertIn("# Triage S001", md)
        self.assertIn("(none)", md)

    def test_wired_into_run_claude(self):
        src = Path(P.__file__).read_text()
        self.assertIn('self.write_triage(f"{sid}{tag}", base)', src)



class CheckAllBetweenSessions(unittest.TestCase):
    """check_all after the hot reload, before the next kickoff: spacing, verdict, triage append, status."""

    REPORT = {"exit": 1, "counts": {"PASS": 7, "WARN": 1, "FAIL": 1, "SKIP": 0},
              "rows": [{"name": "latency", "verdict": "PASS", "secs": 6.1, "detail": "p50 40 ms", "lines": []},
                       {"name": "vis", "verdict": "FAIL", "secs": 3.0, "detail": "markers | unknown op",
                        "lines": ["[FAIL] markers: unknown op", "[FAIL] second: x"]}]}

    def test_due(self):
        self.assertTrue(P.check_all_due(None, 1000.0))
        self.assertFalse(P.check_all_due(1000.0, 1000.0 + 29 * 60))
        self.assertTrue(P.check_all_due(1000.0, 1000.0 + 30 * 60))

    def test_result(self):
        res = P.check_all_result(1, "", self.REPORT)
        self.assertTrue(res["ok"])
        self.assertEqual(res["line"], "check_all: 7 pass, 1 warn, 1 fail")
        for rc, want in ((2, "not reachable"), (3, "refused"), (None, "timed out")):
            res = P.check_all_result(rc, "", None)
            self.assertFalse(res["ok"])
            self.assertIn(want, res["line"])
        self.assertIn("Traceback boom", P.check_all_result(1, "x\nTraceback boom\n", None)["line"])

    def test_markdown(self):
        md = P.check_all_markdown(P.check_all_result(1, "", self.REPORT), time.time(), 25.4, "after the session ")
        self.assertIn("## check_all after the session (", md)
        self.assertIn("25 s): 7 pass, 1 warn, 1 fail", md)
        self.assertIn("| vis | **FAIL** | 3.0 | markers \\| unknown op |", md)
        self.assertIn("[FAIL] second: x", md)
        self.assertEqual(md.count("\n|"), 5)  # header, rule, 2 rows, 1 extra line

    def test_cmd_uses_bridge_env(self):
        with mock.patch.object(P.shutil, "which", return_value="/u/uv"):
            cmd = P.check_all_cmd(Path("/r/x.json"))
        self.assertEqual(cmd[:2], ["/u/uv", "run"])
        self.assertEqual(cmd[-3:], [str(P.CHECK_ALL), "--json", "/r/x.json"])

    def _runner(self, d: Path, last=None):
        r = P.Runner.__new__(P.Runner)
        r.stop_requested = False
        r.pending_notes = []
        r.state = P.RunnerState.__new__(P.RunnerState)
        r.state.dir, r.state.path, r.state.data = d, d / "runner.json", {"check_all_at": last}
        triage_md = d / "S004.triage.md"
        triage_md.write_text("# S004 triage\n")
        r.last_triage = ("S004", triage_md)
        return r, triage_md

    def _run(self, d: Path, r, report, rc=1, screen="game"):
        logs, calls = [], []

        def fake_run(cmd, **kw):
            calls.append((cmd, kw))
            if report is not None:
                Path(cmd[-1]).write_text(json.dumps(report))
            return mock.Mock(returncode=rc, stdout="", stderr="")

        with mock.patch.object(P, "RUNTIME", d), mock.patch.object(P, "log", logs.append), \
                mock.patch.object(P.subprocess, "run", fake_run), mock.patch.object(P, "ui_screen", return_value=screen):
            res = r.between_sessions_check()
        return res, logs, calls

    def test_runs_appends_and_reports(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            r, triage_md = self._runner(d)
            res, logs, calls = self._run(d, r, self.REPORT)
            self.assertTrue(res["ok"])
            self.assertEqual(calls[0][1]["env"]["BG3AI_CHECK_ALL_RUNNER_PID"], str(P.os.getpid()))
            self.assertTrue(logs[0].startswith("check_all: 7 pass, 1 warn, 1 fail (after S004"))
            self.assertTrue(any(line.startswith("check_all FAIL vis") for line in logs))
            md = triage_md.read_text()
            self.assertTrue(md.startswith("# S004 triage\n"))
            self.assertIn("| vis | **FAIL** |", md)
            st = json.loads((d / "runner.status.json").read_text())["check_all"]
            self.assertEqual((st["session"], st["counts"]["FAIL"], st["ok"]), ("S004", 1, True))
            self.assertIsNotNone(r.state["check_all_at"])
            # within 30 min: skipped, nothing run
            res, logs, calls = self._run(d, r, self.REPORT)
            self.assertIsNone(res)
            self.assertEqual(calls, [])

    def test_no_verdict_retries_next_time(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            r, triage_md = self._runner(d, last=time.time() - 3600)
            res, logs, _ = self._run(d, r, None, rc=2)
            self.assertFalse(res["ok"])
            self.assertIn("not reachable", logs[0])
            self.assertLess(r.state["check_all_at"], time.time() - 3000)  # not recorded: retried next session
            self.assertIn("no verdict", triage_md.read_text())

    def test_screen_fail_is_loud(self):
        # S068 started on screen=map: a map check_all could not close fails the prep loudly and tells the session
        report = dict(self.REPORT, rows=self.REPORT["rows"] + [
            {"name": "screen", "verdict": "FAIL", "secs": 6.0, "lines": [],
             "detail": "screen map after the checks (was game): the next session would start there"}])
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            r, _ = self._runner(d)
            _, logs, _ = self._run(d, r, report)
            self.assertTrue(any(line.startswith("PREP FAIL: the game is not on the game view after check_all: "
                                                "screen map after the checks") for line in logs), logs)
            self.assertEqual(len(r.pending_notes), 1)
            self.assertIn("left the game off the game view (screen map after the checks", r.pending_notes[0])
            self.assertIn("ESCAPE", r.pending_notes[0])

    def test_screen_ok_is_quiet(self):
        report = dict(self.REPORT, rows=self.REPORT["rows"] + [
            {"name": "screen", "verdict": "WARN", "secs": 1.0, "lines": [],
             "detail": "game view; closed a map left open (ESCAPE x1)"}])
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            for rep, screen in ((report, "map"), (self.REPORT, "game")):  # no row: the runner asks ui.state
                r, _ = self._runner(d)
                _, logs, _ = self._run(d, r, rep, screen=screen)
                self.assertEqual(r.pending_notes, [])
                self.assertFalse(any("PREP FAIL" in line for line in logs))

    def test_no_verdict_map_up_is_loud(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            r, _ = self._runner(d)
            _, logs, _ = self._run(d, r, None, rc=None, screen="map")  # timed out mid-waypoints
            self.assertTrue(any(line.startswith("PREP FAIL") and "screen map after check_all (timed out" in line
                                for line in logs), logs)
            self.assertEqual(len(r.pending_notes), 1)

    def test_stop_requested_skips(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            r, _ = self._runner(d)
            r.stop_requested = True
            res, _, calls = self._run(d, r, self.REPORT)
            self.assertIsNone(res)
            self.assertEqual(calls, [])

    def test_wired_after_reload_before_kickoff(self):
        src = Path(P.__file__).read_text()
        body = src[src.index("    def play_session("):]
        self.assertLess(body.index("self.hot_reload_mod()"), body.index("self.between_sessions_check()"))
        self.assertLess(body.index("self.between_sessions_check()"), body.index("build_kickoff("))



class SelfRestart(unittest.TestCase):
    """Between sessions the runner execs itself when its own code changed on disk."""

    def test_code_watch(self):
        with tempfile.TemporaryDirectory() as d:
            a, b = Path(d, "a.py"), Path(d, "b.py")
            a.write_text("x = 1\n")
            b.write_text("y = 1\n")
            w = P.CodeWatch([a])
            self.assertEqual(w.changed([a]), [])
            self.assertEqual(w.changed([a, b]), [])          # a lazily imported module: its hash when first seen
            b.write_text("y = 2\n")
            self.assertEqual(w.changed([a, b]), [b])
            a.unlink()
            self.assertEqual(sorted(w.changed([])), sorted([a, b]))

    def test_loaded_code_files(self):
        repo = Path("/r")
        mods = {"play": mock.Mock(__file__="/r/runner/play.py"),
                "metrics": mock.Mock(__file__="/r/bridge/src/bg3ai/metrics.py"),
                "dep": mock.Mock(__file__="/r/bridge/.venv/lib/site-packages/x.py"),
                "json": mock.Mock(__file__="/usr/lib/python3/json/__init__.py"),
                "ext": mock.Mock(__file__="/r/x.so"), "builtin": object()}
        self.assertEqual(P.loaded_code_files(repo, mods),
                         [Path("/r/bridge/src/bg3ai/metrics.py"), Path("/r/runner/play.py")])
        self.assertIn(Path(P.__file__).resolve(), P.loaded_code_files())

    def test_handover(self):
        with tempfile.TemporaryDirectory() as d:
            h = Path(d, "handover.json")
            P.write_handover({"run_end": 5.0}, h)
            self.assertEqual(P.read_handover(h)["run_end"], 5.0)
            self.assertFalse(h.exists())                     # consumed
            self.assertIsNone(P.read_handover(h))
            P.write_handover({}, h)
            self.assertIsNone(P.read_handover(h, now=time.time() + P.HANDOVER_MAX_AGE + 1))  # stale
            h.write_text(json.dumps({"pid": -1, "t": time.time()}))
            self.assertIsNone(P.read_handover(h))            # another process's

    def _runner(self, stale):
        r = object.__new__(P.Runner)
        r.stop_requested = False
        r.bridge_hashes = None
        r.code_watch = mock.Mock(changed=mock.Mock(return_value=stale), hashes={})
        r.event = mock.Mock()
        return r

    def test_between_sessions(self):
        f = Path(P.__file__).resolve()
        logs = []
        with mock.patch.object(P, "log", logs.append), mock.patch.object(P, "tree_hashes", return_value={"a": "1"}):
            r = self._runner([])
            r.code_update_check()                             # nothing changed
            with mock.patch.object(P, "tree_hashes", return_value={"a": "2", "b": "1"}):
                r.code_update_check()
            self.assertIn("bridge code changed since last session: a, b", logs)
            ok = mock.Mock(returncode=0, stdout="", stderr="")
            r = self._runner([f])
            with mock.patch.object(P.subprocess, "run", return_value=ok) as run:
                with self.assertRaises(P.RestartRunner):
                    r.code_update_check()
            self.assertEqual(run.call_args.args[0][-2:], ["run", "--help"])
            self.assertTrue(any(m.startswith("runner code changed: restarting") for m in logs))
            r = self._runner([f])
            r.stop_requested = True                          # stopping anyway: no restart
            r.code_update_check()
            bad = mock.Mock(returncode=1, stdout="", stderr="Traceback\nSyntaxError: invalid syntax")
            r = self._runner([f])
            with mock.patch.object(P.subprocess, "run", return_value=bad):
                r.code_update_check()                         # broken new code: keep running the old one
            self.assertIn(f, r.code_watch.hashes)            # and don't retry until it changes again
            r.event.assert_called_once()
            self.assertIn("SyntaxError", r.event.call_args.kwargs["error"])

    def test_handover_data_roundtrip(self):
        r = self._runner([])
        r.pending_notes, r.quick_failures, r.new_game = ["note"], 1, True
        r.last_triage = ("S007", Path("/x/S007.triage.md"))
        d = json.loads(json.dumps(r.handover_data(123.0, 100.0)))
        self.assertEqual((d["run_end"], d["run_started"], d["pending_notes"]), (123.0, 100.0, ["note"]))
        self.assertEqual(d["last_triage"], ["S007", "/x/S007.triage.md"])

    def test_fake_runner_execs_itself(self):
        """A real execv: same pid, same argv, the lock taken again, the handover read by the successor."""
        script = (
            "import os, sys\n"
            f"sys.path.insert(0, {str(Path(P.__file__).resolve().parent)!r})\n"
            "import play as P\n"
            "lock = P.acquire_lock()\n"
            "h = P.read_handover()\n"
            "if h is None:\n"
            "    P.write_handover({'n': 1})\n"
            "    print('first', os.getpid(), sys.argv[1:], flush=True)\n"
            "    P.exec_new_runner(lock, None)\n"
            "print('second', os.getpid(), sys.argv[1:], h['n'], flush=True)\n")
        with tempfile.TemporaryDirectory() as d:
            Path(d, "fake_runner.py").write_text(script)
            env = {**__import__("os").environ, "BG3AI_RUNTIME": d}
            p = __import__("subprocess").run([sys.executable, "fake_runner.py", "run", "--x"], cwd=d, env=env,
                                             capture_output=True, text=True, timeout=60)
            self.assertEqual(p.returncode, 0, p.stderr)
            first, second = p.stdout.split("\n")[:2]
            self.assertEqual(first.split()[1], second.split()[1])     # same pid
            self.assertIn("['run', '--x']", second)
            self.assertTrue(second.endswith(" 1"))
            self.assertFalse(Path(d, "runner-handover.json").exists())

class MemorySessionWithoutHooks(unittest.TestCase):
    """The runner opens the memory session and makes the stop check itself (were the SessionStart and Stop hooks),
    through the real tools/memory/session.py on a real memory repo."""

    def test_open_then_stop_check(self):
        import _testutil as TU
        import memlib as M
        import new_playthrough as NP
        TU.isolate_git()
        with tempfile.TemporaryDirectory() as d, mock.patch.object(P, "log", lambda msg: None):
            r = P.Runner.__new__(P.Runner)
            r.opt = P.Options("pt", None, 30, "opus")
            r.mem = Path(d) / "memory"
            NP.create_memory("pt", r.mem)
            deadline = time.time() + 30 * 60
            sid, brief = r.open_memory_session(deadline, 30)
            self.assertEqual(sid, "S001")
            self.assertTrue(brief.startswith("=== BG3 PLAYTHROUGH MEMORY — session S001 ==="))
            self.assertIn("Autonomous session: deadline", brief)
            self.assertIn("min of play before WRAP UP at", r.stop_check(sid, deadline, 30))
            near = time.time() + 120   # inside the last 3 minutes: wrap-up is due
            self.assertIn("HANDOFF.md hasn't been rewritten", r.stop_check(sid, near, 30))
            (r.mem / "HANDOFF.md").write_text("# Handoff S001 → S002\nSave to load: \"S001-end\" — camp\n\n"
                                              "## Doing right now\nResting.\n\n## Next steps\n1. Go north.\n")
            (r.mem / "objectives.md").write_text("# Objectives\n## Main\n- reach camp [seen S001]\n")
            self.assertIsNone(r.stop_check(sid, near, 30))     # ends: linted and committed
            self.assertFalse(M.load_state(r.mem)["open"])
            self.assertEqual(M.git(r.mem, "log", "-1", "--format=%s").stdout.strip(), "S001")

    def test_no_memory_falls_back_to_session_start(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(P, "log", lambda msg: None):
            r = P.Runner.__new__(P.Runner)
            r.opt = P.Options("pt", None, 30, "opus")
            r.mem = Path(d) / "missing"
            self.assertEqual(r.open_memory_session(time.time() + 1800, 30), (None, ""))
            self.assertIsNone(r.stop_check("S001", time.time() + 1800, 30))


class MemoryKeeperHook(unittest.TestCase):
    """Runner.start_keeper/join_keeper: threaded pass, logged to events + keeper_history, joined before play."""

    def test_recover_at_start(self):  # MEM-1: a pass the runner died in is undone before any session
        import memory_keeper as K
        with tempfile.TemporaryDirectory() as d:
            events = []
            mem = Path(d) / "memory"
            mem.mkdir()
            (mem / "quests.md").write_text("# Quests\n")
            K.save_pending(Path(d), mem, "S007", K.snapshot(mem))
            (mem / "quests.md").write_text("# Quests\n- unvalidated [seen]\n")

            class FakeState:
                dir = Path(d)

            class FakeRunner:
                opt = P.Options("pt", None, 20, "opus")
                state = FakeState()
                def event(self, kind, **f): events.append((kind, f))
                recover_keeper = P.Runner.recover_keeper
            r = FakeRunner()
            r.mem = mem
            with mock.patch.object(P, "log", lambda *a, **k: None):
                r.recover_keeper()
            self.assertEqual((mem / "quests.md").read_text(), "# Quests\n")
            self.assertEqual(events[0][0], "keeper_recovered")
            self.assertIn("never finished", events[0][1]["detail"])

    def test_start_and_join(self):
        import memory_keeper as K
        with tempfile.TemporaryDirectory() as d:
            calls, events = [], []

            class FakeState:
                dir = Path(d)

            class FakeRunner:
                opt = P.Options("pt", None, 20, "opus")
                state, mem, keeper_thread = FakeState(), Path(d) / "memory", None
                def event(self, kind, **f): events.append((kind, f))
                def memory_state(self): return {"open": False}
                start_keeper = P.Runner.start_keeper
                join_keeper = P.Runner.join_keeper
                def write_review(self, sid): pass   # SessionReview tests it

            def fake_run(mem, n, base, model, effort, minutes, refused=None, client=None):
                calls.append((n, Path(base).name, model, effort, minutes, client))
                got_refused.append(refused)
                release.wait(10)  # alive until the test looked (a 0.2 s sleep could end first on a loaded machine)
                return K.KeeperResult("S007", status="ok", seconds=0.2, files=["quests.md"], commit="abc")

            release = threading.Event()

            r = FakeRunner()
            res = P.SessionResult("S007", "c", 0, "exited", 0.0, 1200.0)
            got_refused = []
            wyll = {"tool": "level_up", "result": "level_up: FAILED — passive Thirsting Blade: not offered; can pick: "
                    "Devil's Sight\n(plan: Wyll L5 from memory/party-build.md)"}
            with mock.patch.object(K, "run_keeper", fake_run), \
                    mock.patch.object(P, "read_actions_since", lambda t, *a, **k: [wyll]):
                r.start_keeper("S007", res, [{"name": "Gale", "level": 8, "problems": "check_plans …: no plan line"},
                                             {"name": "wyll", "level": 5, "problems": "dup of the refused line"}])
                self.assertTrue(r.keeper_thread.is_alive())
                release.set()
                r.join_keeper()
            self.assertIsNone(r.keeper_thread)
            # default: the play model and the play client (claude: run_keeper's default non-MCP file-tools pass)
            self.assertEqual(calls, [(7, "S007-keeper", "opus", "low", 5.0, "claude")])
            self.assertEqual([(x["name"], x["level"]) for x in got_refused[0]], [("Wyll", 5), ("Gale", 8)])
            self.assertEqual(events[0][0], "memory_keeper")
            self.assertEqual(events[0][1]["status"], "ok")
            hist = json.loads((Path(d) / "keeper_history.jsonl").read_text())
            self.assertEqual(hist["files"], ["quests.md"])
            self.assertEqual((hist["model"], events[0][1]["model"]), ("opus", "opus"))
            self.assertEqual((hist["client"], events[0][1]["client"]), ("claude", "claude"))
            # a codex run keeps its memory with codex (run_keeper then goes through the keeper-mode MCP server)
            r.opt = P.Options("pt", None, 20, "gpt-5", client="codex")
            with mock.patch.object(K, "run_keeper", fake_run), \
                    mock.patch.object(P, "read_actions_since", lambda t, *a, **k: []):
                r.start_keeper("S007", res)
                r.join_keeper()
            self.assertEqual(calls[-1][-1], "codex")
            self.assertEqual(calls[-1][2], "gpt-5")
            r.opt = P.Options("pt", None, 20, "opus")
            # quick exits and --no-memory-keeper skip the pass
            r.start_keeper("S008", P.SessionResult("S008", "c", 0, "exited", 0.0, 30.0))
            self.assertIsNone(r.keeper_thread)
            r.opt = P.Options("pt", None, 20, "opus", keeper=False)
            r.pending_notes = []
            r.start_keeper("S009", res, [{"name": "Gale", "level": 8, "problems": "no plan line"}])
            self.assertIsNone(r.keeper_thread)
            self.assertIn("Gale L8: no plan line", r.pending_notes[0])  # no keeper: the player gets the note


class SessionReview(unittest.TestCase):
    """Runner.write_review: tools/logs/review_session.py's Sxxx.review.md once the keeper pass is done."""

    def _runner(self, d: Path, reviews: list | None = None):
        class FakeState:
            dir = d / "sessions" / "pt"

        class FakeRunner:
            opt = P.Options("pt", None, 20, "opus")
            state, mem, keeper_thread, pending_notes = FakeState(), d / "memory", None, []
            def event(self, kind, **f):
                with open(self.state.dir / "runner_events.jsonl", "a") as fh:
                    fh.write(json.dumps({"t": time.time(), "kind": kind, **f}) + "\n")
            def memory_state(self): return {"open": False}
            start_keeper = P.Runner.start_keeper
            join_keeper = P.Runner.join_keeper
            write_review = P.Runner.write_review
        FakeState.dir.mkdir(parents=True)
        r = FakeRunner()
        if reviews is not None:
            r.write_review = lambda sid: reviews.append(
                (sid, (r.state.dir / "runner_events.jsonl").read_text() if (r.state.dir / "runner_events.jsonl").exists() else ""))
        return r

    def test_real_review_after_the_keeper(self):
        import memory_keeper as K
        with tempfile.TemporaryDirectory() as d:
            r = self._runner(Path(d))
            Triage._session(None, r.state.dir)   # S009 stream + turns + metrics
            logs = []
            res = P.SessionResult("S009", "c", 0, "exited", 0.0, 1200.0)
            with mock.patch.object(K, "run_keeper", lambda *a, **k: K.KeeperResult("S009", status="ok", seconds=1.0,
                                                                                   files=["quests.md"], commit="abc")), \
                    mock.patch.object(P, "read_actions_since", lambda t, *a, **k: []), \
                    mock.patch.object(P, "RUNTIME", Path(d)), mock.patch.object(P, "log", logs.append):
                r.start_keeper("S009", res)
                r.join_keeper()
            md = (r.state.dir / "S009.review.md").read_text()
        self.assertIn("# Review pt/S009", md)
        self.assertIn("keeper: ok in 1.0 s", md)   # written after the keeper's memory_keeper event
        self.assertIn(f"S009: review {r.state.dir / 'S009.review.md'}", logs)

    def test_review_without_a_keeper_pass_and_not_in_dry_run(self):
        with tempfile.TemporaryDirectory() as d:
            reviews = []
            r = self._runner(Path(d), reviews)
            r.start_keeper("S008", P.SessionResult("S008", "c", 0, "exited", 0.0, 30.0))   # too short for a keeper
            self.assertEqual([x[0] for x in reviews], ["S008"])
            r.opt = P.Options("pt", None, 20, "opus", dry_run=True)
            r.start_keeper("S009", P.SessionResult("S009", "c", 0, "exited", 0.0, 1200.0))
            self.assertEqual([x[0] for x in reviews], ["S008"])

    def test_review_even_when_the_keeper_raises(self):
        import memory_keeper as K
        with tempfile.TemporaryDirectory() as d:
            reviews = []
            r = self._runner(Path(d), reviews)

            def boom(*a, **k):
                raise RuntimeError("keeper crashed")
            with mock.patch.object(K, "run_keeper", boom), mock.patch.object(P, "log", lambda *a, **k: None), \
                    mock.patch.object(P, "read_actions_since", lambda t, *a, **k: []), \
                    mock.patch("threading.excepthook", lambda *a: None):
                r.start_keeper("S007", P.SessionResult("S007", "c", 0, "exited", 0.0, 1200.0))
                r.join_keeper()
            self.assertEqual([x[0] for x in reviews], ["S007"])

    def test_failure_and_timeout_never_raise(self):
        with tempfile.TemporaryDirectory() as d:
            r = self._runner(Path(d))
            logs = []
            with mock.patch.object(P, "RUNTIME", Path(d)), mock.patch.object(P, "log", logs.append):
                self.assertIsNone(r.write_review("S001"))     # no transcript: review_session exits 1
                with mock.patch.object(P.subprocess, "run", side_effect=P.subprocess.TimeoutExpired("x", 120)):
                    self.assertIsNone(r.write_review("S001"))
            self.assertIn("S001: review failed (exit 1)", logs[0])
            self.assertIn("no timed calls", logs[0])
            self.assertEqual(logs[1], f"S001: review killed after {P.REVIEW_TIMEOUT} s")


class PlanCheck(unittest.TestCase):
    ROWS = [{"name": "Gale", "level": 6, "plan": {"spells": ["Haste", "Slow"]}, "problems": [], "offers": "pick 2"},
            {"name": "Wyll", "level": 5, "plan": {"passives": ["Thirsting Blade"]},
             "problems": ["passive Thirsting Blade: not offered", "spells: 0 of 1 picks"],
             "offers": "pick 1 of Warlock invocations L5: Devil's Sight, Fiendish Vigour"}]

    def test_all_valid(self):
        text, bad = P.plan_check_result(self.ROWS[:1])
        self.assertEqual(bad, [])
        self.assertIn("1 checked, all valid", text)

    def test_invalid_line_goes_to_keeper_format(self):
        text, bad = P.plan_check_result(self.ROWS)
        self.assertIn("1 of 2 INVALID: Wyll L5: passive Thirsting Blade: not offered", text)
        self.assertEqual((bad[0]["name"], bad[0]["level"]), ("Wyll", 5))
        self.assertIn("Fiendish Vigour", bad[0]["problems"])
        self.assertIn('"Thirsting Blade"', bad[0]["problems"])
        self.assertIn("Wyll L5:", P.plan_note(bad))

    def test_cmd_points_at_memory(self):
        cmd = P.check_plans_cmd(Path("/tmp/x.json"), Path("/m"))
        self.assertEqual(cmd[-4:], ["--json", "/tmp/x.json", "--memory", "/m"])
        self.assertTrue(cmd[-5].endswith("check_plans.py"))


class OomGuard(unittest.TestCase):
    """20:48 S038: the Shadowfell load blew the game up to 13.3 GB; the kernel OOM-killed it (and systemd every
    agent). The runner must spot a game OOM, and refuse a save that OOM-killed the game twice in a row."""
    J = ("1791654500.100000 the test machine systemd[1740]: bg3ai-game-20261010-2000.scope: A process of this unit has been "
         "killed by the OOM killer.\n")
    HERDR = ("1791654510.000000 the test machine systemd[1740]: app-the multiplexer@b676.service: A process of this unit has been killed "
             "by the OOM killer.\n")

    def test_find_game_oom(self):
        self.assertIn("its scope bg3ai-game-20261010-2000.scope", P.find_game_oom(self.J, "20261010-2000", []))
        self.assertIsNone(P.find_game_oom(self.J, "20261010-2100", []))             # another launch's scope
        self.assertIsNone(P.find_game_oom(self.HERDR, "x", []))                       # a test OOM'd in the multiplexer's unit
        spike = {"t": 1791654480.0, "from_mb": 5000, "to_mb": 13300, "seconds": 35}
        why = P.find_game_oom(self.HERDR, "x", [spike])                                # legacy: no scope, but a spike
        self.assertIn("app-the multiplexer@b676.service", why)
        self.assertIn("5000 → 13300 MB", why)
        self.assertIsNone(P.find_game_oom("garbage killed by the OOM killer\n", "x", [spike]))

    def test_streak(self):
        a = P.oom_streak_update(None, "S038", "oom")
        self.assertEqual(a["n"], 1)
        self.assertEqual(P.oom_streak_update(a, "S038", "oom")["n"], 2)
        self.assertEqual(P.oom_streak_update(a, "S037", "oom")["n"], 1)
        self.assertIsNone(P.oom_streak_update(a, "S038", None))                     # a non-OOM death resets

    def _runner(self, d: Path, saves):
        self.saved = (P.list_saves, P.log, P.user_journal, P.read_spikes, P.write_status, P.read_actions_since)
        self.logs, self.events = [], []
        P.list_saves = lambda: saves
        P.log = self.logs.append
        P.user_journal = lambda since: self.J
        P.read_spikes = lambda since, path=None: [{"t": 1.0, "tools": ["20:47:41 jump_to {} 12118ms → NOT READY"]}]
        P.write_status = lambda **f: None
        P.read_actions_since = lambda t, path=None: []
        r = P.Runner.__new__(P.Runner)
        r.opt = P.Options("pt", None, 30, "opus", save_owner="Claude")
        r.mem = d
        r.pending_notes = []
        r.state = type("S", (), {"dir": d, "data": {"save_owner": "Claude", "runner_saves": [],
                                                       "loaded_save": "Claude-2__S038-shadowfell"},
                                  "__getitem__": lambda s, k: s.data.get(k),
                                  "__setitem__": lambda s, k, v: s.data.__setitem__(k, v)})()
        r.event = lambda kind, **f: self.events.append(kind)
        return r

    def tearDown(self):
        if hasattr(self, "saved"):
            (P.list_saves, P.log, P.user_journal, P.read_spikes, P.write_status, P.read_actions_since) = self.saved

    def test_second_oom_from_the_same_save_refuses_it(self):
        saves = [P.Save("Claude-1__S037-end", "Claude", "S037-end", 10.0),
                 P.Save("Claude-2__S038-shadowfell", "Claude", "S038-shadowfell", 20.0)]
        with tempfile.TemporaryDirectory() as td:
            r = self._runner(Path(td), saves)
            r.state["game"] = {"stamp": "20261010-2000", "launched": 1.0}
            self.assertIn("OOM-killed", r.check_game_oom())
            self.assertIsNone(r.check_game_oom())                                   # once per launch
            self.assertEqual(r.choose_save().label, "S038-shadowfell")               # 1st OOM: retried
            r.state["game"] = {"stamp": "20261010-2000", "launched": 2.0}
            r.state["oom_checked"] = None                                            # (2nd launch, same journal)
            r.check_game_oom()
            self.assertEqual(r.state["oom_streak"]["n"], 2)
            self.assertEqual(r.choose_save().label, "S037-end")                      # 2nd: refused
            self.assertIn("oom_save_refused", self.events)
            self.assertTrue(any("REFUSING TO RESUME" in m for m in self.logs))
            self.assertIn("jump_to", r.pending_notes[-1])
            self.assertIn("S037-end", r.pending_notes[-1])

    def test_refused_with_no_other_save_stops(self):
        saves = [P.Save("Claude-2__S038-shadowfell", "Claude", "S038-shadowfell", 20.0)]
        with tempfile.TemporaryDirectory() as td:
            r = self._runner(Path(td), saves)
            r.state["oom_blocked"] = ["Claude-2__S038-shadowfell"]
            with self.assertRaises(P.LaunchGaveUp):
                r.choose_save()

    def test_ramwatch_kill_counts_as_oom(self):
        with tempfile.TemporaryDirectory() as td:
            r = self._runner(Path(td), [])
            P.user_journal = lambda since: ""
            P.read_spikes = lambda since, path=None: (
                [{"kind": "kill", "t": 5.0, "why": "game RSS 9600 MB >= 9500 MB"}] if path else [])
            r.state["game"] = {"stamp": "s", "launched": 1.0}
            self.assertIn("ramwatch killed the game", r.check_game_oom())

    def test_crash_note_names_the_last_three_calls(self):
        acts = [{"t": 10.0, "tool": "get_status", "args": {}, "result": "x"},
                {"t": 1791654449.2, "tool": "do_actions", "args": {"n": 3}, "result": "1. jump_to: FAILED\nSTOPPED"},
                {"t": 1791654481.5, "tool": "move_to", "args": {"pos": [-415.4, -0.2, -1436.6]}, "result": "done"},
                {"t": 1791654493.7, "tool": "jump_to", "args": {"x": -408.5, "y": 1, "z": -1437},
                 "result": "NOT READY: timed out"},
                {"t": 1791654600.0, "tool": "get_status", "args": {}, "result": "after"}]
        note = P.crash_tools_note("S038: game gone", acts, 1791654514.7)
        self.assertIn("jump_to {\"x\":-408.5,\"y\":1,\"z\":-1437}", note)
        self.assertIn("do_actions", note)
        self.assertNotIn("after", note)
        self.assertNotIn("get_status {}", note)
        self.assertIn("route/WAYS", note)

    def test_game_gone_at_start_warns_once(self):
        acts = [{"t": 99.0, "tool": "jump_to", "args": {"x": -408.5}, "result": "NOT READY"}]
        with tempfile.TemporaryDirectory() as td:
            r = self._runner(Path(td), [])
            P.read_actions_since = lambda t, path=None: acts
            r.state["history"] = [{"kind": "play", "session": "S038", "ended": 100.0}]
            r.note_game_gone(None)
            r.note_game_gone(None)
            self.assertEqual(len(r.pending_notes), 1)
            self.assertIn("S038", r.pending_notes[0])
            self.assertIn("jump_to", r.pending_notes[0])
            r.pending_notes.clear()
            r.state["crash_noted"] = None
            r.state["game_killed_at"] = 200.0          # the runner killed it after S038 (age relaunch): no warning
            r.note_game_gone(None)
            self.assertEqual(r.pending_notes, [])

    def test_no_oom_resets(self):
        with tempfile.TemporaryDirectory() as td:
            r = self._runner(Path(td), [])
            P.user_journal = lambda since: ""
            r.state["oom_streak"] = {"save": "Claude-2__S038-shadowfell", "n": 1}
            r.state["game"] = {"stamp": "s", "launched": 1.0}
            self.assertIsNone(r.check_game_oom())
            self.assertIsNone(r.state["oom_streak"])


def _stream(path: Path, *events) -> Path:
    path.write_text("".join(json.dumps(e) + "\n" for e in events))
    return path


def _use(i: str, name: str, inp: dict) -> dict:
    return {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": i, "name": name, "input": inp}]}}


def _res(i: str, text: str, is_error: bool = False) -> dict:
    return {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": i, "is_error": is_error,
                                                     "content": [{"type": "text", "text": text}]}]}}


class FakeUI:
    """ui.state + the ui.key / ui.press RPCs over a game screen. screen None: the game doesn't answer."""

    def __init__(self, screen=None, panels=(), resume_works=True):
        self.screen, self.panels, self.resume_works, self.calls = screen, list(panels), resume_works, []

    def ui_state(self):
        return None if self.screen is None else {"screen": self.screen, "panels_open": self.panels}

    def ui_screen(self):
        return self.screen

    def rpc(self, op, args=None, context="server", timeout=None):
        self.calls.append((op, (args or {}).get("key") or (args or {}).get("button")))
        if op == "ui.key" and args.get("key") == "ESCAPE":
            self.screen = {"game": "game_menu", "game_menu": "game"}.get(self.screen, self.screen)
        elif op == "ui.press" and args.get("button") == "Resume" and self.resume_works and self.screen == "game_menu":
            self.screen = "game"
        return {}


class LongRun(unittest.TestCase):
    """Playthrough mode for many hours: the crash-loop stop, the rolling 24 h cost cap, the status line."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="bg3ai-long-"))
        self.addCleanup(__import__("shutil").rmtree, self.tmp, True)
        self.saved = {n: getattr(P, n) for n in ("RUNTIME", "PLAYTHROUGHS", "COST_PAUSE_CHECK_S", "day_cost")}
        self.addCleanup(lambda: [setattr(P, n, v) for n, v in self.saved.items()])
        P.RUNTIME, P.PLAYTHROUGHS = self.tmp / "runtime", self.tmp / "playthroughs"
        P.RUNTIME.mkdir(parents=True)
        self.ui = FakeUI()   # never the real game's UI (the heat pause presses ESCAPE in it)
        for name in ("ui_state", "ui_screen", "rpc"):
            p = mock.patch.object(P, name, getattr(self.ui, name))
            p.start()
            self.addCleanup(p.stop)

    def test_crash_loop(self):
        ev = [{"t": 1000.0 + 60 * k, "kind": "fault", "fault": "crash"} for k in range(5)]
        self.assertIsNone(P.crash_loop(ev, 1300.0, 0.0))
        ev.append({"t": 1310.0, "kind": "setup_failed"})
        self.assertEqual(P.crash_loop(ev, 1320.0, 0.0), "crash loop: 6 game crashes/hangs/setup failures in 5 min")
        self.assertIsNone(P.crash_loop(ev, 1320.0, 1100.0))                 # an operator restart counts from its start
        self.assertIsNone(P.crash_loop(ev, 1000.0 + 3600 + 61, 0.0))        # the first one left the hour
        agent = [{"t": 1000.0 + k, "kind": "fault", "fault": "agent_hang"} for k in range(9)]
        self.assertIsNone(P.crash_loop(agent, 1100.0, 0.0))                 # the agent's hangs aren't the game's

    def test_status_line(self):
        seen = {"regions": ["Lower City"], "areas": ["Lower City|Bhaal Temple"]}
        line = P.status_line(1_000_000.0, "claude-run-2", "playing S070 (until 16:30)", 1_000_000.0 - 5400, 3, seen,
                             12.345, 40.0, 1)
        self.assertRegex(line, r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d claude-run-2: playing S070 \(until 16:30\) \| up 1.5 h, "
                               r"3 session\(s\) \| milestones \d+/\d+ \(Act 3\), next .+ \| \$12.35 in 24 h \(cap \$40\) \| "
                               r"1 game fault\(s\) in 60 min \(stop at 6\)$")
        line = P.status_line(1.0, "pt", "STOPPED (x)", 0.0, 0, dict(seen, game_end={"t": 1}), 0.0, None, 0)
        self.assertIn("THE GAME IS FINISHED", line)
        self.assertNotIn("cap", line)

    def test_cost_pause_waits_and_resumes(self):
        r = P.Runner(P.Options("long", None, 30, "opus", max_cost_per_day=20.0))
        P.COST_PAUSE_CHECK_S = 0.05
        spent = {"usd": 25.0}
        P.day_cost = lambda d, now: spent["usd"]
        self.assertTrue(r.cost_paused())
        self.assertIn("PAUSED: $25.00 in 24 h >= --max-cost-per-day $20", (P.RUNTIME / P.STATUS_LINE).read_text())
        self.assertTrue(r.cost_paused())   # still over: one pause event, not one per check
        spent["usd"] = 19.5
        self.assertFalse(r.cost_paused())
        evs = [json.loads(ln) for ln in (r.state.dir / "runner_events.jsonl").read_text().splitlines()]
        self.assertEqual([(e["kind"], e["spent"]) for e in evs], [("cost_pause", 25.0), ("cost_resume", 19.5)])
        r2 = P.Runner(P.Options("long", None, 30, "opus"))
        self.assertFalse(r2.cost_paused())   # no cap: never

    def thermal(self, temps: list[tuple[int, int]], end: float | None = None) -> None:
        """runtime/thermal.log in thermal_log.sh's format: one line per 5 s ending at `end` (default now)."""
        end = time.time() if end is None else end
        t0 = end - 5 * (len(temps) - 1)
        (P.RUNTIME / "thermal.log").write_text("".join(
            f"{P.datetime.fromtimestamp(t0 + 5 * i):%Y-%m-%d %H:%M:%S} cpu={c} acpi={c} amdgpu=50 nvme=40 "
            f"nv=[{g},35.0,1500,99,0x0] profile=balanced load=5.0 bg3=up\n" for i, (c, g) in enumerate(temps)))

    def test_heat_pause_waits_until_cool(self):
        # the test machine cut its power twice under the naut series load: no session starts while it is too hot
        r = P.Runner(P.Options("long", None, 30, "opus"))
        self.thermal([(93, 70)] * 14)                                    # 65 s at 93 C (pause at 92 C for 60 s)
        sleeps = []

        def sleep(s):   # the next check: cool for a minute
            sleeps.append(s)
            self.thermal([(93, 70)] * 3 + [(80, 70)] * 12 if len(sleeps) == 1 else [(80, 60)] * 14)
        with mock.patch.object(P.time, "sleep", side_effect=sleep):
            self.assertTrue(r.heat_paused())
        self.assertEqual(sleeps, [P.HEAT_PAUSE_CHECK_S] * 2)              # 1st check: cool for only 55 s
        self.assertIn("PAUSED: too hot (CPU 80 C, GPU 70 C; limit 92/85 C)", (P.RUNTIME / P.STATUS_LINE).read_text())
        evs = [json.loads(ln) for ln in (r.state.dir / "runner_events.jsonl").read_text().splitlines()]
        self.assertEqual([(e["kind"], e["cpu_c"]) for e in evs], [("heat_pause", 93), ("heat_resume", 80)])
        self.thermal([(80, 86)] * 10)                                    # GPU 86 C for 45 s: not yet
        self.assertFalse(r.heat_paused())
        self.thermal([(99, 99)] * 14, end=time.time() - 120)               # stale log: no pause
        self.assertFalse(r.heat_paused())
        (P.RUNTIME / "thermal.log").unlink()
        self.assertFalse(r.heat_paused())                                # no log (no ramwatch): no pause

    def _hot_pause(self, r, logs, stop_at=None):
        """A heat pause that cools at the 2nd check (stop_at: STOP written at that check instead)."""
        self.thermal([(93, 70)] * 14)
        n = []

        def sleep(s):
            if s != P.HEAT_PAUSE_CHECK_S:
                return
            n.append(s)
            self.assertEqual(self.ui.screen, "game_menu" if self.ui.calls else self.ui.screen)  # open while hot
            if stop_at and len(n) == stop_at:
                (P.RUNTIME / "STOP").write_text("x")
            self.thermal([(80, 60)] * 14 if len(n) >= 2 else [(93, 70)] * 14)
        with mock.patch.object(P.time, "sleep", side_effect=sleep), mock.patch.object(P, "log", logs.append):
            return r.heat_paused()

    def test_heat_pause_opens_the_game_menu_and_closes_it_when_cool(self):
        r = P.Runner(P.Options("long", None, 30, "opus"))
        self.ui.screen, logs = "game", []
        self.assertTrue(self._hot_pause(r, logs))
        self.assertEqual(self.ui.calls, [("ui.key", "ESCAPE"), ("ui.press", "Resume")])   # the player's ESC; Resume
        self.assertEqual(self.ui.screen, "game")
        self.assertIn("heat: game menu open (ESCAPE) while it cools", logs)
        self.assertIn("heat: game menu closed", logs)
        self.assertLess(logs.index("heat: game menu closed"), [i for i, x in enumerate(logs) if "playing again" in x][0])

    def test_heat_pause_resume_button_fails_then_escape(self):
        r = P.Runner(P.Options("long", None, 30, "opus"))
        self.ui.screen, self.ui.resume_works, logs = "game", False, []
        with mock.patch.object(P, "HEAT_MENU_WAIT_S", 0.0):
            r.wait_screen = lambda want, timeout=0.0: P.Runner.wait_screen(want, 0.0)
            self.assertTrue(self._hot_pause(r, logs))
        self.assertEqual(self.ui.calls, [("ui.key", "ESCAPE"), ("ui.press", "Resume"), ("ui.key", "ESCAPE")])
        self.assertEqual(self.ui.screen, "game")

    def test_heat_pause_skips_the_menu_where_escape_does_something_else(self):
        for screen, panels, why in (("dialogue", [], "the dialogue screen is up"),
                                    ("loading", [], "the loading screen is up"),
                                    ("movie", [], "the movie screen is up"),
                                    ("game", ["Trade"], "the game screen with Trade is up"),
                                    (None, [], "the game's UI doesn't answer")):
            with self.subTest(screen=screen):
                r = P.Runner(P.Options("long", None, 30, "opus"))
                self.ui.screen, self.ui.panels, self.ui.calls, logs = screen, panels, [], []
                self.assertTrue(self._hot_pause(r, logs))           # still paused: only the menu is skipped
                self.assertEqual(self.ui.calls, [])                 # no key pressed
                self.assertTrue(any(why in x for x in logs), logs)
                self.assertTrue(any("playing again" in x for x in logs))

    def test_heat_pause_stopped_while_paused_still_closes_the_menu(self):
        r = P.Runner(P.Options("long", None, 30, "opus"))
        self.ui.screen, logs = "game", []
        self.assertTrue(self._hot_pause(r, logs, stop_at=1))
        self.assertEqual(self.ui.screen, "game")
        self.assertIn("heat: game menu closed", logs)

    def test_heat_menu_never_after_a_heat_kill(self):
        r = P.Runner(P.Options("long", None, 30, "opus"))
        self.ui.screen = "game"   # (a game the kill left would be dead; nothing is pressed either way)
        self.thermal([(98, 70)] * 4 + [(85, 60)] * 3)
        with mock.patch.object(P.time, "sleep", side_effect=lambda s: self.thermal([(70, 50)] * 14)):
            self.assertTrue(r.heat_paused(after_kill=True))
        self.assertEqual(self.ui.calls, [])

    def test_heat_kill_waits_before_the_relaunch(self):
        r = P.Runner(P.Options("long", None, 30, "opus"))
        r.state["game"] = {"stamp": "s", "launched": time.time() - 600}
        self.assertIsNone(r.heat_killed())
        (P.RUNTIME / "heat_kills.jsonl").write_text(
            json.dumps({"kind": "heat_kill", "t": time.time() - 900, "why": "old launch"}) + "\n"
            + json.dumps({"kind": "heat_kill", "t": time.time() - 10, "why": "CPU 98 C / GPU 70 C for 20 s"}) + "\n")
        self.assertEqual(r.heat_killed(), "CPU 98 C / GPU 70 C for 20 s")
        self.thermal([(98, 70)] * 4 + [(85, 60)] * 3)                    # cooling, but not for 60 s yet
        self.assertFalse(r.heat_paused())                                # between sessions: not a pause
        with mock.patch.object(P.time, "sleep", side_effect=lambda s: self.thermal([(70, 50)] * 14)):
            self.assertTrue(r.heat_paused(after_kill=True))              # after the kill: wait for 60 s cool
        self.thermal([(70, 50)] * 14)
        self.assertFalse(r.heat_paused(after_kill=True))                 # already cool: relaunch at once

    def test_recover_after_a_heat_kill_names_it_and_waits(self):
        r = P.Runner(P.Options("long", None, 30, "opus"))
        r.state["game"] = {"stamp": "s", "launched": time.time() - 600}
        (P.RUNTIME / "heat_kills.jsonl").write_text(json.dumps({"kind": "heat_kill", "t": time.time() - 10,
                                                                "why": "CPU 98 C / GPU 70 C for 20 s"}) + "\n")
        order = []
        with mock.patch.multiple(r, game_alive=lambda: False, check_game_oom=lambda: None,
                                 kill_game=lambda: order.append("kill"), choose_save=lambda: None,
                                 load_save=lambda s: order.append("relaunch") or "S070-end",
                                 heat_paused=lambda after_kill=False: order.append(("cool", after_kill)) or True), \
                mock.patch.object(P, "read_actions_since", return_value=[]):
            r.recover("crashed")
        self.assertEqual(order, ["kill", ("cool", True), "relaunch"])
        self.assertIn("crashed (the heat guard stopped it: CPU 98 C / GPU 70 C for 20 s)", r.pending_notes[0])

    def test_status_prints_the_last_thermal_line(self):
        import contextlib
        import io
        self.thermal([(80, 68)])
        with mock.patch.object(P, "port_open", return_value=False), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            P.cmd_status(P.argparse.Namespace(playthrough=None))
        self.assertRegex(out.getvalue(), r"thermal:  \d{4}-\d\d-\d\d \d\d:\d\d:\d\d cpu=80 .* nv=\[68,")
        (P.RUNTIME / "thermal.log").unlink()
        with mock.patch.object(P, "port_open", return_value=False), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            P.cmd_status(P.argparse.Namespace(playthrough=None))
        self.assertIn("thermal:  - (no runtime/thermal.log)", out.getvalue())

    def test_cli_refuses_a_bad_day_cap(self):
        import contextlib
        import io
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(P.main(["run", "--playthrough", "x", "--max-cost-per-day", "0"]), 2)
        self.assertIn("--max-cost-per-day", err.getvalue())


class SeriesRuns(unittest.TestCase):
    """run --until M --runs N: naming, fresh memory and the same profile per run, the per-run reset (saves, the nav
    kill list, knowledge/), failure classes, the clean flag and the runs table."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="bg3ai-series-"))
        self.addCleanup(__import__("shutil").rmtree, self.tmp, True)
        self.saved = {n: getattr(P, n) for n in ("RUNTIME", "PLAYTHROUGHS", "SE_BG3AI_DIR", "KNOWLEDGE_DIR",
                                                 "PLAY_DIR", "list_saves", "read_actions_since")}
        self.addCleanup(lambda: [setattr(P, n, v) for n, v in self.saved.items()])
        P.RUNTIME, P.PLAYTHROUGHS = self.tmp / "runtime", self.tmp / "playthroughs"
        P.SE_BG3AI_DIR = self.tmp / "se" / "BG3AI"
        P.PLAY_DIR = self.tmp / "play"
        P.KNOWLEDGE_DIR = P.PLAY_DIR / "knowledge"
        P.read_actions_since = lambda t, path=None: []

    # ---------------------------------------------------------------- naming, memory, profile

    def test_run_names_and_the_next_run(self):
        self.assertEqual([P.runs.run_name("naut", k) for k in (1, 2, 3)], ["naut", "naut-r2", "naut-r3"])
        f = self.tmp / "runs.jsonl"
        self.assertEqual(P.runs.next_run_index(P.runs.series_records(f, "naut")), 1)    # no file yet
        f.write_text("".join(json.dumps(r) + "\n" for r in (
            {"playthrough": "naut", "milestone": "nautiloid_escaped"},                   # a plain --until run
            {"series": "naut", "run": 2, "playthrough": "naut-r2", "outcome": "timeout"},
            {"series": "other", "run": 7, "playthrough": "other-r7"})) + "not json\n")
        recs = P.runs.series_records(f, "naut")
        self.assertEqual([r["playthrough"] for r in recs], ["naut", "naut-r2"])
        self.assertEqual(P.runs.next_run_index(recs), 3)

    def test_each_run_gets_a_fresh_memory_and_the_series_profile(self):
        prof = {"character": {"brief": "a dwarf cleric", "roleplay": "strict"}, "difficulty": "Explorer"}
        P.freeze_series("naut", 3, "nautiloid_escaped", prof)
        P.freeze_series("naut", 3, "nautiloid_escaped", {"changed": True})                # frozen once
        # run 1 adopted the name its agent chose: run 2 still gets the brief without it
        (P.PLAYTHROUGHS / "naut").mkdir(parents=True)
        (P.PLAYTHROUGHS / "naut" / "profile.json").write_text(json.dumps(
            {**prof, "character": {**prof["character"], "name": "Mira"}}))
        P.prepare_series_run("naut", "naut", 1)
        P.prepare_series_run("naut", "naut-r2", 2)
        for pt in ("naut", "naut-r2"):
            mem = P.PLAYTHROUGHS / pt / "memory"
            self.assertTrue((mem / "HANDOFF.md").is_file(), pt)
            self.assertTrue((mem / ".git").is_dir(), pt)
            self.assertIn(pt, (mem / "PLAYTHROUGH.md").read_text())
        self.assertEqual(json.loads((P.PLAYTHROUGHS / "naut-r2" / "profile.json").read_text()), prof)
        self.assertEqual(P.load_profile(P.PLAYTHROUGHS / "naut-r2" / "profile.json")["character"]["brief"],
                         "a dwarf cleric")
        # a resumed run keeps what it has
        (P.PLAYTHROUGHS / "naut-r2" / "memory" / "lessons.md").write_text("kept\n")
        P.prepare_series_run("naut", "naut-r2", 2)
        self.assertEqual((P.PLAYTHROUGHS / "naut-r2" / "memory" / "lessons.md").read_text(), "kept\n")

    # ---------------------------------------------------------------- per-run reset

    def _series_runner(self, pt="naut-r2", k=2) -> P.Runner:
        return P.Runner(P.Options(pt, None, 20, "opus", save_owner="Claude", until="nautiloid_escaped",
                                  series="naut", run_index=k))

    def test_a_fresh_run_resets_the_nav_kill_list_saves_and_snapshots_knowledge(self):
        P.SE_BG3AI_DIR.mkdir(parents=True)
        (P.SE_BG3AI_DIR / "nav_guard.json").write_text('{"slow":{"TUT_Avernus_C":{"why":"slow"}}}')
        (P.SE_BG3AI_DIR / "nav_1234.json").write_text('{"levels":{}}')
        (P.SE_BG3AI_DIR / "other.json").write_text("{}")
        P.KNOWLEDGE_DIR.mkdir(parents=True)
        (P.KNOWLEDGE_DIR / "builds.md").write_text("builds\n")
        r = self._series_runner()
        t0 = time.time()
        self.assertEqual(r.begin_run(t0), t0)
        self.assertEqual(sorted(p.name for p in P.SE_BG3AI_DIR.iterdir()), ["other.json"])   # only nav state moved
        self.assertEqual(sorted(p.name for p in (r.state.dir / "reset").iterdir()), ["nav_1234.json", "nav_guard.json"])
        self.assertEqual(json.loads((r.state.dir / "knowledge_snapshot.json").read_text()), {"builds.md": "builds\n"})
        self.assertEqual(r.state["run"]["reset"], {"nav_files": ["nav_1234.json", "nav_guard.json"],
                                                   "knowledge_files": 1})
        self.assertEqual(r.state["saves_after"], t0)
        # saves older than the run (run 1's, same character name) are never this run's
        P.list_saves = lambda: [P.Save("Claude-1__S009-escaped", "Claude", "S009-escaped", t0 - 60),
                                P.Save("Claude-2__S001-start", "Claude", "S001-start", t0 + 60)]
        self.assertEqual(r.choose_save().folder, "Claude-2__S001-start")
        self.assertEqual(r.save_env()["BG3AI_SAVES_AFTER"], str(int(t0)))
        P.list_saves = lambda: [P.Save("Claude-1__S009-escaped", "Claude", "S009-escaped", t0 - 60)]
        self.assertIsNone(r.choose_save())                                        # → a new game
        # a restart of the same run keeps its start and doesn't reset again
        (P.SE_BG3AI_DIR / "nav_guard.json").write_text("{}")
        r2 = self._series_runner()
        self.assertEqual(r2.begin_run(t0 + 500), t0)
        self.assertTrue((P.SE_BG3AI_DIR / "nav_guard.json").exists())

    def test_no_reset_outside_a_series_or_for_a_playthrough_that_already_played(self):
        P.SE_BG3AI_DIR.mkdir(parents=True)
        (P.SE_BG3AI_DIR / "nav_guard.json").write_text("{}")
        r = P.Runner(P.Options("solo", None, 20, "opus", until="nautiloid_escaped"))
        r.begin_run(time.time())
        r2 = self._series_runner("played", 1)
        r2.state["history"] = [{"kind": "play", "session": "S001"}]
        r2.begin_run(time.time())
        self.assertTrue((P.SE_BG3AI_DIR / "nav_guard.json").exists())
        self.assertIsNone(r.state["saves_after"])
        self.assertNotIn("BG3AI_SAVES_AFTER", r.save_env())

    def test_agent_knowledge_edits_are_put_back_and_others_reported(self):
        P.KNOWLEDGE_DIR.mkdir(parents=True)
        (P.KNOWLEDGE_DIR / "builds.md").write_text("builds\n")
        (P.KNOWLEDGE_DIR / "tools.md").write_text("tools\n")
        r = self._series_runner()
        t0 = time.time() - 5
        r.begin_run(t0)
        (P.KNOWLEDGE_DIR / "builds.md").write_text("builds\nthe answer to the nautiloid\n")    # the agent
        (P.KNOWLEDGE_DIR / "new.md").write_text("cached answers\n")                           # the agent
        (P.KNOWLEDGE_DIR / "tools.md").write_text("tools v2\n")                               # an operator
        _stream(r.state.dir / "S001.jsonl",
                _use("a", "Edit", {"file_path": str(P.KNOWLEDGE_DIR / "builds.md")}),
                _use("b", "Write", {"file_path": "knowledge/new.md"}),                     # relative to play/
                _use("c", "Edit", {"file_path": "memory/lessons.md"}))
        k = r.restore_knowledge(t0)
        self.assertEqual(k["agent_edits"], ["builds.md (changed)", "new.md (added)"])
        self.assertEqual(k["other_changes"], ["tools.md (changed)"])
        self.assertEqual((P.KNOWLEDGE_DIR / "builds.md").read_text(), "builds\n")
        self.assertFalse((P.KNOWLEDGE_DIR / "new.md").exists())
        self.assertEqual((P.KNOWLEDGE_DIR / "tools.md").read_text(), "tools v2\n")
        self.assertIn("nautiloid", (r.state.dir / "knowledge_edits" / "builds.md").read_text())   # kept
        self.assertEqual((r.state.dir / "knowledge_edits" / "new.md").read_text(), "cached answers\n")

    def test_knowledge_change_makes_the_run_unclean(self):  # FAIR-2
        P.KNOWLEDGE_DIR.mkdir(parents=True)
        (P.KNOWLEDGE_DIR / "tools.md").write_text("tools\n")
        r = self._series_runner()
        t0 = time.time() - 5
        r.begin_run(t0)
        (P.KNOWLEDGE_DIR / "tools.md").write_text("tools v2\n")   # changed mid-run (by anyone)
        with mock.patch.object(P, "run_record", lambda *a, **k: {"clean": True, "milestone": "m", "wall_min": 1,
                                                                  "sessions": 1, "cost_usd": 0.0, "crashes": 0,
                                                                  "recoveries": 0, "reloads": 0, "harness_failures": 0,
                                                                  "agent_failures": 0, "unknown_failures": 0,
                                                                  "t": time.time()}), \
                mock.patch.object(P.model_time, "model_time", lambda *a, **k: None), \
                mock.patch.object(P.runs, "model_time_field", lambda *a, **k: None):
            r.opt.judge = False
            rec = r.write_run_record(t0)
        self.assertFalse(rec["clean"])
        self.assertTrue(rec["knowledge_changed"])

    def test_play_settings_keep_knowledge_unwritable(self):  # FAIR-2: the agent's only file access is the MCP's
        settings = json.loads((Path(P.__file__).resolve().parents[1] / "play" / ".claude" / "settings.json").read_text())
        deny = settings["permissions"]["deny"]
        for tool in ("Write", "Edit", "MultiEdit", "NotebookEdit", "Bash"):
            self.assertIn(tool, deny)

    # ---------------------------------------------------------------- failure classes

    def kinds(self, fails) -> list[tuple[str, str]]:
        return [(f["kind"], f["class"]) for f in fails]

    def test_runner_events(self):
        fails, ops = P.runs.event_failures([
            {"t": 1, "kind": "runner_start"},
            {"t": 2, "kind": "fault", "fault": "crash", "session": "S001", "detail": "game process exited"},
            {"t": 3, "kind": "recovered", "fault": "crash"},
            {"t": 4, "kind": "fault", "fault": "hang", "session": "S001"},
            {"t": 5, "kind": "fault", "fault": "agent_hang", "session": "S002", "detail": "no claude output"},
            {"t": 6, "kind": "game_oom", "why": "oom-kill"},
            {"t": 7, "kind": "setup_failed", "error": "the main menu never came up"},
            {"t": 8, "kind": "launch_failed", "error": "crash: signal 11"},
            {"t": 9, "kind": "load_refused", "error": "gate"},
            {"t": 10, "kind": "launch_gave_up", "error": "3 launches"},
            {"t": 11, "kind": "recovery_failed", "error": "x"},
            {"t": 12, "kind": "steam_refused", "why": "online"},
            {"t": 13, "kind": "runner_restart", "files": ["runner/play.py"]},
            {"t": 14, "kind": "runner_stop", "why": "stopped"},
            {"t": 15, "kind": "runner_start"},
            {"t": 16, "kind": "milestone", "milestone": "nautiloid_escaped"}])
        self.assertEqual(self.kinds(fails), [
            ("game_crash", "harness"), ("game_hang", "harness"), ("agent_hang", "unknown"), ("game_oom", "harness"),
            ("setup_failed", "harness"), ("launch_failed", "harness"), ("load_refused", "harness"),
            ("launch_gave_up", "harness"), ("recovery_failed", "harness"), ("steam_refused", "harness")])
        self.assertIn("game process exited", fails[0]["detail"])
        self.assertEqual([o["t"] for o in ops], [13, 14, 15])          # self-restart, operator stop, manual start

    def test_session_history(self):
        fails = P.runs.history_failures([
            {"session": "S001", "reason": "exited", "started": 0, "ended": 20, "exit": 1},
            {"session": "S002", "reason": "killed", "started": 100, "ended": 900},
            {"session": "S003", "reason": "exited", "started": 1000, "ended": 3000, "early_exit_min": 12.5},
            {"session": "S004", "reason": "hard_stop", "started": 4000, "ended": 8000, "started_at_game_over": True},
            {"session": "S005", "reason": "milestone", "started": 9000, "ended": 9030}])   # quick, but the goal
        self.assertEqual(self.kinds(fails), [("claude_quick_exit", "harness"), ("claude_killed", "unknown"),
                                             ("early_exit", "agent"), ("party_wipe", "agent")])

    def test_stream_failures(self):
        d = self.tmp / "s"
        d.mkdir()
        s = _stream(d / "S001.jsonl",
                    {"type": "system", "subtype": "init", "mcp_servers": [{"name": "bg3", "status": "failed"}]},
                    _use("1", "mcp__bg3__move_to", {"x": 1}), _res("1", "FAILED: no path to the destination"),
                    _use("2", "mcp__bg3__look", {}), _res("2", "Error executing tool look: KeyError 'pos'", True),
                    _use("3", "mcp__bg3__look", {}), _res("3", "Error executing tool look: KeyError 'pos'", True),
                    _use("4", "mcp__bg3__travel_to", {}), _res("4", "MCP error -32001: Request timed out", True),
                    _use("5", "mcp__bg3__combat_turn", {}), _res("5", "2. end_turn: TIMEOUT — still my turn"),
                    _use("6", "Read", {"file_path": "../runner/play.py"}),
                    _res("6", "Blocked by the sandbox/fairness rule: Read outside memory/", True),
                    _use("7", "mcp__bg3__get_status", {}), _res("7", "HP 0/12 ... screen=game_over"),
                    _use("8", "mcp__bg3__look", {}),
                    _res("8", "e3 goblin 7f0c6a1e-1111-2222-3333-444455556666 hp 7"),
                    _use("9", "mcp__bg3__talk_to", {}), _res("9", "<tool_use_error>InputValidationError: npc</tool_use_error>",
                                                            True),
                    {"type": "assistant", "message": {"content": [{"type": "text", "text": "API Error: 529 overloaded"}]}},
                    {"type": "result", "is_error": True, "result": "Claude AI usage limit reached|1760000000"})
        fails = P.runs.stream_failures(s)
        got = sorted(self.kinds(fails))
        self.assertEqual(got, sorted([
            ("mcp_failed", "harness"), ("claude_api", "harness"), ("claude_api", "harness"),
            ("tool_exception", "harness"), ("tool_timeout", "harness"), ("tool_timeout", "harness"),
            ("fairness_block", "agent"), ("party_wipe", "agent"), ("fairness_leak", "harness")]))
        exc = next(f for f in fails if f["kind"] == "tool_exception")
        self.assertEqual((exc["n"], exc["detail"][:10]), (2, "S001: look"))       # grouped, FAILED: not counted

    def test_failed_tools_and_hand_input_are_unknown(self):
        d = self.tmp / "h"
        d.mkdir()
        s = _stream(d / "S001.jsonl",
                    _use("1", "mcp__bg3__create_character", {}), _res("1", "FAILED: the name box never reported"),
                    _use("2", "mcp__bg3__create_character", {}), _res("2", "FAILED: the name box never reported"),
                    _use("3", "mcp__bg3__click", {"x": 0.5, "y": 0.5}), _res("3", "clicked left at 960,540"),
                    _use("4", "mcp__bg3__move_to", {}), _res("4", "FAILED: destination is beyond remaining movement"),
                    _use("5", "mcp__bg3__combat_turn", {}), _res("5", "1. move: FAILED: no path\n2. attack: done"))
        fails = P.runs.stream_failures(s)
        self.assertEqual(sorted(self.kinds(fails)), [("hand_input", "unknown"), ("tool_failed", "unknown")])
        tf = next(f for f in fails if f["kind"] == "tool_failed")
        self.assertEqual((tf["n"], tf["detail"][:21]), (2, "S001: create_characte"))
        self.assertTrue(P.runs.is_clean("reached", fails, []))   # unknown never breaks "clean"

    def test_stalls_reloads_and_the_outcome(self):
        p = _stream(self.tmp / "S001.progress.jsonl", {"type": "stuck", "t": 50, "idle_min": 12.0, "actions": 40},
                    {"type": "stuck", "t": 200, "idle_min": 15.0, "actions": 60}, {"type": "unstuck", "t": 300})
        self.assertEqual(self.kinds(P.runs.progress_failures([p], 100)), [("stall", "agent")])
        self.assertEqual(self.kinds(P.runs.reload_failures([{"t": 1, "tool": "load_game", "args": {"save": "latest"}},
                                                            {"t": 2, "tool": "look"}])), [("reload", "agent")])
        harness = [{"class": "harness"}]
        self.assertEqual(self.kinds(P.runs.outcome_failure("timeout", "", 9, [])), [("timeout", "agent")])
        self.assertEqual(self.kinds(P.runs.outcome_failure("timeout", "", 9, harness)), [("timeout", "unknown")])
        self.assertEqual(self.kinds(P.runs.outcome_failure("gave_up", "setup", 9, [])), [("gave_up", "harness")])
        self.assertEqual(self.kinds(P.runs.outcome_failure("runner_error", "x", 9, [])), [("runner_error", "harness")])
        self.assertEqual(P.runs.outcome_failure("reached", "", 9, harness), [])

    def test_clean(self):
        agent = [{"class": "agent", "kind": "reload"}, {"class": "unknown", "kind": "agent_hang"}]
        self.assertTrue(P.runs.is_clean("reached", agent, []))
        self.assertFalse(P.runs.is_clean("reached", agent + [{"class": "harness", "kind": "game_crash"}], []))
        self.assertFalse(P.runs.is_clean("reached", [], [{"what": "runner stopped by the operator"}]))
        self.assertFalse(P.runs.is_clean("timeout", [], []))

    def test_run_record_tags_the_whole_run(self):
        d = P.RUNTIME / "sessions" / "naut"
        d.mkdir(parents=True)
        t0 = time.time() - 3600
        _stream(d / "S001.jsonl", _use("1", "mcp__bg3__look", {}), _res("1", "Error executing tool look: boom", True),
                {"type": "result", "num_turns": 3, "total_cost_usd": 1.0, "usage": {"output_tokens": 5}})
        _stream(d / "S001.progress.jsonl", {"type": "stuck", "t": t0 + 60, "idle_min": 11, "actions": 30})
        _stream(d / "runner_events.jsonl", {"t": t0 - 10, "kind": "fault", "fault": "crash"},     # before the run
                {"t": t0 + 1, "kind": "runner_start"}, {"t": t0 + 5, "kind": "fault", "fault": "crash"})
        P.read_actions_since = lambda t, path=None: [{"t": t0 + 30, "tool": "load_game", "args": {"save": "latest"}}]
        hist = [{"kind": "play", "session": "S001", "reason": "milestone", "started": t0 + 2, "ended": t0 + 3000,
                 "minutes": 50}]
        rec = P.run_record(d, t0, t0 + 3600, {"id": "nautiloid_escaped", "label": "Nautiloid escaped",
                                              "session": "S001"}, "naut", hist)
        self.assertEqual((rec["outcome"], rec["clean"], rec["crashes"], rec["reloads"]), ("reached", False, 1, 1))
        self.assertEqual(sorted(self.kinds(rec["failures"])), [("game_crash", "harness"), ("reload", "agent"),
                                                               ("stall", "agent"), ("tool_exception", "harness")])
        self.assertEqual((rec["harness_failures"], rec["agent_failures"], rec["unknown_failures"]), (2, 2, 0))
        self.assertEqual(rec["operator"], [])
        rec = P.run_record(d, t0, t0 + 3600, {}, "naut", hist, "timeout", "time cap used up", take_over=True)
        self.assertEqual((rec["outcome"], rec["milestone"], rec["clean"]), ("timeout", None, False))
        self.assertEqual(rec["failures"][-1]["kind"], "timeout")
        self.assertIn("--take-over", rec["operator"][0]["what"])

    # ---------------------------------------------------------------- the table

    def test_runs_table_and_streak(self):
        recs = [{"series": "naut", "run": 1, "playthrough": "naut", "outcome": "reached", "clean": False,
                 "wall_min": 80, "sessions": 2, "cost_usd": 12.5,
                 "failures": [{"kind": "game_crash", "class": "harness"}, {"kind": "reload", "class": "agent"}]},
                {"series": "naut", "run": 2, "playthrough": "naut-r2", "outcome": "reached", "clean": True,
                 "wall_min": 60, "sessions": 1, "cost_usd": 9,
                 "failures": [{"kind": "stall", "class": "agent", "n": 2}]},
                {"series": "naut", "run": 3, "playthrough": "naut-r3", "outcome": "reached", "clean": True,
                 "wall_min": 55, "sessions": 1, "cost_usd": 8, "failures": []}]
        self.assertEqual((P.runs.clean_streak(recs), P.runs.best_streak(recs)), (2, 2))
        self.assertEqual(P.runs.clean_streak(recs + [{"clean": False}]), 0)
        out = P.runs.runs_table(recs)
        lines = out.splitlines()
        self.assertTrue(lines[0].startswith("run  playthrough  outcome"))
        self.assertIn("naut-r2", lines[3])
        self.assertIn("stall(a)×2", lines[3])
        self.assertIn("game_crash(h)", lines[2])
        self.assertTrue(out.endswith("3 run(s), 2 clean; 2 clean in a row (latest), best streak 2"))

    def test_runs_command(self):
        d = P.RUNTIME / "sessions" / "naut"
        d.mkdir(parents=True)
        (d / "runs.jsonl").write_text(json.dumps({"series": "naut", "run": 1, "playthrough": "naut",
                                                  "outcome": "timeout", "why": "time cap used up", "clean": False,
                                                  "wall_min": 240, "sessions": 4, "cost_usd": 30,
                                                  "failures": [{"t": time.time(), "kind": "timeout", "class": "agent",
                                                                "detail": "time cap used up"}],
                                                  "operator": [{"what": "runner stopped by the operator"}]}) + "\n")
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(P.main(["runs", "--playthrough", "naut", "--failures"]), 0)
        out = buf.getvalue()
        self.assertIn("0 clean in a row", out)
        self.assertIn("agent    timeout: time cap used up", out)
        self.assertIn("operator: runner stopped by the operator", out)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(P.main(["runs", "--playthrough", "nothing"]), 1)

    def _plan(self, *argv: str, env: dict | None = None, unset: tuple = (), xwayland: str = "") -> str:
        import contextlib
        import io
        from unittest import mock
        pins = {"game_build": "25605617", "repo": {"commit": "abc", "dirty": False, "diff": None}}
        buf = io.StringIO()
        with mock.patch.object(P, "acquire_lock", side_effect=AssertionError("--plan took the runner lock")), \
                mock.patch.object(P, "Runner", side_effect=AssertionError("--plan built a Runner")), \
                mock.patch.object(P, "current_pins", lambda: pins), \
                mock.patch.object(P.steam_paths, "offline_required", lambda: (True, "marker")), \
                mock.patch.object(P.steam_paths, "steam_offline", lambda: (False, "last login online")), \
                mock.patch.object(P.bg3le_buildinfo, "problems", lambda: []), \
                mock.patch.object(P, "pids_of_game", lambda: []), \
                mock.patch.object(P, "foreign_bg3_pids", lambda: []), \
                mock.patch.dict(P.os.environ, env or {}), \
                mock.patch.object(P.display_env, "pgrep_xwayland", lambda: xwayland), \
                contextlib.redirect_stdout(buf):
            for k in unset:
                P.os.environ.pop(k, None)
            self.assertEqual(P.main(["run", *argv, "--plan"]), 0)
        return buf.getvalue()

    def test_plan_fills_the_display_env_from_xwayland(self):
        # a the multiplexer pane has no DISPLAY/XAUTHORITY: the runner finds them itself (no exports in the start command)
        out = self._plan("--playthrough", "np", unset=("DISPLAY", "XAUTHORITY"),
                         xwayland="9 /usr/bin/Xwayland :1 -rootless -auth /run/user/1000/xauth_Ab -listenfd 4\n")
        self.assertIn("display:        DISPLAY=:1 XAUTHORITY=/run/user/1000/xauth_Ab", out)
        self.assertNotIn("NO XAUTHORITY", out)
        out = self._plan("--playthrough", "np", unset=("DISPLAY", "XAUTHORITY"), xwayland="")
        self.assertIn("display:        DISPLAY=:1 XAUTHORITY=unset", out)
        self.assertIn("NO XAUTHORITY", out)
        with tempfile.NamedTemporaryFile() as x:
            out = self._plan("--playthrough", "np", env={"DISPLAY": ":7", "XAUTHORITY": x.name},
                             xwayland="9 /usr/bin/Xwayland :1 -auth /run/user/1000/xauth_Ab\n")
        self.assertIn(f"display:        DISPLAY=:7 XAUTHORITY={x.name}", out)   # inherited values win
        # naut3: an exported value from before Xwayland restarted (the file is gone) is replaced
        out = self._plan("--playthrough", "np", env={"DISPLAY": ":7", "XAUTHORITY": "/run/user/1000/xauth_old"},
                         xwayland="9 /usr/bin/Xwayland :1 -auth /run/user/1000/xauth_Ab\n")
        self.assertIn("display:        DISPLAY=:7 XAUTHORITY=/run/user/1000/xauth_Ab", out)
        out = self._plan("--playthrough", "np", env={"DISPLAY": ":7", "XAUTHORITY": "/run/user/1000/xauth_old"},
                         xwayland="")
        self.assertIn("XAUTHORITY=unset", out)
        self.assertIn("NO XAUTHORITY", out)

    def test_plan_prints_the_freeze_and_the_command_and_writes_nothing(self):
        out = self._plan("--playthrough", "np", "--until", "nautiloid_escaped", "--runs", "3", "--model", "opus",
                         "--effort", "medium", "--session-minutes", "30", "--character-brief", "A watch sergeant",
                         "--roleplay", "strict")
        self.assertIn("series:         np: run 1/3 next (np), 0 recorded", out)
        start = out.index("would be:") + len("would be:")
        frozen = json.loads(out[start:out.index("\n}\n", start) + 3])
        self.assertEqual((frozen["series"], frozen["runs"], frozen["until"]), ("np", 3, "nautiloid_escaped"))
        self.assertEqual(frozen["profile"]["character"], {"brief": "A watch sergeant", "roleplay": "strict"})
        self.assertEqual((frozen["client"], frozen["sandbox"], frozen["judge_model"]),
                         ("claude", "none", P.judge.DEFAULT_MODEL))
        self.assertEqual(frozen["pins"]["game_build"], "25605617")
        self.assertIn("THE RUNNER WOULD REFUSE TO LAUNCH", out)   # Steam online where offline is required
        cmd = json.loads(out[out.index('{\n "cmd"'):])
        self.assertEqual(cmd["cmd"][cmd["cmd"].index("--model") + 1], "opus")
        self.assertEqual(cmd["cmd"][cmd["cmd"].index("--effort") + 1], "medium")
        self.assertEqual(cmd["env"]["BG3AI_PLAYTHROUGH"], "np")
        self.assertEqual(cmd["env"]["BG3_SESSION_MINUTES"], "30.0")
        written = [x for d in (P.RUNTIME, P.PLAYTHROUGHS) if d.exists() for x in d.rglob("*")]
        self.assertEqual(written, [])

    def test_plan_of_a_resume_shows_the_next_session_save_and_hidden_saves(self):
        # claude-run-2 resume check (2026-10-17): --plan printed "S001" and no save for a playthrough at S065
        from unittest import mock
        mem = P.PLAYTHROUGHS / "cr" / "memory"
        (mem / "sessions").mkdir(parents=True)
        for n in (64, 65):
            (mem / "sessions" / f"S{n:03d}.md").write_text(f"# S{n:03d}\n")
        (mem / "HANDOFF.md").write_text('Save to load: "S065-end"\n')
        d = P.RUNTIME / "sessions" / "cr"
        d.mkdir(parents=True)
        t = 1_000_000.0
        (d / "runner.json").write_text(json.dumps({"save_owner": "Claude", "history": [
            {"kind": "play", "session": "S065", "started": t, "ended": t + 1800}]}))
        (d / "session-config.json").write_text(json.dumps({"minutes": 30, "effort": "high"}))
        saves = [P.Save("Claude-1__S065-end", "Claude", "S065-end", t + 1700),
                 P.Save("Claude-2__S027-end", "Claude", "S027-end", t + 90000),       # another playthrough's
                 P.Save("Claude-3__S999-savetest", "Claude", "S999-savetest", t + 95000)]
        with mock.patch.object(P, "list_saves", lambda: saves):
            out = self._plan("--playthrough", "cr", "--save-owner", "Claude", "--session-minutes", "20")
        self.assertIn("resume:         S066; loads Claude-1__S065-end (HANDOFF names S065-end)", out)
        self.assertIn("saves hidden:   Claude-2__S027-end, Claude-3__S999-savetest", out)
        self.assertIn("session-config: 30 min, model opus, effort high", out)
        cmd = json.loads(out[out.index('{\n "cmd"'):])
        self.assertEqual(cmd["env"]["BG3AI_SESSION"], "S066")
        self.assertEqual(cmd["env"]["BG3_SESSION_MINUTES"], "30.0")
        self.assertEqual(cmd["env"]["BG3AI_SAVES_EXCLUDE"], "Claude-2__S027-end\nClaude-3__S999-savetest")
        self.assertEqual(cmd["cmd"][cmd["cmd"].index("--effort") + 1], "high")

    def test_plan_of_a_frozen_series_shows_the_next_run_and_drift(self):
        d = P.RUNTIME / "sessions" / "np"
        d.mkdir(parents=True)
        meta = {"series": "np", "runs": 3, "until": "nautiloid_escaped", "profile": {}, "created": 1.0,
                "pins": {"game_build": "1"}, "caps": {}, "judge_model": "j", "client": "claude"}
        (d / "series.json").write_text(json.dumps(meta))
        (d / "runs.jsonl").write_text(json.dumps({"series": "np", "run": 1, "playthrough": "np",
                                                  "outcome": "reached"}) + "\n")
        before = sorted(P.RUNTIME.rglob("*"))
        out = self._plan("--playthrough", "np", "--until", "nautiloid_escaped", "--runs", "3", "--judge-model", "j",
                         "--allow-judge-change")
        self.assertIn("run 2/3 next (np-r2), 1 recorded", out)
        self.assertIn("exists with every key (frozen", out)
        self.assertIn("REFUSED (--allow-version-drift)", out)
        self.assertEqual(sorted(P.RUNTIME.rglob("*")), before)

    def test_cli_refuses_runs_without_until(self):
        import contextlib
        import io
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(P.main(["run", "--playthrough", "x", "--runs", "3"]), 2)
            self.assertEqual(P.main(["run", "--playthrough", "x", "--until", "nautiloid_escaped", "--runs", "0"]), 2)
            self.assertEqual(P.main(["run", "--playthrough", "x", "--run-hours", "2"]), 2)
        self.assertIn("--until", err.getvalue())


    # ---------------------------------------------------------------- a half-played run (naut2 run 2, 2026-10-16)

    def _half_run(self, pt="np-r2", k=2, finished=False):
        """naut2 run 2 as the machine crash left it: runner.json run start, a S001 stream, the memory session
        open with edits, saves/<pt>.txt and a save since the start; no runs.jsonl record."""
        from unittest import mock
        d = P.RUNTIME / "sessions" / "np"
        d.mkdir(parents=True, exist_ok=True)
        (d / "series.json").write_text(json.dumps({"series": "np", "runs": 3, "until": "nautiloid_escaped",
                                                   "profile": {"character": {"brief": "a sergeant"}}}))
        (d / "runs.jsonl").write_text("" if k == 1 else json.dumps(
            {"series": "np", "run": 1, "playthrough": "np", "outcome": "reached"}) + "\n")
        (d / "runner_events.jsonl").write_text('{"kind": "runner_start"}\n')
        P.prepare_series_run("np", pt, k)
        sd = P.RUNTIME / "sessions" / pt
        sd.mkdir(parents=True, exist_ok=True)
        hist = [{"kind": "play", "session": "S001", "started": 1001.0}] if finished else []
        (sd / "runner.json").write_text(json.dumps({"history": hist, "runner_saves": [], "saves_after": 1000.0,
                                                    "run": {"started": 1000.0, "series": "np", "index": k}}))
        (sd / "S001.jsonl").write_text("{}\n")
        mem = P.PLAYTHROUGHS / pt / "memory"
        (mem / ".session.json").write_text(json.dumps({"current": 1, "open": True}))
        (mem / "CHARACTER.md").write_text("# Character: Brannoc Vale\n")
        (P.RUNTIME / "saves").mkdir(parents=True, exist_ok=True)
        P._save_record_path(pt).write_text("Brannoc Vale-51612617911__S001-start\n")
        saves = [P.Save("Brannoc Vale-51612617911__S001-start", "Brannoc Vale", "S001-start", 1100.0),
                 P.Save("Corran Vell-1__S001-start", "Corran Vell", "S001-start", 900.0)]   # run 1's: older
        P.list_saves = lambda: saves
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(P, "HANDOVER", self.tmp / "runtime" / "runner-handover.json").start()
        return sd, mem

    def test_half_run_state_reads_what_a_crash_left(self):
        P.list_saves = lambda: []
        P.prepare_series_run("np", "np-r2", 2)
        self.assertIsNone(P.half_run_state("np-r2"))     # a fresh run: template memory only
        sd, mem = self._half_run()
        h = P.half_run_state("np-r2")
        self.assertEqual((h["started"], h["finished_sessions"], h["streams"], h["open_session"]),
                         (1000.0, [], ["S001.jsonl"], "S001"))
        self.assertTrue(any("CHARACTER.md" in x for x in h["uncommitted"]))
        self.assertEqual(h["recorded_saves"], ["Brannoc Vale-51612617911__S001-start"])
        self.assertEqual(h["saves_since_start"], ["Brannoc Vale-51612617911__S001-start"])
        self.assertTrue(h["restartable"])
        lines = "\n".join(P.half_run_lines(h))
        self.assertIn("HALF-PLAYED:    np-r2 started", lines)
        self.assertIn("session S001 open", lines)
        self.assertIn("RESTARTS it (no finished play session)", lines)
        self.assertIn("CONTINUES",
                      "\n".join(P.half_run_lines({**h, "restartable": False, "finished_sessions": ["S001"]})))
        self.assertIn("RESTARTS it (--restart-run)",
                      "\n".join(P.half_run_lines({**h, "restartable": False}, forced=True)))
        self.assertNotIn(", \n", lines)
        busy = "\n".join(P.half_run_lines(h, active=True))
        self.assertIn("a runner holds the lock", busy)
        self.assertNotIn("RESTARTS", busy)

    def test_plan_warns_about_a_half_played_run_and_writes_nothing(self):
        self._half_run()
        before = sorted(x for d in (P.RUNTIME, P.PLAYTHROUGHS) for x in d.rglob("*"))
        out = self._plan("--playthrough", "np", "--until", "nautiloid_escaped", "--runs", "3", "--allow-judge-change")
        self.assertIn("run 2/3 next (np-r2), 1 recorded", out)
        self.assertIn("HALF-PLAYED:    np-r2", out)
        self.assertIn("-> run RESTARTS it", out)
        self.assertEqual(sorted(x for d in (P.RUNTIME, P.PLAYTHROUGHS) for x in d.rglob("*")), before)
        self.assertTrue(P.HANDOVER.parent.is_dir())

    def _restart(self, pt="np-r2", k=2, forced=False):
        logged = []
        from unittest import mock
        with mock.patch.object(P, "log", logged.append):
            h = P.restart_half_run(P.Options(pt, None, 20, "opus", until="nautiloid_escaped", series="np",
                                             run_index=k), "np", pt, k, forced)
        return h, logged

    def test_a_crashed_run_without_a_finished_session_restarts_from_scratch(self):
        sd, mem = self._half_run()
        h, logged = self._restart()
        self.assertIsNotNone(h)
        self.assertFalse(sd.exists())
        self.assertFalse((P.PLAYTHROUGHS / "np-r2").exists())
        self.assertFalse(P._save_record_path("np-r2").exists())
        arch = [x.name for x in (P.RUNTIME / "sessions").iterdir() if x.name.startswith("np-r2.restarted-")]
        self.assertEqual(len(arch), 1)
        self.assertTrue((P.RUNTIME / "sessions" / arch[0] / "S001.jsonl").is_file())        # moved, not deleted
        self.assertTrue((P.PLAYTHROUGHS / arch[0] / "memory" / "CHARACTER.md").is_file())
        self.assertTrue((P.RUNTIME / "saves" / f"{arch[0]}.txt").is_file())
        self.assertIn("RESTARTED from scratch (its runner died without a handover", logged[-1])
        # then run_series' prepare_series_run gives it a template memory and the series' profile again
        P.prepare_series_run("np", "np-r2", 2)
        self.assertIsNone(P.half_run_state("np-r2"))
        self.assertFalse((P.PLAYTHROUGHS / "np-r2" / "memory" / "CHARACTER.md").exists())
        # and the fresh Runner resets the run (begin_run: saves_after = now, so the crashed saves are old)
        r = self._series_runner("np-r2", 2)
        self.assertIsNone(r.state["run"])
        self.assertGreater(r.begin_run(5000.0), 1100.0)
        self.assertEqual(r.state["saves_after"], 5000.0)

    def test_a_run_with_a_finished_session_continues_unless_forced(self):
        sd, mem = self._half_run(finished=True)
        h, _ = self._restart()
        self.assertIsNone(h)
        self.assertTrue((sd / "runner.json").is_file())
        h, logged = self._restart(forced=True)
        self.assertEqual(h["finished_sessions"], ["S001"])
        self.assertFalse(sd.exists())
        self.assertIn("(--restart-run;", logged[-1])

    def test_never_restarts_its_own_execv_successor(self):
        sd, mem = self._half_run()
        P.write_handover({"run_started": 1000.0}, P.HANDOVER)   # same pid: this process is the successor
        h, _ = self._restart(forced=True)
        self.assertIsNone(h)
        self.assertTrue((sd / "runner.json").is_file())
        self.assertTrue(P.HANDOVER.exists())                      # peeked, not consumed: the Runner reads it

    def test_restarting_run_1_keeps_the_series_files_and_profile(self):
        sd, mem = self._half_run(pt="np", k=1)
        (P.PLAYTHROUGHS / "np" / "profile.json").write_text(json.dumps(
            {"character": {"brief": "a sergeant", "name": "Brannoc Vale"}}))   # the half run adopted its name
        h, _ = self._restart(pt="np", k=1)
        self.assertIsNotNone(h)
        self.assertEqual(sorted(x.name for x in sd.iterdir()), ["runner_events.jsonl", "runs.jsonl", "series.json"])
        arch = next(x for x in (P.RUNTIME / "sessions").iterdir() if x.name.startswith("np.restarted-"))
        self.assertTrue((arch / "runner.json").is_file())
        P.prepare_series_run("np", "np", 1)
        self.assertEqual(json.loads((P.PLAYTHROUGHS / "np" / "profile.json").read_text()),
                         {"character": {"brief": "a sergeant"}})          # the frozen brief, no half-run name

    def test_restart_run_needs_runs(self):
        import contextlib
        import io
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(P.main(["run", "--playthrough", "x", "--until", "nautiloid_escaped", "--restart-run"]), 2)
        self.assertIn("--restart-run", err.getvalue())

    # ---------------------------------------------------------------- the game at the end (naut2 left it running)

    def _series_with_fake_runners(self, keep_game=False, n=2):
        from unittest import mock
        calls = []

        class FakeRunner:
            def __init__(fr, opt):
                fr.opt, fr.outcome, fr.fatal, fr.stop_requested, fr.profile = opt, None, False, False, {}

            def run(fr):
                path = P.RUNTIME / "sessions" / "np" / "runs.jsonl"
                path.parent.mkdir(parents=True, exist_ok=True)
                with open(path, "a") as f:
                    f.write(json.dumps({"series": "np", "run": fr.opt.run_index, "playthrough": fr.opt.playthrough,
                                        "outcome": "reached"}) + "\n")
                fr.outcome = "reached"
                return 0

            def end_run_game(fr, when="between runs", after=""):
                calls.append((fr.opt.run_index, when))

            def event(fr, *a, **k):
                pass

        opt = P.Options("np", None, 20, "opus", until="nautiloid_escaped", series="np", keep_game=keep_game)
        with mock.patch.object(P, "Runner", FakeRunner), \
                mock.patch.object(P, "prepare_series_run", lambda *a, **k: None), \
                mock.patch.object(P, "freeze_series", lambda *a, **k: None), \
                mock.patch.object(P, "gate_series_run", lambda *a, **k: None), \
                mock.patch.object(P, "current_pins", lambda: {}), \
                mock.patch.object(P, "restart_half_run", lambda *a, **k: None), \
                mock.patch.object(P, "log"):
            P.run_series(opt, n, {"runner": None, "stop": False})
        return calls

    def test_a_series_stops_its_last_game_unless_keep_game(self):
        self.assertEqual(self._series_with_fake_runners(), [(1, "between runs"), (2, "series end")])
        __import__("shutil").rmtree(P.RUNTIME / "sessions" / "np")
        self.assertEqual(self._series_with_fake_runners(keep_game=True), [(1, "between runs")])

    def test_stop_game_at_end_only_for_until_runs(self):
        from unittest import mock
        r = mock.Mock()
        until = P.Options("x", None, 20, "opus", until="nautiloid_escaped")
        self.assertTrue(P.stop_game_at_end(r, until, "run end"))
        r.end_run_game.assert_called_once_with("run end", "--keep-game leaves it up")
        r.reset_mock()
        for opt in (P.Options("x", None, 20, "opus"),                                   # continuous play
                    P.replace(until, keep_game=True), P.replace(until, dry_run=True)):
            self.assertFalse(P.stop_game_at_end(r, opt, "run end"))
        r.end_run_game.assert_not_called()
        self.assertFalse(P.stop_game_at_end(None, until, "run end"))

    def test_end_run_game_names_when(self):
        from unittest import mock
        r = self._series_runner()
        logged = []
        with mock.patch.object(P, "port_open", lambda: True), mock.patch.object(r, "owns_game", lambda: True), \
                mock.patch.object(r, "kill_game") as kill, mock.patch.object(P, "log", logged.append):
            r.end_run_game("series end", "--keep-game leaves it up")
        kill.assert_called_once()
        self.assertEqual(logged, ["series end: game stopped; --keep-game leaves it up"])
        with mock.patch.object(P, "port_open", lambda: True), mock.patch.object(r, "owns_game", lambda: False), \
                mock.patch.object(r, "kill_game") as kill, mock.patch.object(P, "log", logged.append):
            r.end_run_game("run end")
        kill.assert_not_called()                                   # not this runner's game: never killed
        self.assertEqual(logged[-1], "run end: the game up is not one this runner launched; left running")

    # ---------------------------------------------------------------- fix-notes after the --until stop

    HANDOFF_ONLY = ("ERROR  handoff     HANDOFF.md was written by S000, not by the current session S001. Rewrite it\n"
                    "ERROR  handoff     HANDOFF.md is missing the required '## Next steps' line/section\n")
    REAL = HANDOFF_ONLY + "ERROR  objectives  objectives.md: o3 has no status\n"

    def test_until_stop_handoff_only(self):
        self.assertTrue(P.until_stop_handoff_only("milestone", True, self.HANDOFF_ONLY))
        self.assertFalse(P.until_stop_handoff_only("milestone", True, self.REAL))          # a real notes problem
        self.assertFalse(P.until_stop_handoff_only("milestone", True, "Traceback: boom"))  # session.py failed
        self.assertFalse(P.until_stop_handoff_only("exited", True, self.HANDOFF_ONLY))     # it had its wrap-up time
        self.assertFalse(P.until_stop_handoff_only("milestone", False, self.HANDOFF_ONLY))

    def _close(self, report: str, reason="milestone"):
        from unittest import mock
        r = self._series_runner()
        fixes, events = [], []
        res = mock.Mock(sid="S001", reason=reason, claude_session="u", started=0.0, ended=500.0)
        with mock.patch.object(r, "memory_state", return_value={"open": True}), \
                mock.patch.object(r, "session_py", return_value=mock.Mock(returncode=1, stdout=report)), \
                mock.patch.object(r, "run_claude", side_effect=lambda *a, **k: fixes.append(a)), \
                mock.patch.object(r, "event", side_effect=lambda kind, **f: events.append(kind)), \
                mock.patch.object(P, "log"):
            r.close_memory_session(res)
        return fixes, events

    def test_no_fix_notes_session_after_the_milestone_when_only_the_handoff_fails(self):
        fixes, events = self._close(self.HANDOFF_ONLY)
        self.assertEqual(fixes, [])
        self.assertIn("fix_notes_skipped", events)
        fixes, events = self._close(self.REAL)
        self.assertEqual(len(fixes), 1)                 # a real lint failure still gets its fix pass
        self.assertEqual(fixes[0][0], "S001")
        fixes, _ = self._close(self.HANDOFF_ONLY, reason="exited")
        self.assertEqual(len(fixes), 1)                 # not the --until stop: the agent skipped its wrap-up

class InflightStuck(unittest.TestCase):
    """A hung game's in-flight background search is marked stuck by the runner (the mod's Tick froze with it)."""

    def test_marks_fresh_inflight_once(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "nav_guard.json"
            f.write_text(json.dumps({"strikes": [], "inflight": {"level": "SCL_Main_A", "op": "ways warm"}}))
            self.assertEqual(P.mark_inflight_stuck(f), "SCL_Main_A")
            self.assertTrue(json.loads(f.read_text())["inflight"]["stuck"])
            self.assertIsNone(P.mark_inflight_stuck(f))          # already stuck

    def test_per_playthrough_file_and_per_op_markers(self):  # NAV-3 + NAV-7
        with tempfile.TemporaryDirectory() as d:
            old = Path(d) / "nav_guard.json"
            old.write_text(json.dumps({"inflight": {"level": "WLD_Main_A", "op": "ways warm"}}))
            os.utime(old, (1, 1))
            f = Path(d) / "nav_guard_aaaa.json"
            f.write_text(json.dumps({"inflights": {"jump": {"level": "SCL_Main_A", "op": "jump"},
                                                   "ways warm": {"level": "SCL_Main_A", "op": "ways warm"}}}))
            self.assertEqual(P.mark_inflight_stuck(Path(d)), "SCL_Main_A")    # the newest file
            got = json.loads(f.read_text())["inflights"]
            self.assertTrue(got["ways warm"]["stuck"])
            self.assertNotIn("stuck", got["jump"])                             # a jump marker counts anyway
            self.assertNotIn("stuck", json.loads(old.read_text())["inflight"])
            self.assertIsNone(P.mark_inflight_stuck(Path(d) / "none"))

    def test_nothing_in_flight_or_no_file(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "nav_guard.json"
            self.assertIsNone(P.mark_inflight_stuck(f))
            f.write_text(json.dumps({"strikes": {}}))
            self.assertIsNone(P.mark_inflight_stuck(f))
            self.assertNotIn("inflight", json.loads(f.read_text()))


if __name__ == "__main__":
    unittest.main(verbosity=1)

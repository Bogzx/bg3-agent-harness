#!/usr/bin/env python3
"""Unit tests for runner/play.py's pure logic. Never launches claude or the game.

    python3 runner/test_play.py
"""
from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from unittest import mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
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


class KillGame(unittest.TestCase):
    """kill_game with every OS call mocked: nothing is signalled for real."""

    def setUp(self):
        self.saved = (P.pids_of_game, P.port_open, P.os.kill, P.os.killpg, P.time.sleep, P.KILL_GRACE, P.KILL_CONFIRM)
        self.sent = []
        self.alive = {4242}
        self.ignores_term = True

        def kill(pid, sig):
            self.sent.append((pid, sig))
            if sig == P.signal.SIGKILL or not self.ignores_term:
                self.alive.discard(pid)

        P.pids_of_game = lambda: sorted(self.alive)
        P.port_open = lambda *a, **k: bool(self.alive)
        P.os.kill = kill
        P.os.killpg = lambda pg, sig: self.sent.append(("pg", pg, sig))
        P.time.sleep = lambda s: None
        P.KILL_GRACE = 0.01
        P.KILL_CONFIRM = 0.01
        self.r = P.Runner.__new__(P.Runner)
        self.r.game_proc = None
        self.r.state = type("S", (), {"data": {"game": {"pgid": 777}}, "__getitem__": lambda s, k: s.data.get(k),
                                      "__setitem__": lambda s, k, v: s.data.__setitem__(k, v)})()

    def tearDown(self):
        (P.pids_of_game, P.port_open, P.os.kill, P.os.killpg, P.time.sleep, P.KILL_GRACE, P.KILL_CONFIRM) = self.saved

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
        with mock.patch.multiple(P, RUNTIME=tmp, SAVE_PREFIX="", log=lambda m: None), \
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
        self.assertIn('save_game(label="S001-start")', n)
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
        r.state = P.RunnerState.__new__(P.RunnerState)
        r.state.dir, r.state.path, r.state.data = d, d / "runner.json", {"check_all_at": last}
        triage_md = d / "S004.triage.md"
        triage_md.write_text("# S004 triage\n")
        r.last_triage = ("S004", triage_md)
        return r, triage_md

    def _run(self, d: Path, r, report, rc=1):
        logs, calls = [], []

        def fake_run(cmd, **kw):
            calls.append((cmd, kw))
            if report is not None:
                Path(cmd[-1]).write_text(json.dumps(report))
            return mock.Mock(returncode=rc, stdout="", stderr="")

        with mock.patch.object(P, "RUNTIME", d), mock.patch.object(P, "log", logs.append), \
                mock.patch.object(P.subprocess, "run", fake_run):
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
            self.assertIn("Keep playing: 29 minutes left", r.stop_check(sid, deadline, 30))
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

            def fake_run(mem, n, base, model, effort, minutes, refused=None):
                calls.append((n, Path(base).name, model, effort, minutes))
                got_refused.append(refused)
                time.sleep(0.2)
                return K.KeeperResult("S007", status="ok", seconds=0.2, files=["quests.md"], commit="abc")

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
                r.join_keeper()
            self.assertIsNone(r.keeper_thread)
            self.assertEqual(calls, [(7, "S007-keeper", "opus", "low", 5.0)])   # default: the play model
            self.assertEqual([(x["name"], x["level"]) for x in got_refused[0]], [("Wyll", 5), ("Gale", 8)])
            self.assertEqual(events[0][0], "memory_keeper")
            self.assertEqual(events[0][1]["status"], "ok")
            hist = json.loads((Path(d) / "keeper_history.jsonl").read_text())
            self.assertEqual(hist["files"], ["quests.md"])
            self.assertEqual((hist["model"], events[0][1]["model"]), ("opus", "opus"))
            # quick exits and --no-memory-keeper skip the pass
            r.start_keeper("S008", P.SessionResult("S008", "c", 0, "exited", 0.0, 30.0))
            self.assertIsNone(r.keeper_thread)
            r.opt = P.Options("pt", None, 20, "opus", keeper=False)
            r.pending_notes = []
            r.start_keeper("S009", res, [{"name": "Gale", "level": 8, "problems": "no plan line"}])
            self.assertIsNone(r.keeper_thread)
            self.assertIn("Gale L8: no plan line", r.pending_notes[0])  # no keeper: the player gets the note


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

    def test_cli_refuses_runs_without_until(self):
        import contextlib
        import io
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(P.main(["run", "--playthrough", "x", "--runs", "3"]), 2)
            self.assertEqual(P.main(["run", "--playthrough", "x", "--until", "nautiloid_escaped", "--runs", "0"]), 2)
            self.assertEqual(P.main(["run", "--playthrough", "x", "--run-hours", "2"]), 2)
        self.assertIn("--until", err.getvalue())


class InflightStuck(unittest.TestCase):
    """A hung game's in-flight background search is marked stuck by the runner (the mod's Tick froze with it)."""

    def test_marks_fresh_inflight_once(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "nav_guard.json"
            f.write_text(json.dumps({"strikes": [], "inflight": {"level": "SCL_Main_A", "op": "ways warm"}}))
            self.assertEqual(P.mark_inflight_stuck(f), "SCL_Main_A")
            self.assertTrue(json.loads(f.read_text())["inflight"]["stuck"])
            self.assertIsNone(P.mark_inflight_stuck(f))          # already stuck

    def test_nothing_in_flight_or_no_file(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "nav_guard.json"
            self.assertIsNone(P.mark_inflight_stuck(f))
            f.write_text(json.dumps({"strikes": {}}))
            self.assertIsNone(P.mark_inflight_stuck(f))
            self.assertNotIn("inflight", json.loads(f.read_text()))


if __name__ == "__main__":
    unittest.main(verbosity=1)

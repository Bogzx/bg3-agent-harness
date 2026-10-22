#!/usr/bin/env python3
"""Unit tests for runner/play.py's pure logic. Never launches claude or the game.

    python3 runner/test_play.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from unittest import mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import testenv  # noqa: F401  (first: the game's profile folders are a temp dir before play)
# RUN-9: play.RUNTIME is read at import; a test must never append to the real runtime/runner.log (the phantom
# "0.01 s after SIGTERM" kill bursts the orchestrator chased). Tests that need a runtime set their own.
os.environ.setdefault("BG3AI_RUNTIME", tempfile.mkdtemp(prefix="bg3ai-test-runtime-"))
os.environ.setdefault("BG3AI_KNOWLEDGE_LOCK", "0")   # never chmod the real play/knowledge (Knowledge tests patch it)
import play as P  # noqa: E402
from bg3harness.game import controller as gamectl
from bg3harness.runner import agent_side as ragent
from bg3harness.runner import checks_side as rchecks
from bg3harness.runner import codewatch as rcode
from bg3harness.runner import env as renv
from bg3harness.runner import policy as rpolicy
from bg3harness.runner import state as rstate
import legacy_cli as lcli
import legacy_playthrough as lpt
from bg3play.runner import prompts as pprompts
import legacy_series as lser
from bg3harness.game import io as G  # noqa: E402  (the game's I/O, moved out of play.py)

G_FUNCS = {n for n, v in vars(G).items() if callable(v) and getattr(v, "__module__", None) == G.__name__
           and not isinstance(v, type)}


def home(name: str):
    """The module a patched name lives in: the game's I/O functions moved to bg3harness.game.io."""
    return G if name in G_FUNCS else P


class SessionNumbering(unittest.TestCase):
    def test_first_session(self):
        self.assertEqual(rpolicy.predict_session_number([], None), 1)

    def test_after_logs(self):
        self.assertEqual(rpolicy.predict_session_number([1, 2, 3], 3), 4)

    def test_state_ahead_of_logs(self):
        # a session whose log was never written still advances the counter (session.py does the same)
        self.assertEqual(rpolicy.predict_session_number([1, 2], 5), 6)

    def test_gaps(self):
        self.assertEqual(rpolicy.predict_session_number([1, 7, 3], None), 8)

    def test_ids(self):
        self.assertEqual(rpolicy.session_id(7), "S007")
        self.assertEqual(rpolicy.session_id(1234), "S1234")


class DeadlineMath(unittest.TestCase):
    def test_deadlines(self):
        d, hard = rpolicy.deadlines(1000.0, 60)
        self.assertEqual(d, 1000 + 3600)
        self.assertEqual(hard, d + 600)
        self.assertEqual(rpolicy.deadlines(0, 10, grace=30), (600, 630))

    def test_wrap_minutes(self):
        self.assertEqual(rpolicy.wrap_minutes(60), 57)
        self.assertEqual(rpolicy.wrap_minutes(30), 27)
        self.assertEqual(rpolicy.wrap_minutes(20), 17)
        self.assertEqual(rpolicy.wrap_minutes(6), 4)  # tiny harness sessions: wrap-up is a third of the session
        self.assertEqual(rpolicy.wrapup_minutes(90), 3)

    def test_early_exit_minutes(self):
        t0 = 1_000_000.0
        # claude-run-2 S002: exited cleanly after 12.6 of 20 min -> early exit with 7.4 min left
        self.assertEqual(rpolicy.early_exit_minutes("exited", t0, t0 + 12.6 * 60, 20), 7.4)
        self.assertIsNone(rpolicy.early_exit_minutes("exited", t0, t0 + 17.5 * 60, 20))  # normal wrap-up end
        self.assertIsNone(rpolicy.early_exit_minutes("exited", t0, t0 + 25 * 60, 20))    # overtime
        for hard in ("crash", "hang", "hard_stop", "aborted"):
            self.assertIsNone(rpolicy.early_exit_minutes(hard, t0, t0 + 60, 20))

    def test_short_sessions_refused(self):
        a = mock.Mock(session_minutes=15, allow_short_sessions=False)
        with mock.patch.object(lcli, "acquire_lock") as lock, mock.patch("sys.stderr"):
            self.assertEqual(lcli.cmd_run(a), 2)
        lock.assert_not_called()

    def test_budget_unbounded(self):
        self.assertEqual(rpolicy.session_budget(0, None, 60), 60)

    def test_budget_clipped_and_exhausted(self):
        now = 10_000.0
        self.assertEqual(rpolicy.session_budget(now, now + 3 * 3600, 60), 60)
        self.assertEqual(rpolicy.session_budget(now, now + 40 * 60, 60), 40)
        self.assertIsNone(rpolicy.session_budget(now, now + 14 * 60, 60))
        self.assertIsNone(rpolicy.session_budget(now, now - 5, 60))

    def test_kickoff_has_pace_preflight(self):
        k = pprompts.build_kickoff("S004", "pt1", None, [])
        self.assertIn("PACE PRE-FLIGHT", k)
        self.assertIn("Never pass wait=true", k)
        self.assertIn("stuck.md", k)
        self.assertNotIn(time.strftime("%Y-%m-%d"), rpolicy.PACE_PREFLIGHT)  # static: cache-friendly

    def test_kickoff_names_no_deadline_and_has_the_notes(self):
        # S082: any time shown before WRAP UP NOW was read as a clock to beat (early wrap-up 6 sessions in a row)
        k = pprompts.build_kickoff("S004", "pt1", "S003-end", ["NOTE A", "NOTE B"])
        self.assertIn("S004", k)
        self.assertNotRegex(k.split("PACE PRE-FLIGHT")[0], r"\d\d:\d\d|deadline|time left")
        self.assertIn("you'll be told when to wrap up", k)
        self.assertIn('"S003-end"', k)
        self.assertTrue(k.index("NOTE A") < k.index("NOTE B"))
        self.assertNotIn("already loaded", pprompts.build_kickoff("S001", "pt1", None, []))

    def test_kickoff_leads_with_the_memory_briefing(self):
        k = pprompts.build_kickoff("S004", "pt1", None, [], "=== BG3 PLAYTHROUGH MEMORY — session S004 ===\n...")
        self.assertTrue(k.startswith("=== BG3 PLAYTHROUGH MEMORY — session S004"))
        self.assertIn("briefing (PLAYTHROUGH.md and HANDOFF.md) is above", k)
        self.assertIn("end_session", k)
        self.assertIn("First call session_start", pprompts.build_kickoff("S004", "pt1", None, []))

    def test_a_rules_only_kickoff_has_no_pace_preflight(self):
        self.assertNotIn("PACE PRE-FLIGHT", pprompts.build_kickoff("S004", "pt1", None, [], pace=""))


class StuckDetection(unittest.TestCase):
    status = {"level": "SCL_Main_A", "region": "Shadow-Cursed Lands", "subregion": "Last Light Inn",
              "party": [{"level": 6, "xp": 100}, {"level": 6, "xp": 90}]}
    journal = [{"title": "Find the Nightsong", "steps": ["a", "b"]}, {"title": "Lift the Curse", "steps": ["x"]}]

    def test_signature(self):
        sig = G.progress_signature(self.status, self.journal)
        self.assertEqual(sig["level"], "SCL_Main_A")
        self.assertEqual(sig["party_levels"], ["6", "6"])
        self.assertEqual(sig["xp"], 190)
        self.assertEqual(sig["journal_steps"], 3)
        self.assertIsNone(G.progress_signature(None, None))
        self.assertEqual(G.progress_signature(None, {"quests": self.journal})["journal_steps"], 3)

    def test_changes_count_as_progress(self):
        a = G.progress_signature(self.status, self.journal)
        self.assertFalse(rpolicy.made_progress(a, G.progress_signature(self.status, self.journal)))
        moved = dict(self.status, subregion="Reithwin Town")
        self.assertTrue(rpolicy.made_progress(a, G.progress_signature(moved, self.journal)))
        more = self.journal + [{"title": "New", "steps": ["s"]}]
        self.assertTrue(rpolicy.made_progress(a, G.progress_signature(self.status, more)))
        xp = dict(self.status, party=[{"level": 6, "xp": 150}, {"level": 6, "xp": 90}])
        self.assertTrue(rpolicy.made_progress(a, G.progress_signature(xp, self.journal)))
        self.assertIsNone(rpolicy.made_progress(None, a))

    def test_session_progress_counts_points_and_far_moves(self):
        a = G.progress_signature(self.status, self.journal)
        # S040: islands descent + Balthazar (32 pts), wipe, reload: same save signature, still progress
        self.assertEqual(rpolicy.session_progress(a, dict(a), 32), (True, "32 story points"))
        self.assertEqual(rpolicy.session_progress(a, dict(a), 2)[0], False)            # S039: 2 explore points
        here = dict(self.status, party=[{"level": 6, "xp": 100, "pos": [0, 0, 0]}, {"level": 6, "xp": 90}])
        near = dict(here, party=[{"level": 6, "xp": 100, "pos": [20, 0, 0]}, {"level": 6, "xp": 90}])
        far = dict(here, party=[{"level": 6, "xp": 100, "pos": [0, -30, 70]}, {"level": 6, "xp": 90}])
        s0 = G.progress_signature(here, self.journal)
        self.assertEqual(s0["pos"], [0.0, 0.0, 0.0])
        self.assertFalse(rpolicy.made_progress(s0, G.progress_signature(near, self.journal)))   # a step isn't progress
        self.assertEqual(rpolicy.session_progress(s0, G.progress_signature(near, self.journal), 0)[0], False)
        ok, why = rpolicy.session_progress(s0, G.progress_signature(far, self.journal), 0)
        self.assertTrue(ok)
        self.assertIn("moved 76 m", why)
        ok, why = rpolicy.session_progress(a, dict(a, subregion="Reithwin Town"), 0)
        self.assertEqual((ok, why), (True, "changed subregion"))
        self.assertEqual(rpolicy.session_progress(None, a, 12), (True, "12 story points"))   # points alone tell it
        self.assertEqual(rpolicy.session_progress(None, a, 3), (None, "unknown"))
        self.assertEqual(rpolicy.session_progress(a, a, None)[0], False)

    def test_boundary_s(self):
        h = [{"kind": "play", "ended": 1000.0}, {"kind": "fix", "ended": 1100.0}]
        self.assertEqual(rpolicy.boundary_s(h, 1045.0), 45.0)          # fix entries are no play boundary
        self.assertIsNone(rpolicy.boundary_s(h, 1000.0 + rpolicy.BOUNDARY_MAX_S + 1))   # the runner was stopped
        self.assertIsNone(rpolicy.boundary_s([], 5.0))

    def test_is_stuck(self):
        h = lambda p, kind="play": {"progress": p, "kind": kind}  # noqa: E731
        self.assertFalse(rpolicy.is_stuck([]))
        self.assertFalse(rpolicy.is_stuck([h(False)]))
        self.assertTrue(rpolicy.is_stuck([h(True), h(False), h(False)]))
        self.assertFalse(rpolicy.is_stuck([h(False), h(True)]))
        self.assertFalse(rpolicy.is_stuck([h(False), h(None)]))          # unknown (crash) isn't "stuck"
        self.assertTrue(rpolicy.is_stuck([h(False), h(False, "fix"), h(False)]))  # fix sessions don't count

    def test_directive_mentions_location(self):
        sig = G.progress_signature(self.status, self.journal)
        d = pprompts.stuck_directive([{"progress": False, "end_sig": sig}] * 2)
        self.assertIn("CHANGE APPROACH", d)
        self.assertIn("Last Light Inn", d)
        self.assertNotIn(sig["level"], d)  # audit C24: the level id stays internal


# ---------------------------------------------------------------- the claude path before runner/clients.py

def legacy_claude_cmd(r, prompt: str, claude_session: str, resume: bool, tag: str = "") -> list[str]:
    """play.py's Runner.claude_cmd as it was before the client interface (commit 1ca87d6), frozen: the claude
    command a session gets must stay byte-identical (ClaudeClient through Runner.play_request)."""
    model, effort = r.model_for(tag)
    cmd = ["claude", "-p", prompt, "--model", model, "--output-format", "stream-json",
           "--verbose", "--strict-mcp-config", "--mcp-config", str(renv.PLAY_DIR / ".mcp.json"),
           "--exclude-dynamic-system-prompt-sections", "--setting-sources", "project,local",
           "--disable-slash-commands", "--tools", "ToolSearch"]
    if effort:
        cmd += ["--effort", effort]
    if r.opt.fallback_model:
        cmd += ["--fallback-model", r.opt.fallback_model]
    if tag != "-fix":
        state = getattr(r, "state", None)
        ac = (rstate.session_overrides(state.dir / "session-config.json") if state else {}).get(
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
        t = rcode.HealthTracker(3)
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
        r = lpt.Runner.__new__(lpt.Runner)
        r.opt = rstate.Options("pt", None, 60, "opus")
        cmd = _cmd(r, "go", "1234", resume=False)
        self.assertEqual(cmd[:3], ["claude", "-p", "go"])
        self.assertIn("--session-id", cmd)
        self.assertIn("stream-json", cmd)
        self.assertIn("--verbose", cmd)
        self.assertEqual(cmd[cmd.index("--model") + 1], "opus")
        self.assertIn("--resume", _cmd(r, "fix", "1234", resume=True))
        self.assertNotIn("--effort", cmd)  # Claude Code's default unless asked
        for flag in renv.CLAUDE_FLAGS:
            self.assertIn(flag, cmd)

    def test_default_client_command_unchanged(self):
        # The whole Runner path (Options default client → play_request → agent_cmd) gives the pre-client command
        cases = [{}, {"effort": "high"}, {"fallback_model": "sonnet"}, {"autocompact": 350_000},
                 {"effort": "max", "fallback_model": "sonnet", "autocompact": 200_000, "fix_model": "haiku"}]
        for kw in cases:
            r = lpt.Runner.__new__(lpt.Runner)
            r.opt = rstate.Options("pt", None, 60, "opus", **kw)
            self.assertEqual(r.opt.client, "claude")
            for prompt, sid, resume, tag in (("go", "1234", False, ""), ("keep playing", "1234", True, ""),
                                             ("fix", "5678", False, "-fix"), ("fix", "5678", True, "-fix")):
                with self.subTest(opt=kw, resume=resume, tag=tag):
                    self.assertEqual(_cmd(r, prompt, sid, resume, tag),
                                     legacy_claude_cmd(r, prompt, sid, resume, tag))

    def test_builtin_tools_disabled(self):
        # The playing agent gets only the bg3 MCP tools (+ ToolSearch to load their schemas): files, memory and the
        # sandbox are the server's (memtools.py); no built-in file, shell, web or agent tool exists in the session.
        r = lpt.Runner.__new__(lpt.Runner)
        r.opt = rstate.Options("pt", None, 60, "opus")
        for cmd in (_cmd(r, "go", "1", resume=False), _cmd(r, "fix", "1", resume=True, tag="-fix")):
            tools = cmd[cmd.index("--tools") + 1]
            self.assertEqual(tools, "ToolSearch")
            self.assertIn("--strict-mcp-config", cmd)
            self.assertIn("--disable-slash-commands", cmd)
        settings = json.loads((renv.PLAY_DIR / ".claude" / "settings.json").read_text())
        self.assertNotIn("hooks", settings)                  # nothing a client must run for correctness
        self.assertEqual(settings["permissions"]["allow"], ["mcp__bg3__*"])
        for tool in ("Bash", "Read", "Edit", "Write", "Glob", "Grep", "WebFetch", "Agent"):
            self.assertIn(tool, settings["permissions"]["deny"])

    def test_keeper_model_default_is_the_play_model(self):
        self.assertEqual(rstate.Options("pt", None, 60, "opus").keeper_model, "same")
        self.assertEqual(rpolicy.keeper_model("same", "opus"), "opus")
        self.assertEqual(rpolicy.keeper_model(None, "fable"), "fable")
        self.assertEqual(rpolicy.keeper_model("sonnet", "opus"), "sonnet")   # an explicit override
        with mock.patch.object(lcli, "cmd_run", lambda a: (a.keeper_model, a.no_memory_keeper)):
            self.assertEqual(P.main(["run", "--playthrough", "x"]), ("same", False))
            self.assertEqual(P.main(["run", "--playthrough", "x", "--no-keeper", "--keeper-model", "haiku"]),
                             ("haiku", True))
            self.assertEqual(P.main(["run", "--playthrough", "x", "--no-memory-keeper"])[1], True)

    def test_model_and_effort(self):
        import tempfile
        from types import SimpleNamespace
        r = lpt.Runner.__new__(lpt.Runner)
        r.opt = rstate.Options("pt", None, 60, "opus", effort="medium", fix_model="sonnet", fallback_model="sonnet")
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
        r = lpt.Runner.__new__(lpt.Runner)
        r.opt = rstate.Options("pt", None, 30, "opus")
        with tempfile.TemporaryDirectory() as d:
            r.state = SimpleNamespace(dir=Path(d))
            self.assertNotIn("--autocompact", _cmd(r, "go", "1", resume=False))  # Claude Code's default
            r.opt = rstate.Options("pt", None, 30, "opus", autocompact=350_000)
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



class ApiAndMcpFailures(unittest.TestCase):
    """RUN-7 / RUN-8 pure parts."""

    def test_classify(self):
        self.assertIsNone(renv.classify_api_failure([], False))
        self.assertEqual(renv.classify_api_failure(["Claude AI usage limit reached|1760000000"], False), "limit")
        self.assertEqual(renv.classify_api_failure(["API Error: 529 Overloaded"], False), "limit")
        self.assertEqual(renv.classify_api_failure(["API Error: 500 Internal server error"], False), "limit")
        self.assertEqual(renv.classify_api_failure([], True), "limit")              # rate_limit_event rejected
        self.assertEqual(renv.classify_api_failure(["Invalid API key · Please run /login"], False), "auth")
        self.assertEqual(renv.classify_api_failure(["API Error: 404 model: claude-x not found"], False), "auth")
        self.assertEqual(renv.classify_api_failure(["Credit balance is too low"], True), "auth")

    def test_backoff(self):
        now = 1000.0
        self.assertEqual(renv.api_backoff_s(now + 600, now, 1), 600 + renv.API_RESET_SLACK_S)   # its reset time
        self.assertEqual(renv.api_backoff_s(now + 10 ** 6, now, 1), renv.API_RESET_MAX_S)
        self.assertEqual([renv.api_backoff_s(None, now, k) for k in (1, 2, 3)], [60, 120, 240])
        self.assertEqual(renv.api_backoff_s(now - 5, now, 99), renv.API_BACKOFF_MAX_S)        # a past reset: doubling

    def test_mcp_problem(self):
        self.assertIsNone(renv.mcp_problem(["bg3:connected"], True))
        self.assertEqual(renv.mcp_problem(["bg3:failed"], True), "the bg3 MCP server is failed")
        self.assertIn("missing", renv.mcp_problem([], True))
        self.assertIsNone(renv.mcp_problem([], False))         # codex/gemini: an absent list proves nothing
        self.assertIsNone(renv.mcp_problem(None, True))
        self.assertEqual(renv.mcp_problem(["bg3:pending"], False), "the bg3 MCP server is pending")


class Profile(unittest.TestCase):
    """CHAR-1 / I4 / I5: profile.json and the phase 0 kickoff block."""

    def test_load(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "profile.json"
            self.assertEqual(rpolicy.load_profile(f), {})                                    # missing: today's flow
            f.write_text("{broken")
            self.assertEqual(rpolicy.load_profile(f), {})
            f.write_text(json.dumps({"character": {"brief": "  "}, "difficulty": "Hard"}))
            self.assertEqual(rpolicy.load_profile(f), {})                                    # no brief, bad difficulty
            f.write_text(json.dumps({"character": {"brief": "a dwarf cleric", "roleplay": "x"}, "rules": {}}))
            self.assertEqual(rpolicy.load_profile(f), {"character": {"brief": "a dwarf cleric", "roleplay": "strict"},
                                                 "rules": {}})                         # strict by default

    def test_update(self):
        p = rpolicy.update_profile({}, "a dwarf cleric")
        self.assertEqual(p, {"character": {"brief": "a dwarf cleric", "roleplay": "strict"}})
        p = rpolicy.update_profile(dict(p, character=dict(p["character"], name="Mira")), roleplay="light",
                             difficulty="Tactician")
        self.assertEqual(p, {"character": {"brief": "a dwarf cleric", "roleplay": "light", "name": "Mira"},
                             "difficulty": "Tactician"})
        self.assertEqual(rpolicy.update_profile({}, roleplay="light"), {})                   # no brief: no character

    def test_note(self):
        prof = {"character": {"brief": "pious\nshort-tempered", "constraints": "no Dark Urge", "roleplay": "strict"}}
        n = rpolicy.character_creation_note(prof, None)
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
        self.assertIn('Name: exactly "Ilse"', rpolicy.character_creation_note(prof, "Ilse"))


class UntilMilestone(unittest.TestCase):
    """RUN-2: run --until <milestone> and its runs.jsonl record."""

    def test_until_reached(self):
        self.assertIsNone(rpolicy.until_reached(None, "nautiloid_escaped"))
        seen = {"regions": ["Nautiloid"], "quests": {"Escape the Nautiloid": {"done": False}}}
        self.assertIsNone(rpolicy.until_reached(seen, "nautiloid_escaped"))
        self.assertEqual(rpolicy.until_reached(seen, "nautiloid")["label"], "Aboard the Nautiloid")
        seen["quests"]["Escape the Nautiloid"]["done"] = True
        self.assertEqual(rpolicy.until_reached(seen, "nautiloid_escaped"),
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
            with mock.patch.object(rpolicy, "read_actions_since", lambda t: [{"tool": "load_game"}, {"tool": "look"}]):
                rec = lser.run_record(d, 100, 100 + 3600, {"id": "crash_site", "label": "Reached the surface",
                                                        "session": "S004", "t": 3000}, "pt", hist)
        self.assertEqual((rec["milestone"], rec["reached_in"], rec["wall_min"]), ("crash_site", "S004", 60.0))
        self.assertEqual((rec["sessions"], rec["play_min"]), (1, 20))
        self.assertEqual((rec["cost_usd"], rec["turns"], rec["tokens"]["output"]), (2.1, 6, 16))
        self.assertEqual((rec["crashes"], rec["agent_hangs"], rec["recoveries"], rec["reloads"]), (1, 1, 1, 1))


class Latency(unittest.TestCase):
    """Runner metrics: stream timing from a fake stream-json transcript + the end-of-session summary."""

    def test_stream_timing_and_summary(self):
        import json
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            base = d / "S001"
            timing = rpolicy.latency.StreamTiming(Path(f"{base}.turns.jsonl"))
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

            s = lpt.Runner.write_latency(FakeRunner(), "S001", base, t0, t0 + 60)
            self.assertEqual(s["think_s"]["median"], 1.5)
            self.assertEqual(s["source"], "stream")       # no action records from the bridge: stream fallback
            self.assertEqual(s["idle_s"]["all"]["median"], 1.5)
            self.assertTrue(s["targets"]["median_idle_explore_s"]["pass"])
            self.assertTrue(s["targets"]["bridge_rt_median_ms"]["pass"])
            self.assertTrue(Path(f"{base}.latency.json").exists())
            hist = (d / "latency_history.jsonl").read_text().splitlines()
            self.assertEqual(json.loads(hist[0])["session"], "S001")

    def test_metrics_path_in_env(self):
        src = Path(ragent.__file__).read_text()
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

            orig_read, orig_poll = rchecks.story.read_game, rchecks.story.POLL_S
            rchecks.story.read_game, rchecks.story.POLL_S = fake_read, 0.001
            try:
                log = []
                human = type("H", (), {"write": lambda self, x: log.append(x)})()
                lpt.Runner.progress_loop(FakeRunner(), "S004", base, 0.0, stop, human)
            finally:
                rchecks.story.read_game, rchecks.story.POLL_S = orig_read, orig_poll
            self.assertTrue(any("+15 region: Wilderness" in x for x in log), log)
            self.assertTrue(any("+2 quest_step: Escape: b" in x for x in log), log)
            s = lpt.Runner.write_progress(FakeRunner(), "S004", base, 0.0, 1800.0)
            self.assertEqual(s["points"], 15 + 2 + 3)  # region, quest_step, area (Hold in a new region)
            self.assertEqual(s["per_hour"], 40.0)
            self.assertEqual(rpolicy.story_points(Path(f"{base}.progress.json")), 20)
            row = json.loads((d / "progress_history.jsonl").read_text())
            self.assertEqual(row["session"], "S004")
            self.assertTrue((d / "progress_state.json").exists())

    def test_wired_into_run_claude(self):
        src = Path(ragent.__file__).read_text()
        self.assertIn('progress=f"{base}.progress.jsonl"', src)
        self.assertIn('self.write_progress(f"{sid}{tag}", base, start, end)', src)


class AvatarName(unittest.TestCase):
    def test_reads_the_avatar_not_the_selection(self):
        seen = []
        saved = G.lua_eval
        try:
            G.lua_eval = lambda code, context="client", timeout=0: seen.append((code, context)) or " Claude \n"
            self.assertEqual(G.host_character_name(), "Claude")
        finally:
            G.lua_eval = saved
        code, ctx = seen[0]
        self.assertEqual(ctx, "server")
        self.assertIn("DB_Avatars", code)
        self.assertLess(code.index("DB_Avatars"), code.index("GetHostCharacter"))  # selection only as fallback

    def test_unreadable_is_none(self):
        saved = G.lua_eval
        try:
            G.lua_eval = lambda *a, **k: (_ for _ in ()).throw(OSError("no game"))
            self.assertIsNone(G.host_character_name())
        finally:
            G.lua_eval = saved


class WaitWatchdog(unittest.TestCase):
    def test_reports_after_90s_then_every_30s(self):
        out = []
        w = gamectl.WaitReporter("game state Running", 600, start=0.0, logf=out.append)
        fired = [t for t in range(0, 200, 5) if w.tick(float(t))]
        self.assertEqual(fired, [90, 120, 150, 180])
        self.assertIn("game state Running", out[0])
        self.assertIn("90 s so far, 510 s left", out[0])

    def test_short_wait_silent(self):
        out = []
        w = gamectl.WaitReporter("x", 60, start=0.0, logf=out.append)
        self.assertFalse(any(w.tick(float(t)) for t in range(0, 61)))


class InflightStuck(unittest.TestCase):
    """A hung game's in-flight background search is marked stuck by the runner (the mod's Tick froze with it)."""

    def test_marks_fresh_inflight_once(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "nav_guard.json"
            f.write_text(json.dumps({"strikes": [], "inflight": {"level": "SCL_Main_A", "op": "ways warm"}}))
            self.assertEqual(rpolicy.mark_inflight_stuck(f), "SCL_Main_A")
            self.assertTrue(json.loads(f.read_text())["inflight"]["stuck"])
            self.assertIsNone(rpolicy.mark_inflight_stuck(f))          # already stuck

    def test_per_playthrough_file_and_per_op_markers(self):  # NAV-3 + NAV-7
        with tempfile.TemporaryDirectory() as d:
            old = Path(d) / "nav_guard.json"
            old.write_text(json.dumps({"inflight": {"level": "WLD_Main_A", "op": "ways warm"}}))
            os.utime(old, (1, 1))
            f = Path(d) / "nav_guard_aaaa.json"
            f.write_text(json.dumps({"inflights": {"jump": {"level": "SCL_Main_A", "op": "jump"},
                                                   "ways warm": {"level": "SCL_Main_A", "op": "ways warm"}}}))
            self.assertEqual(rpolicy.mark_inflight_stuck(Path(d)), "SCL_Main_A")    # the newest file
            got = json.loads(f.read_text())["inflights"]
            self.assertTrue(got["ways warm"]["stuck"])
            self.assertNotIn("stuck", got["jump"])                             # a jump marker counts anyway
            self.assertNotIn("stuck", json.loads(old.read_text())["inflight"])
            self.assertIsNone(rpolicy.mark_inflight_stuck(Path(d) / "none"))

    def test_nothing_in_flight_or_no_file(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "nav_guard.json"
            self.assertIsNone(rpolicy.mark_inflight_stuck(f))
            f.write_text(json.dumps({"strikes": {}}))
            self.assertIsNone(rpolicy.mark_inflight_stuck(f))
            self.assertNotIn("inflight", json.loads(f.read_text()))


if __name__ == "__main__":
    unittest.main(verbosity=1)

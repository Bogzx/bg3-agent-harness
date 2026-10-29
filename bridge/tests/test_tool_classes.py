"""Offline tests: bg3ai.tool_classes (the read tools, what a failed result looks like) and the bg3 server sending a
failed text result with is_error set, so a client's events mark it (the evidence video draws it as a failure).

Run: cd bridge && uv run python -m unittest tests/test_tool_classes.py
"""

from __future__ import annotations

import ast
import asyncio
import re
import unittest
from pathlib import Path

from bg3ai import tool_classes
from bg3ai.game import JobResult
from bg3ai.tools import core, plumbing
from test_turn_macros import Base  # noqa: F401  (a test runtime for the server)

SRC = Path(tool_classes.__file__).parent
_WORD = re.compile(r"\b(?:FAILED|NOT READY|REFUSED|CHAIN STOPPED)\b")
# Calls whose string arguments test or edit a result, not say one.
_CHECKS = {"startswith", "endswith", "replace", "split", "compile", "match", "search", "fullmatch", "sub", "findall"}
# Strings in the tool code that name a failure but are no call's outcome (file, text as rendered by _render).
NOT_OUTCOMES = {
    ("dialogue.py", " → FAILED"),  # the roll failed; roll_check worked
    ("saver.py", "⚠ SAVE FAILED 'x': x; call save_game again"),  # a background save's note, on any later result
    ("saver.py", "⚠ SAVE FAILED 'x': no save folder appeared within xs (x). On disk: x. Call save_game again"),
    ("saver.py", "⚠ SAVE FAILED 'x': the game could not save for x min (x); call save_game again"),
    ("inventory.py", "\nFAILED: x"),  # manage_inventory / equip_best: the items that failed, under "partly done"
    ("progression.py", "swap back FAILED: x"),  # level_up's line after the levels it did
}


def _render(node: ast.AST) -> str | None:
    """A string expression as the text it gives: a formatted value reads x, a non-string left operand of + is a note
    line the tool put before the literal."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(str(v.value) if isinstance(v, ast.Constant) else "x" for v in node.values)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _render(node.left), _render(node.right)
        if left is None and right is None:
            return None
        return (left if left is not None else "a note\n") + (right if right is not None else "x")
    return None


def tool_failure_strings() -> list[tuple[str, int, str]]:
    """Every string the bridge's code builds that names a failure (FAILED, NOT READY, REFUSED, CHAIN STOPPED):
    docstrings, the strings a check compares against and tool_classes itself left out."""
    found = []
    for path in sorted(SRC.rglob("*.py")):
        if path.name == "tool_classes.py":
            continue
        tree = ast.parse(path.read_text())
        parent = {c: n for n in ast.walk(tree) for c in ast.iter_child_nodes(n)}
        for node in ast.walk(tree):
            up = parent.get(node)
            if isinstance(up, ast.JoinedStr) or (isinstance(up, ast.BinOp) and _render(up) is not None):
                continue  # part of a bigger string, rendered there
            text = _render(node)
            if text is None or not _WORD.search(text) or isinstance(up, (ast.Expr, ast.Compare)):
                continue
            call = up if isinstance(up, ast.Call) else parent.get(up) if isinstance(up, ast.Tuple) else None
            if isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute) and call.func.attr in _CHECKS:
                continue
            found.append((path.name, getattr(node, "lineno", 0), text))
    return found


def _subject(text: str) -> str:
    """The tool a failure line names ("short_rest: FAILED" -> short_rest), as the server passes the called tool."""
    m = re.search(rf"^(?:1\. )?([a-z_]+)[^:\n]*: {_WORD.pattern}", text, re.M)
    return m.group(1) if m else ""


class FailedResults(unittest.TestCase):
    def test_every_failure_the_tool_code_says_is_a_failed_result(self):
        found = tool_failure_strings()
        self.assertGreater(len(found), 100)  # the scan reads the tool code
        seen = set()
        for name, line, text in found:
            if (name, text) in NOT_OUTCOMES:
                seen.add((name, text))
                continue
            with self.subTest(f"{name}:{line}"):
                self.assertTrue(tool_classes.failed(text, _subject(text)), f"{name}:{line} {text!r}")
        self.assertEqual(seen, NOT_OUTCOMES)  # each exception still names a string in the code

    def test_a_job_that_failed_is_a_failed_result_for_every_tool(self):
        tools = [t.name for t in core.mcp._tool_manager.list_tools()]
        self.assertIn("jump_to", tools)
        for name in tools:
            failed = plumbing._job_text(name, JobResult("failed", None, "it is not this character's turn"))
            self.assertTrue(tool_classes.failed(failed, name), failed)
            self.assertTrue(tool_classes.failed("1. " + failed, "do_actions"), failed)
            for status in ("done", "interrupted", "timeout", "cancelled", "stopped"):
                text = plumbing._job_text(name, JobResult(status, None, "combat started"))
                self.assertFalse(tool_classes.failed(text, name), text)

    def test_the_failure_forms_live_runs_sent(self):
        for text, tool in (
            ("FAILED: no objective o1 now (done or gone); current: o2 Helm", "travel_to"),
            ("NOT READY: loading", "look"),
            ("REFUSED: past the deadline", "move_to"),
            ("CHAIN STOPPED after 0 of 4: Lae'zel: not played", "combat_turn"),
            ("jump_to: FAILED — that spot is not in view and the party hasn't walked there", "jump_to"),
            ("use_ability: FAILED — it is not this character's turn", "use_ability"),
            ("interact: FAILED — not available during combat through this action: click_on e137", "interact"),
            ("travel_to (46.8,17.6,-360): REFUSED: the goal is 15m above you", "travel_to"),
            ("1. FAILED: no walkable ground toward SE within 4m (0 spots checked)", "do_actions"),
            ("1. move_to: FAILED — beyond movement (path 4.5 m, 0.8 m left)\nSTOPPED: step 1 failed", "do_actions"),
            (
                "(closed the trade window first)\nshort_rest: FAILED — not pressed: a conversation is on screen",
                "short_rest",
            ),
            (
                "BRIEFING\nmemory: 3 notes" + tool_classes.RESULT_MARK + "FAILED: no marker m9 (yours: m1)",
                "delete_marker",
            ),
        ):
            self.assertTrue(tool_classes.failed(text, tool), text)

    def test_what_is_no_failed_call(self):
        for text, tool in (
            ("done", "move_to"),
            ("", "move_to"),
            ("TURN e384 Spiritual Weapon\nCHAIN STOPPED after 1 of 2: x", "combat_turn"),
            ("TURN e4 Tav (round 9)\naction: Target_MainHandAttack: FAILED — no line of sight", "combat_turn"),
            ("queue e1 Tav: STOPPED: step 1 travel_to o1: no walkable ground leads further toward it", "wait_for"),
            (
                "travel_to objective Helm: started (17.2m)\nqueue e1 Tav: STOPPED: the engine tried to teleport",
                "travel_to",
            ),
            ("1. move_to: done — dist=0\n2. move_to: FAILED — target is not visible\nSTOPPED: step 2", "do_actions"),
            ("roll: Persuasion (Charisma Check) DC 16: d20 4 + 7 = 11 → FAILED", "roll_check"),
            ("level_up: partly done (1 of 2 levels)\nswap back FAILED: x", "level_up"),
            ("get_status: party ...\n⚠ SAVE FAILED 'S010': x; call save_game again", "get_status"),
            ("look: e1 Goblin\nmove_to: FAILED — an earlier call's line, not this one's", "look"),
            ("BRIEFING: FAILED: last session's notes" + tool_classes.RESULT_MARK + "memory: 3 notes", "memory_list"),
        ):
            self.assertFalse(tool_classes.failed(text, tool), text)

    def test_the_read_tools_are_the_reporting_layers(self):
        from bg3ai.tools import reporting

        self.assertIs(reporting._READ_TOOLS, tool_classes.READ_TOOLS)
        self.assertIn("get_status", tool_classes.READ_TOOLS)
        self.assertNotIn("combat_turn", tool_classes.READ_TOOLS)

    def test_the_server_sends_a_failed_result_as_an_error(self):
        srv = core.Server("t")

        @srv.tool()
        def act(ok: bool) -> str:
            return "done" if ok else "act: FAILED — no path"

        @srv.tool()
        def rest() -> str:
            return "(closed the trade window first)\nrest: FAILED — not pressed"

        good = asyncio.run(srv.call_tool("act", {"ok": True}))
        bad = asyncio.run(srv.call_tool("act", {"ok": False}))
        noted = asyncio.run(srv.call_tool("rest", {}))
        self.assertFalse(good.is_error)
        self.assertTrue(bad.is_error)
        self.assertTrue(noted.is_error)  # the server passes the called tool's name
        self.assertEqual(bad.content[0].text, "act: FAILED — no path")
        self.assertIsInstance(core.mcp, core.Server)


if __name__ == "__main__":
    unittest.main()

"""Offline tests: BG3AI_TOOLS / BG3AI_TOOLS_OFF (capabilities.py) remove tools from the server at startup.
Each env builds the server in its own subprocess (the tool set is fixed at import).

Run: cd bridge && uv run python -m unittest tests/test_capabilities.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from bg3ai import capabilities

BRIDGE = Path(__file__).resolve().parents[1]
FULL = 94

# Prints the tool names and, for a note= call, what do_actions/queue_actions/travel_to answer. Port 1: never a
# running game.
PROBE = """
import json, sys
from bg3ai import server
out = {"tools": sorted(t.name for t in server.mcp._tool_manager.list_tools()),
       "batchable": sorted(server._BATCHABLE)}
if "--note" in sys.argv:
    out["note"] = [server.do_actions("[]", note="x"), server.queue_actions("[]", note="x"),
                   server.travel_to(note="x")]
print(json.dumps(out))
"""


def build(env: dict[str, str], *args: str) -> subprocess.CompletedProcess:
    with tempfile.TemporaryDirectory(prefix="bg3caps-") as rt:
        full = {k: v for k, v in os.environ.items() if not k.startswith("BG3AI_TOOLS")}
        full.update({"BG3AI_RUNTIME": rt, "BG3AI_MEMORY_DIR": rt, "BG3LE_DEBUG_PORT": "1"}, **env)
        return subprocess.run([sys.executable, "-c", PROBE, *args], cwd=BRIDGE, env=full, capture_output=True, check=False,
                              text=True, timeout=120)


def tools(env: dict[str, str], *args: str) -> dict:
    r = build(env, *args)
    if r.returncode != 0:
        raise AssertionError(r.stderr[-2000:])
    return json.loads(r.stdout.strip().splitlines()[-1])


class Server(unittest.TestCase):
    def test_default_is_every_tool(self):
        names = tools({})["tools"]
        self.assertEqual(len(names), FULL)
        self.assertEqual(tools({"BG3AI_TOOLS": ""})["tools"], names)

    def test_game_only(self):
        out = tools({"BG3AI_TOOLS": "game"})
        off = {t for g in capabilities.GROUPS.values() for t in g}
        self.assertFalse(off & set(out["tools"]))
        self.assertIn("move_to", out["tools"])
        self.assertEqual(len(out["tools"]), FULL - len(off))
        self.assertNotIn("press_key", out["batchable"])  # raw input can't come back as a do_actions step

    def test_groups_and_names(self):
        names = set(tools({"BG3AI_TOOLS": "game, wiki,list_saves"})["tools"])
        self.assertTrue({"wiki_search", "wiki_page", "list_saves", "move_to"} <= names)
        self.assertFalse({"save_game", "load_game", "note", "screenshot"} & names)

    def test_off_on_top(self):
        names = set(tools({"BG3AI_TOOLS_OFF": "reload,ui_action"})["tools"])
        self.assertEqual(len(names), FULL - 2)
        self.assertFalse({"load_game", "ui_action"} & names)

    def test_unknown_name_fails_loudly(self):
        for env in ({"BG3AI_TOOLS": "game,wikis"}, {"BG3AI_TOOLS_OFF": "nope"}):
            r = build(env)
            self.assertNotEqual(r.returncode, 0, env)
            self.assertIn("unknown tool group or tool name", r.stderr)

    def test_note_refused_with_memory_off(self):
        out = tools({"BG3AI_TOOLS": "game"}, "--note")
        for text in out["note"]:
            self.assertIn("note= is not available", text)

    def test_note_kept_with_memory_on(self):
        out = tools({}, "--note")
        for text in out["note"]:
            self.assertNotIn("note= is not available", text)


class Disabled(unittest.TestCase):
    ALL = {t for g in capabilities.GROUPS.values() for t in g} | {"move_to", "look"}

    def test_unset(self):
        self.assertEqual(capabilities.disabled(self.ALL, {}), set())

    def test_game_always_on(self):
        self.assertEqual(capabilities.disabled(self.ALL, {"BG3AI_TOOLS": "wiki"}),
                         self.ALL - {"move_to", "look", "wiki_search", "wiki_page"})

    def test_off_removes_game_tools(self):
        self.assertEqual(capabilities.disabled(self.ALL, {"BG3AI_TOOLS_OFF": "game"}), {"move_to", "look"})

    def test_renamed_grouped_tool_fails(self):
        with self.assertRaisesRegex(ValueError, "not registered: zoom"):
            capabilities.disabled(self.ALL - {"zoom"}, {})


if __name__ == "__main__":
    unittest.main()

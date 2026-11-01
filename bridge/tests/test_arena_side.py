"""Offline tests: a server playing one team of a two-team match (BG3AI_SIDE) sends its side with every op, and a server
without it sends exactly what it sent before; the arena tool set (capabilities.ARENA_TOOLS_OFF) serves the fight tools.

Run: cd bridge && uv run python -m unittest tests/test_arena_side.py
"""

from __future__ import annotations

import gzip
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

from test_capabilities import BRIDGE, build, tools  # (sets BG3AI_RUNTIME before importing the server)

# isort: split
from bg3ai import capabilities, rpc_record, settings
from bg3ai.game import RPC_EXPR, Game

FIXTURES = Path(__file__).resolve().parents[2] / "packages/harness/tests/fixtures/rpc"
GOLDEN = Path(__file__).resolve().parent / "golden" / "tools_list.json"


class Capture(Game):
    """A Game whose evals are kept instead of sent; each answers like the mod would for `replies` ops."""

    def __init__(self, side: str | None = None):
        super().__init__(port=1, side=side)
        self.sent: list[str] = []
        self._ev_synced = True  # no events.since probe before the first call

    def eval(self, code: str, context: str = "client", timeout: float = 12.0) -> str:
        self.sent.append(code)
        decoded = rpc_record.decode(code)
        assert decoded is not None, code
        calls, multi = decoded
        parts = [json.dumps({"ok": True, "result": c["op"]}) for c in calls]
        return "[" + ",".join(parts) + "]" if multi else parts[0]


def old_single(op: str, args: dict) -> str:
    """The eval Game.rpc sent before sides existed (game.py: json.dumps({op, args}) in a long bracket)."""
    payload = json.dumps({"op": op, "args": args or {}}, separators=(",", ":"))
    eq = "="
    while f"]{eq}]" in payload:
        eq += "="
    return RPC_EXPR.format(eq=eq, payload=payload)


def old_multi(calls: list[tuple[str, dict]]) -> str:
    parts = []
    for op, args in calls:
        payload = json.dumps({"op": op, "args": args or {}}, separators=(",", ":"))
        eq = "="
        while f"]{eq}]" in payload:
            eq += "="
        parts.append(f"R([{eq}[{payload}]{eq}])")
    return "local R=Mods.BG3AI.BG3AI.rpc return '['.." + "..','..".join(parts) + "..']'"


def recorded_calls() -> list[tuple[str, dict]]:
    """Every (op, args) in the recorded RPC fixtures."""
    out = []
    for f in sorted(FIXTURES.glob("*.jsonl*")):
        opener = gzip.open if f.suffix == ".gz" else open
        with opener(f, "rt", encoding="utf-8") as fh:
            for line in fh:
                for c in json.loads(line).get("calls") or []:
                    out.append((c["op"], c.get("args") or {}))
    return out


class Side(unittest.TestCase):
    def test_settings_side(self):
        self.assertIsNone(settings.side({}))
        self.assertIsNone(settings.side({"BG3AI_SIDE": ""}))
        self.assertEqual([settings.side({"BG3AI_SIDE": s}) for s in ("A", "B")], ["A", "B"])
        for bad in ("C", "a", " A", "AB"):
            with self.assertRaises(ValueError, msg=bad):
                settings.side({"BG3AI_SIDE": bad})
        self.assertEqual(settings.from_env({"BG3AI_SIDE": "B"}, home=Path("/h")).side, "B")
        self.assertIsNone(settings.from_env({}, home=Path("/h")).side)

    def test_no_side_sends_the_recorded_evals_unchanged(self):
        calls = recorded_calls()
        self.assertGreater(len(calls), 100)
        g = Capture()
        for op, args in calls:
            g.rpc(op, args)
        self.assertEqual(g.sent, [old_single(op, args) for op, args in calls])
        g.sent.clear()
        batch = calls[:40]
        g.multi(batch)
        self.assertEqual(g.sent, [old_multi(batch)])

    def test_side_on_every_op(self):
        calls = recorded_calls()[:60]
        for side in ("A", "B"):
            g = Capture(side)
            for op, args in calls:
                g.rpc(op, args)
            g.multi(calls[:10])
            got = [c for code in g.sent for c in rpc_record.decode(code)[0]]
            want = [{"op": op, "args": args or {}, "side": side} for op, args in calls + calls[:10]]
            self.assertEqual(got, want)

    def test_decode_keeps_a_side_only_when_sent(self):
        plain = rpc_record.decode(old_single("state.status", {}))
        self.assertEqual(plain, ([{"op": "state.status", "args": {}}], False))
        code = 'return Mods.BG3AI.BG3AI.rpc([=[{"op":"state.combat","args":{},"side":"A"}]=])'
        self.assertEqual(rpc_record.decode(code), ([{"op": "state.combat", "args": {}, "side": "A"}], False))


PROBE_SIDE = """
import json
from bg3ai.tools import core, queueing
print(json.dumps({"game": core.game.side, "watch": queueing._watch_conn().side}))
"""


class Server(unittest.TestCase):
    def test_every_game_of_the_server_has_the_side(self):
        for side, want in (("A", "A"), ("B", "B"), ("", None)):
            env = {**os.environ, "BG3AI_SIDE": side, "BG3LE_DEBUG_PORT": "1"}
            r = subprocess.run(
                [sys.executable, "-c", PROBE_SIDE],
                cwd=BRIDGE,
                env=env,
                capture_output=True,
                text=True,
                check=False,
                timeout=120,
            )
            self.assertEqual(r.returncode, 0, r.stderr[-2000:])
            self.assertEqual(json.loads(r.stdout.strip().splitlines()[-1]), {"game": want, "watch": want})

    def test_bad_side_fails_startup(self):
        r = build({"BG3AI_SIDE": "C"})
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("BG3AI_SIDE", r.stderr)

    def test_arena_tool_set(self):
        names = {t["name"] for t in json.loads(GOLDEN.read_text(encoding="utf-8"))}
        off = capabilities.disabled(names, {"BG3AI_TOOLS_OFF": capabilities.ARENA_TOOLS_OFF})
        fight = {
            "get_status",
            "look",
            "get_party",
            "get_character",
            "get_actions",
            "get_nearby",
            "look_at",
            "get_combat_state",
            "path_preview",
            "ability_preview",
            "move_to",
            "jump_to",
            "use_ability",
            "react",
            "set_reactions",
            "end_turn",
            "combat_turn",
            "do_actions",
            "queue_actions",
            "cancel_queue",
            "wait_for",
            "wait",
            "manage_inventory",
            "set_party",
            "get_dialogue",
            "choose_dialogue",
            "dialogue_advance",
            "roll_check",
            "confirm",
            "dismiss_popups",
            "flee",
            "session_clock",
        }
        self.assertFalse(off & fight, off & fight)
        for group in ("memory", "reload", "saves", "raw_input"):
            self.assertTrue(set(capabilities.GROUPS[group]) <= off, group)
        self.assertTrue(
            {
                "travel_to",
                "fast_travel",
                "camp",
                "long_rest",
                "short_rest",
                "trade",
                "talk_to",
                "level_up",
                "get_journal",
            }
            <= off
        )
        served = tools({"BG3AI_TOOLS_OFF": capabilities.ARENA_TOOLS_OFF})
        self.assertEqual(set(served["tools"]), names - off)
        self.assertFalse(off & set(served["batchable"]))


if __name__ == "__main__":
    unittest.main()

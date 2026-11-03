"""Offline tests: a server playing one team of a two-team match (BG3AI_SIDE) sends its side with every op, and a server
without it sends exactly what it sent before; the arena tool list (capabilities.LISTS arena-v1) serves the fight tools;
a reaction prompt of the other side's character reads as waiting (sideview.py).

Run: cd bridge && uv run python -m unittest tests/test_arena_side.py
"""

from __future__ import annotations

import gzip
import json
import os
import re
import subprocess
import sys
import unittest
from pathlib import Path

from test_capabilities import BRIDGE, build, tools  # (sets BG3AI_RUNTIME before importing the server)

# isort: split
from bg3ai import capabilities, rpc_record, settings, sideview
from bg3ai.game import RPC_EXPR, Game, JobResult

P1, P2 = (f"00000000-0000-0000-0000-{n:012d}" for n in (1, 2))

FIXTURES = Path(__file__).resolve().parents[2] / "packages/harness/tests/fixtures/rpc"
GOLDEN = Path(__file__).resolve().parent / "golden" / "tools_list.json"
ARENA_MD = Path(__file__).resolve().parents[2] / "packages/harness/prompts/rules/arena.md"


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
            got = [c for code in g.sent for c in (rpc_record.decode(code) or ([], False))[0]]
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
        off = capabilities.disabled(names, {"BG3AI_TOOLS_OFF": "arena-v1"})
        groups = ("memory", "wiki", "reload", "saves", "screenshots", "raw_input")
        by_name = set(SPEC_ARENA_V1.split())
        self.assertEqual(off, {t for g in groups for t in capabilities.GROUPS[g]} | by_name)
        self.assertEqual(set(capabilities.LISTS["arena-v1"]), set(groups) | by_name)
        self.assertIn("ui_action", off)  # a named UI command on the shared client has no side check
        kept = {
            "interact",
            "set_party",
            "get_frontier",
            "get_layout",
            "end_combat_report",
            "wait",
            "session_clock",
            "confirm",
            "dismiss_popups",
            "react",
            "set_reactions",
            "use_ability",
            "end_turn",
            "combat_turn",
            "move_to",
            "wait_for",
            "get_combat_state",
        }
        self.assertFalse(off & kept, off & kept)
        # a list name mixes with groups and names, and only BG3AI_TOOLS_OFF takes one
        self.assertEqual(capabilities.disabled(names, {"BG3AI_TOOLS_OFF": "memory, arena-v1,flee"}), off | {"flee"})
        with self.assertRaises(ValueError):
            capabilities.disabled(names, {"BG3AI_TOOLS": "arena-v1"})
        served = tools({"BG3AI_TOOLS_OFF": "arena-v1"})
        self.assertEqual(set(served["tools"]), names - off)
        self.assertFalse(off & set(served["batchable"]))

    def test_a_sides_server_serves_the_match_rules(self):
        side = tools({"BG3AI_SIDE": "A", "BG3AI_TOOLS_OFF": "arena-v1"})["instructions"]
        self.assertIn("## Two-team match", side)
        self.assertTrue(side.rstrip().endswith(ARENA_MD.read_text(encoding="utf-8").strip()))
        self.assertNotIn("## Two-team match", tools({"BG3AI_TOOLS_OFF": "arena-v1"})["instructions"])
        self.assertNotIn("## Two-team match", tools({})["instructions"])

    def test_the_match_rules_name_every_tool_the_list_removes(self):
        text = ARENA_MD.read_text(encoding="utf-8")
        named = {n for n in capabilities.LISTS["arena-v1"] if n not in capabilities.GROUPS}
        self.assertEqual({n for n in named if f"`{n}`" not in text}, set())
        flat = " ".join(text.split())
        for words in ("memory", "wiki", "saves and reloads", "screenshots", "raw input"):
            self.assertIn(words, flat)

    def test_a_list_naming_an_unregistered_tool_fails(self):
        names = {t["name"] for t in json.loads(GOLDEN.read_text(encoding="utf-8"))}
        saved = capabilities.LISTS["arena-v1"]
        capabilities.LISTS["arena-v1"] = (*saved, "renamed_tool")
        try:
            with self.assertRaisesRegex(ValueError, "renamed_tool"):
                capabilities.disabled(names, {"BG3AI_TOOLS_OFF": "arena-v1"})
        finally:
            capabilities.LISTS["arena-v1"] = saved


# The by-name part of the arena tools-off list in the spec (§3.5), verbatim.
SPEC_ARENA_V1 = """ui_action fast_travel camp swap_party long_rest short_rest rest_if_needed travel_to talk_to
choose_dialogue dialogue_advance get_dialogue roll_check trade pickpocket lockpick disarm loot loot_corpse loot_area
pick_up feed_gale insert_item read_all place_marker delete_marker new_game character_creation_options create_character
get_level_up_options level_up prepare_spells get_journal get_map"""

SRC = Path(__file__).resolve().parents[1] / "src" / "bg3ai"
# Request builders besides Game.envelope: progress.combined_code serves the harness's own readers, which must send no
# side (they see both teams); delta.multi_rpc's fallback runs only for objects without Game.multi (test doubles).
OTHER_BUILDERS = {"progress.py": 1, "delta.py": 1}


class Builders(unittest.TestCase):
    def test_the_envelope_is_built_in_one_place(self):
        found: dict[str, int] = {}
        for path in sorted(SRC.rglob("*.py")):
            n = len(re.findall(r"""json\.dumps\(\s*\{\s*["']op["']|\{\s*["']op["']\s*:\s*op\b""", path.read_text()))
            if n:
                found[path.relative_to(SRC).as_posix()] = n
        self.assertEqual(found, {"game.py": 1, **OTHER_BUILDERS})


class HudSelected(unittest.TestCase):
    """ui.selected with a side: no uuid = the other side's character holds the HUD (not an unreadable client)."""

    def game(self, side, replies):
        g = Game(port=1, side=side)
        g.SELECT_FOLLOW_S = 0.0
        jobs: list[dict] = []
        g.rpc = lambda op, args=None, context="server", **k: replies.pop(0) if op == "ui.selected" else {}
        g.try_rpc = lambda op, args=None, context="server", default=None: {"party": [{"uuid": P1}, {"uuid": P2}]}
        g._run_job = lambda op, args, *a, **k: jobs.append(args) or JobResult("done", {})
        return g

    def test_no_uuid(self):
        self.assertEqual(self.game("A", [{}]).hud_selected(), sideview.HUD_OTHER_SIDE)
        self.assertIsNone(self.game(None, [{}]).hud_selected())
        self.assertEqual(self.game("A", [{"uuid": P1}]).hud_selected(), P1)

    def test_a_select_the_hud_doesnt_follow_says_so(self):
        r = self.game("A", [{}] * 6)._select({"char": P1}, 1.0, 0.0)
        self.assertEqual(r.status, "failed")
        self.assertIn("the HUD kept the other side's character selected", r.error)
        self.assertEqual(r.result["holder"], sideview.HUD_OTHER_SIDE)
        # without a side an unreadable client counts as followed, as before
        self.assertEqual(self.game(None, [{}] * 6)._select({"char": P1}, 1.0, 0.0).status, "done")


class ReadCost(unittest.TestCase):
    """A ui.state read with a side is one RPC: the mod marks the other side's prompt, and the team's names are read
    once per match (again only when a prompt names someone they miss)."""

    def game(self, ui: dict) -> tuple[Game, list[str]]:
        sent: list[str] = []

        class G(Game):
            def eval(self, code, context="client", timeout=12.0):
                calls, _ = rpc_record.decode(code) or ([], False)
                sent.extend(c["op"] for c in calls)
                party = [{"uuid": P1, "name": "Tav"}, {"uuid": P2, "name": "Shadowheart"}]
                res = ui if calls[0]["op"] == "ui.state" else {"party": party}
                return json.dumps({"ok": True, "result": res})

        g = G(port=1, side="A")
        g._ev_synced = True
        return g, sent

    def prompt(self, *who: str) -> dict:
        return {"screen": "reaction", "reaction": {"options": [{"i": 1, "who": w} for w in who]}}

    def test_one_rpc_per_read(self):
        for ui in ({"screen": "game"}, {"screen": "reaction", "reaction": {"other_side": True}}):
            g, sent = self.game(ui)
            for _ in range(3):
                g.rpc("ui.state", context="server")
            self.assertEqual(sent, ["ui.state"] * 3, ui)

    def test_names_read_once_per_match(self):
        g, sent = self.game(self.prompt("Tav"))
        for _ in range(3):
            self.assertEqual(g.rpc("ui.state", context="server")["screen"], "reaction")
        self.assertEqual(sent, ["ui.state", "state.status", "ui.state", "ui.state"])
        g, sent = self.game(self.prompt("Lae'zel"))  # a name the cache misses (an unmarked prompt): read again
        g._team = {"Tav", "Shadowheart"}
        self.assertEqual(g.rpc("ui.state", context="server")["screen"], "game")
        self.assertEqual(sent, ["ui.state", "state.status"])


class ReactionView(unittest.TestCase):
    """A reaction prompt of the other side's character is waiting for this side, never a prompt to answer."""

    TEAM = ("Tav", "Shadowheart")

    def prompt(self, *who: str) -> dict:
        rows = [{"i": i + 1, "who": w, "name": "Shield", "level": 1} for i, w in enumerate(who)]
        return {
            "screen": "reaction",
            "page": "ReactionDecisionPopup",
            "blocking": True,
            "region": "Act 1",
            "reaction": {"prompt": " | ".join(who) + " | Shield", "options": rows},
            "ms": 2,
        }

    def view(self, ui):
        return sideview.ui_view(ui, lambda fresh: self.TEAM)

    def test_the_other_sides_prompt_is_waiting(self):
        out = self.view(self.prompt("Lae'zel"))
        self.assertEqual(out, {"screen": "game", "region": "Act 1", "ms": 2, "waiting": sideview.WAITING})

    def test_the_mods_other_side_mark_is_waiting(self):
        ui = {"screen": "reaction", "page": "ReactionDecisionPopup", "reaction": {"other_side": True}, "ms": 2}
        self.assertEqual(self.view(ui), {"screen": "game", "ms": 2, "waiting": sideview.WAITING})

    def test_own_prompt_unchanged(self):
        ui = self.prompt("Tav")
        self.assertIs(self.view(ui), ui)

    def test_mixed_prompt_keeps_own_rows(self):
        out = self.view(self.prompt("Lae'zel", "Shadowheart"))
        self.assertEqual(out["screen"], "reaction")
        self.assertEqual(out["reaction"], {"options": [{"i": 2, "who": "Shadowheart", "name": "Shield", "level": 1}]})

    def test_rows_without_a_name_and_other_screens_unchanged(self):
        for ui in (
            {"screen": "reaction", "reaction": {"prompt": "x", "options": [{"i": 1}]}},
            {"screen": "game"},
            {"screen": "dialogue"},
            None,
            "busy",
        ):
            self.assertIs(self.view(ui), ui)

    def test_game_applies_it_to_ui_state_only_with_a_side(self):
        ui = self.prompt("Lae'zel")
        for side, screen in ((None, "reaction"), ("A", "game")):
            g = Game(port=1, side=side)
            g._team_names = lambda fresh=False: set(self.TEAM)
            self.assertEqual(g._view("ui.state", ui)["screen"], screen)
            self.assertIs(g._view("state.status", ui), ui)


if __name__ == "__main__":
    unittest.main()

"""Offline tests (D15): do_actions, queue_actions and combat_turn take a real JSON array/object as well as the same
JSON as a string, through the MCP argument validation, and both forms reach the tool as the same text; so do
level_up's and create_character's plan and set_reactions' settings.

Run: cd bridge && uv run python -m unittest discover -s tests -p test_batch_args.py
"""

from __future__ import annotations

import json
import unittest
from unittest import mock

from test_turn_macros import Base  # noqa: F401  (sets BG3AI_RUNTIME before importing the server)

# isort: split
from bg3ai.game import JobResult
from bg3ai.tools import batch, core, newgame, progression, saving, turn_tool, uitools

STEPS = [{"tool": "wait", "seconds": 1}, {"tool": "end_turn"}]
PLAN = {"move": "e12", "action": ["Target_MainHandAttack"]}


def validated(tool: str, args: dict) -> dict:
    """The arguments as the server hands them to the tool function (MCP pre-parse + pydantic)."""
    return core.mcp._tool_manager.get_tool(tool).fn_metadata.validate_arguments(args)


class Validation(unittest.TestCase):
    def test_batch_tools_take_a_list_or_a_string(self):
        for tool in ("do_actions", "queue_actions"):
            self.assertEqual(validated(tool, {"actions": STEPS})["actions"], STEPS)
            # a JSON string is pre-parsed into the same list (clients that stringify arrays)
            self.assertEqual(validated(tool, {"actions": json.dumps(STEPS)})["actions"], STEPS)
            self.assertEqual(validated(tool, {"actions": {"actions": STEPS}})["actions"], {"actions": STEPS})
            self.assertEqual(validated(tool, {"actions": "not json"})["actions"], "not json")

    def test_combat_turn_takes_an_object_a_list_or_a_string(self):
        chain = [{"who": "Karlach", "plan": PLAN}]
        self.assertEqual(validated("combat_turn", {"plan": PLAN})["plan"], PLAN)
        self.assertEqual(validated("combat_turn", {"plan": chain})["plan"], chain)
        self.assertEqual(validated("combat_turn", {"plan": json.dumps(PLAN)})["plan"], PLAN)
        self.assertEqual(validated("combat_turn", {"plan": {}})["plan"], {})

    def test_plan_and_settings_args_take_an_object_or_a_string(self):
        build = {"class": "Wizard", "name": "Ilse"}
        for tool, arg, value in (
            ("level_up", "plan", {"spells": ["Shield"]}),
            ("create_character", "plan", build),
            ("set_reactions", "settings", {"Shield": "ask"}),
        ):
            self.assertEqual(validated(tool, {arg: value})[arg], value, tool)
            self.assertEqual(validated(tool, {arg: json.dumps(value)})[arg], value, tool)  # pre-parsed, as above
            types = {o["type"] for o in core.mcp._tool_manager.get_tool(tool).parameters["properties"][arg]["anyOf"]}
            self.assertLessEqual({"object", "string"}, types, tool)

    def test_schemas_offer_the_array(self):
        for tool, arg in (("do_actions", "actions"), ("queue_actions", "actions"), ("combat_turn", "plan")):
            types = {o["type"] for o in core.mcp._tool_manager.get_tool(tool).parameters["properties"][arg]["anyOf"]}
            self.assertLessEqual({"array", "string"}, types, tool)


class SameText(unittest.TestCase):
    def test_as_json(self):
        self.assertEqual(batch._as_json(STEPS), json.dumps(STEPS))
        self.assertEqual(batch._as_json('[{"tool":"wait"}]'), '[{"tool":"wait"}]')

    def test_list_and_string_fail_alike(self):
        bad = [{"tool": "fly"}]
        self.assertEqual(batch.do_actions(bad), batch.do_actions(json.dumps(bad)))
        self.assertIn("'fly' can't be batched", batch.do_actions(bad))

    def test_a_refused_step_names_what_a_step_can_be(self):
        # live3 run 1: queue_actions [look, loot_area], later [loot_area]: the refusal named neither alternative
        out = batch.queue_actions([{"tool": "look"}, {"tool": "loot_area", "radius": 10}])
        self.assertIn("step 1: 'look' can't be batched or queued (a step is one of: camp, choose_dialogue,", out)
        self.assertIn("walk_toward; call other tools on their own)", out)

    def test_combat_turn_chain_from_a_list(self):
        chain = [{"who": "Karlach", "plan": PLAN}, {"who": "Gale", "plan": {}}]
        self.assertEqual(turn_tool._turn_chain(batch._as_json(chain)), [("Karlach", json.dumps(PLAN)), ("Gale", "{}")])
        self.assertIsNone(turn_tool._turn_chain(batch._as_json(PLAN)))


class ObjectArgs(unittest.TestCase):
    """The object and its JSON text reach the game alike (each failed with TypeError on json.loads(dict) before)."""

    def test_set_reactions(self):
        sent = []
        with (
            mock.patch.object(uitools, "_reactions_rpc", lambda char, args: sent.append(args) or {"ok": True}),
            mock.patch.object(uitools.plumbing, "_who", lambda who: "u1"),
        ):
            uitools.set_reactions(settings={"Shield": "ask"})
            uitools.set_reactions(settings='{"Shield": "ask"}')
        self.assertEqual(sent, [{"char": "u1", "set": {"Shield": "ask"}}] * 2)

    def test_level_up(self):
        got = []
        with (
            mock.patch.object(core.game, "try_rpc", lambda *a, **k: {}),
            mock.patch.object(progression.plumbing, "_who", lambda who: "u1"),
            mock.patch.object(progression, "_level_up", lambda uuid, plan: got.append(plan) or "level_up: done"),
        ):
            progression.level_up(plan={"spells": ["Shield"]})
            progression.level_up(plan='{"spells": ["Shield"]}')
            progression.level_up()
        self.assertEqual([json.loads(p) for p in got[:2]], [{"spells": ["Shield"]}] * 2)
        self.assertEqual(got[2], "")

    def test_create_character(self):
        sent = []

        def run_job(method, args, **kw):
            sent.append(args["plan"])
            return JobResult("failed", None, "stop here")

        with (
            mock.patch.object(saving, "PLAYTHROUGH", "pt"),
            mock.patch.object(saving, "SAVE_OWNER", ""),
            mock.patch.object(core, "_session_over", lambda: False),
            mock.patch.object(core.game, "run_job", run_job),
        ):
            newgame.create_character({"class": "Wizard", "name": "Ilse"})
            newgame.create_character('{"class": "Wizard", "name": "Ilse"}')
        self.assertEqual(sent, [{"class": "Wizard", "name": "Ilse"}] * 2)


if __name__ == "__main__":
    unittest.main()

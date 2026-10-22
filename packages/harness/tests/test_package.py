"""bg3harness locates the prompt files and the data contracts and exposes prompts/assemble.py."""

from __future__ import annotations

import importlib
import subprocess
import sys
import unittest
from pathlib import Path

import bg3harness

REPO = Path(__file__).resolve().parents[3]


class Package(unittest.TestCase):
    def test_paths_are_the_source_tree(self) -> None:
        self.assertEqual(bg3harness.PROMPTS, REPO / "packages" / "harness" / "prompts")
        self.assertEqual(bg3harness.SCHEMAS, REPO / "packages" / "harness" / "schemas")

    def test_schema_loads_by_stem(self) -> None:
        for name in ("facts.v1", "events.v1"):
            self.assertTrue(bg3harness.schema(name)["$id"].endswith(f"/schemas/{name}.schema.json"))

    def test_assemble_is_the_prompts_module(self) -> None:
        got = bg3harness.assemble("rules")
        self.assertEqual(got, bg3harness.prompts().assemble("rules"))
        self.assertEqual(bg3harness.prompts().__file__, str(bg3harness.PROMPTS / "assemble.py"))

    def test_leaf_modules_import_without_the_runner(self) -> None:
        # step 5: the moved modules import each other inside the package, never runner/ (its shims alias them). The
        # check is by the repo's runner/ path: GitHub's checkout lives under /home/runner/, so every path has "runner"
        mods = (
            "clients",
            "pi_client",
            "profiles",
            "sandbox",
            "display_env",
            "ramwatch",
            "host_tuning",
            "fairness",
            "triage",
            "model_time",
            "events",
            "facts",
            "choices",
            "game_settings",
            "game.io",
            "game.oom",
            "game.controller",
            "scenario_file",
            "game.saves",
        )
        code = (
            "import importlib, sys\n"
            + "".join(f"importlib.import_module('bg3harness.{m}')\n" for m in mods)
            + (
                f"runner = {str(REPO / 'runner') + '/'!r}\n"
                "files = {n: str(getattr(m, '__file__', '') or '') for n, m in sys.modules.items()}\n"
                "bad = sorted(n for n, f in files.items() if f.startswith(runner))\n"
                "assert not bad, bad\n"
            )
        )
        subprocess.run([sys.executable, "-I", "-c", code], check=True)

    def test_runner_shims_are_the_package_modules(self) -> None:
        sys.path.insert(0, str(REPO / "runner"))
        self.addCleanup(sys.path.remove, str(REPO / "runner"))
        events = importlib.import_module("events")
        self.assertIs(events, importlib.import_module("bg3harness.events"))
        self.assertIs(importlib.import_module("clients").Event, events.Event)


if __name__ == "__main__":
    unittest.main()

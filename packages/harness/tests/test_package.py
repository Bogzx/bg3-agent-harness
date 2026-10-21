"""bg3harness locates the prompt files and the data contracts and exposes prompts/assemble.py."""

from __future__ import annotations

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


if __name__ == "__main__":
    unittest.main()

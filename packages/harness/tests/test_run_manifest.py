"""bg3harness.run_manifest: the run's manifest fields (seed, timing, client, prompt hash, tool list hash)."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

import bg3harness
from bg3harness import run_manifest

REPO = Path(__file__).resolve().parents[3]
# the e2e claude run's bg3 tools (runner/fixtures/claude: its server's registry, all on) and a v2 scenario
REGISTRY = json.loads((REPO / "runner" / "fixtures" / "claude" / "e2e-naut-init.json").read_text())["registry"]
NAUT = json.loads((REPO / "packages" / "bench" / "scenarios" / "nautiloid.json").read_text())
CLAUDE_MD = REPO / "play" / "CLAUDE.md"


class Fields(unittest.TestCase):
    def test_claude_rules_and_coaching_is_play_claude_md(self) -> None:
        f = run_manifest.manifest_fields("claude", "2.1.301", "rules+coaching", REGISTRY, NAUT)
        self.assertEqual((f["seed"], f["timing"]), (NAUT["game_settings"]["seed"], NAUT["timing"]))
        self.assertEqual(f["client"], {"name": "claude", "version": "2.1.301"})
        asm = bg3harness.assemble("rules+coaching", "claude")
        self.assertEqual(f["prompt"], {"sha256": asm.sha256, "parts": asm.parts})
        body = CLAUDE_MD.read_text(encoding="utf-8").split("-->\n\n", 1)[1]  # play/CLAUDE.md without its header
        self.assertEqual(f["prompt"]["sha256"], run_manifest.hashlib.sha256(body.encode("utf-8")).hexdigest())
        self.assertEqual(f["tools"]["count"], 94)

    def test_tools_off_change_the_prompt_and_the_hash(self) -> None:
        wiki = set(bg3harness.prompts().CAPABILITIES["wiki"])
        less = [t for t in REGISTRY if t not in wiki]
        full, off = (run_manifest.manifest_fields("pi", "1.3.0", "rules", t, None) for t in (REGISTRY, less))
        self.assertIn(
            {
                "file": "rules/capabilities/wiki.md",
                "sha256": run_manifest.hashlib.sha256(
                    (bg3harness.PROMPTS / "rules/capabilities/wiki.md").read_bytes()
                ).hexdigest(),
            },
            full["prompt"]["parts"],
        )
        self.assertNotIn("rules/capabilities/wiki.md", [p["file"] for p in off["prompt"]["parts"]])
        self.assertEqual((off["tools"]["count"], off["seed"], off["timing"]), (92, None, None))
        self.assertNotEqual(off["tools"]["allowlist_sha256"], full["tools"]["allowlist_sha256"])
        with self.assertRaisesRegex(ValueError, "needs every capability"):  # coaching names the wiki
            run_manifest.manifest_fields("claude", None, "rules+coaching", less, None)

    def test_tools_digest_ignores_order(self) -> None:
        self.assertEqual(run_manifest.tools_digest(["b", "a"]), run_manifest.tools_digest(["a", "b", "a"]))
        self.assertEqual(run_manifest.tools_digest(["a"])["count"], 1)

    def test_client_version(self) -> None:
        asked: list[str] = []

        def cli(name: str) -> str:
            asked.append(name)
            return "2.1.301"

        self.assertEqual(run_manifest.client_version("claude", cli, lambda: "x"), "2.1.301")
        self.assertEqual(run_manifest.client_version("pi", cli, lambda: "1.3.0"), "1.3.0")
        self.assertIsNone(run_manifest.client_version("baseline", cli, lambda: "x"))
        self.assertEqual(asked, ["claude"])

    def test_artifact_kwargs(self) -> None:
        f = run_manifest.manifest_fields("pi", "1.3.0", "rules", REGISTRY, NAUT)
        self.assertEqual(
            run_manifest.artifact_kwargs(f),
            {
                "prompt_sha": f["prompt"]["sha256"],
                "prompt_parts": f["prompt"]["parts"],
                "tools": f["tools"],
                "client_version": "1.3.0",
                "condition": "rules",
            },
        )


if __name__ == "__main__":
    unittest.main()

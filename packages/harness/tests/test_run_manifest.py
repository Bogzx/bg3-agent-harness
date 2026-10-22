"""bg3harness.run_manifest: the run's manifest fields (seed, timing, client, prompt hash, tool list hash)."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

import bg3harness
from bg3harness import run_manifest

REPO = Path(__file__).resolve().parents[3]
# the e2e claude run's bg3 tools (runner/fixtures/claude: its server's registry, all on) and a v2 scenario
REGISTRY = json.loads((REPO / "runner" / "fixtures" / "claude" / "e2e-naut-init.json").read_text())["registry"]
NAUT = json.loads((REPO / "packages" / "bench" / "scenarios" / "nautiloid.json").read_text())
CLAUDE_MD = REPO / "play" / "CLAUDE.md"
INSTR = "Tools to play Baldur's Gate 3 as the player."  # stands in for the server's instructions


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class Fields(unittest.TestCase):
    def test_claude_rules_and_coaching_is_play_claude_md(self) -> None:
        f = run_manifest.manifest_fields("claude", "2.1.301", "rules+coaching", REGISTRY, NAUT, INSTR)
        self.assertEqual((f["seed"], f["timing"]), (NAUT["game_settings"]["seed"], NAUT["timing"]))
        self.assertEqual(f["client"], {"name": "claude", "version": "2.1.301"})
        asm = bg3harness.assemble("rules+coaching", "claude")
        instr = {"file": "server:instructions", "sha256": sha(INSTR)}
        self.assertEqual(f["prompt"]["parts"], [instr, *asm.parts])
        body = CLAUDE_MD.read_text(encoding="utf-8").split("-->\n\n", 1)[1]  # play/CLAUDE.md without its header
        self.assertEqual(f["prompt"]["sha256"], sha(f"{INSTR}\n\n=== RULES ===\n{body.strip()}\n"))
        self.assertEqual(f["tools"]["count"], 94)

    def test_the_hash_covers_the_server_instructions(self) -> None:
        # it hashed the rules only: a change in what every client gets in `instructions` left it the same
        a, b = (run_manifest.manifest_fields("pi", None, "rules", REGISTRY, None, i)["prompt"] for i in (INSTR, "x"))
        self.assertNotEqual(a["sha256"], b["sha256"])
        self.assertEqual(a["parts"][1:], b["parts"][1:])

    def test_the_hash_is_pis_system_text(self) -> None:
        # the bg3 server dumped offline with screenshots off: its --instructions give the manifest the hash of the
        # --system file pi is handed (rules condition)
        with tempfile.TemporaryDirectory() as d:
            env = {**os.environ, "BG3AI_TOOLS_OFF": "screenshots", "BG3AI_PROMPT_CONDITION": "rules"}
            files = {k: Path(d) / k for k in ("system", "tools", "instructions")}
            subprocess.run(
                [
                    "uv",
                    "run",
                    "--project",
                    str(REPO / "bridge"),
                    "python",
                    str(REPO / "tools/pi/dump_server.py"),
                    *[x for k, f in files.items() for x in (f"--{k}", str(f))],
                ],
                check=True,
                capture_output=True,
                timeout=300,
                env=env,
            )
            tools = [t["name"] for t in json.loads(files["tools"].read_text())]
            self.assertNotIn("screenshot", tools)
            f = run_manifest.manifest_fields("pi", None, "rules", tools, None, files["instructions"].read_text())
            self.assertEqual(f["prompt"]["sha256"], sha(files["system"].read_text()))

    def test_tools_off_change_the_prompt_and_the_hash(self) -> None:
        wiki = set(bg3harness.prompts().CAPABILITIES["wiki"])
        less = [t for t in REGISTRY if t not in wiki]
        full, off = (run_manifest.manifest_fields("pi", "1.3.0", "rules", t, None, INSTR) for t in (REGISTRY, less))
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
            run_manifest.manifest_fields("claude", None, "rules+coaching", less, None, INSTR)

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
        f = run_manifest.manifest_fields("pi", "1.3.0", "rules", REGISTRY, NAUT, INSTR)
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

"""bg3harness's lazy imports resolve inside the package: an isolated interpreter (-I, no runner/ on sys.path) once hit
ModuleNotFoundError on runs/clients' __import__("pi_client") and model_time's `import judge` (runner/'s shims)."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


def isolated(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-I", *args], capture_output=True, text=True, check=False)


class NoRunnerPath(unittest.TestCase):
    def test_pi_parser_and_client_come_from_the_package(self) -> None:
        code = (
            "import sys\n"
            "from bg3harness import clients, pi_client, profiles, runs\n"
            "parsers = {}\n"
            "assert runs.stream_events([], parsers, 'pi') == []\n"
            "assert type(parsers['pi']) is pi_client.PiParser, parsers\n"
            "p = profiles.parse({'client': 'pi', 'model': 'm', 'endpoint': {'provider': 'x', "
            "'base_url': 'http://x', 'api_key_env': 'K'}, 'model_info': {'input': ['text'], 'reasoning': False, "
            "'context_window': 1000, 'max_tokens': 100}})\n"
            "c = clients.make('pi', profile=p, system_file='s.md', server={})\n"
            "assert type(c) is pi_client.PiClient, c\n"
            "assert 'pi_client' not in sys.modules\n"
        )
        got = isolated("-c", code)
        self.assertEqual(got.returncode, 0, got.stderr)

    def test_model_time_cli_finds_a_run(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            sdir = Path(d) / "pt"
            sdir.mkdir()
            (sdir / "runs.jsonl").write_text(json.dumps({"run": 1, "started": 0.0, "t": 1.0}) + "\n")
            got = isolated("-m", "bg3harness.model_time", str(sdir), "--run", "1")
            self.assertEqual(got.returncode, 0, got.stderr)
            self.assertEqual(json.loads(got.stdout), {"sessions": 0})
            missing = isolated("-m", "bg3harness.model_time", str(sdir), "--run", "2")
            self.assertEqual(missing.returncode, 2, missing.stderr)
            self.assertIn("no run 2", missing.stderr)


if __name__ == "__main__":
    unittest.main()

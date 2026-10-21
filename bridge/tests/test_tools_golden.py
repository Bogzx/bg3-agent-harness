"""The tools/list the server answers (names, order, descriptions, input/output schemas, annotations, _meta) equals
the golden dump in golden/tools_list.json. Guards the server.py split (B-split): moving tool code between modules
must not change what any client sees. A deliberate tool interface change regenerates the dump and says why in
bridge/CHANGELOG.md:

    cd bridge && uv run python tests/test_tools_golden.py --update

The server is built in a subprocess with no BG3AI_* settings (the tool set is fixed at import).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

BRIDGE = Path(__file__).resolve().parents[1]
GOLDEN = Path(__file__).resolve().parent / "golden" / "tools_list.json"

PROBE = """
import asyncio, json
from bg3ai import server
print(json.dumps([t.model_dump(mode="json") for t in asyncio.run(server.mcp.list_tools())]))
"""


def dump() -> list[dict]:
    with tempfile.TemporaryDirectory(prefix="bg3golden-") as rt:
        env = {k: v for k, v in os.environ.items() if not k.startswith(("BG3AI_", "BG3_", "BG3LE_"))}
        env.update({"BG3AI_RUNTIME": rt, "BG3AI_MEMORY_DIR": rt, "BG3LE_DEBUG_PORT": "1"})
        r = subprocess.run(
            [sys.executable, "-c", PROBE], cwd=BRIDGE, env=env, capture_output=True, text=True, check=False, timeout=120
        )
    if r.returncode != 0:
        raise AssertionError(r.stderr[-2000:])
    return json.loads(r.stdout.strip().splitlines()[-1])


class ToolsGolden(unittest.TestCase):
    def test_tools_list_matches_the_golden_dump(self):
        want = json.loads(GOLDEN.read_text(encoding="utf-8"))
        got = dump()
        self.assertEqual([t["name"] for t in got], [t["name"] for t in want], "tool names or order changed")
        for g, w in zip(got, want, strict=True):
            self.assertEqual(g, w, f"tool {w['name']} changed")


if __name__ == "__main__":
    if sys.argv[1:] == ["--update"]:
        GOLDEN.write_text(json.dumps(dump(), indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"wrote {GOLDEN}")
    else:
        unittest.main()

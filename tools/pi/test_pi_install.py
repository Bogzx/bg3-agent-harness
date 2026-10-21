#!/usr/bin/env python3
"""The pinned pi install in tools/pi: package.json, the lockfile and node_modules all say pi 1.3.0. The version
check runs the installed binary; it skips with the install command when node_modules is missing.

    python3 -m unittest tools/pi/test_pi_install.py
"""
from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path

PI_DIR = Path(__file__).resolve().parent
PI_VERSION = "1.3.0"
PACKAGES = ("@earendil-works/pi-coding-agent", "@earendil-works/pi-mcp")
BINARY = PI_DIR / "node_modules" / ".bin" / "pi"


class PinnedTest(unittest.TestCase):
    def test_package_json_pins_exact_versions(self):
        deps = json.loads((PI_DIR / "package.json").read_text())["dependencies"]
        self.assertEqual({p: deps[p] for p in PACKAGES}, {p: PI_VERSION for p in PACKAGES})

    def test_lockfile_resolves_the_pinned_version(self):
        lock = json.loads((PI_DIR / "package-lock.json").read_text())["packages"]
        for p in PACKAGES:
            self.assertEqual(lock[f"node_modules/{p}"]["version"], PI_VERSION, p)

    @unittest.skipUnless(BINARY.exists(), "tools/pi/node_modules missing: cd tools/pi && npm ci --ignore-scripts")
    def test_installed_binary_version(self):
        out = subprocess.run([str(BINARY), "--version"], capture_output=True, text=True, timeout=60, check=True)
        self.assertEqual(out.stdout.strip(), PI_VERSION)


if __name__ == "__main__":
    unittest.main()

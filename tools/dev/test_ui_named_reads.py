"""Client/UI.lua reads live elements by name, never with GetAllProperties (S017's crash).

Runs tools/dev/test_ui_named_reads.lua with a Lua 5.3 interpreter: $LUA, else lua5.3 or lua on PATH.
None on this machine without sudo? `apt-get download lua5.3 liblua5.3-0` and `dpkg -x` them, then
LUA=<root>/usr/bin/lua5.3 LD_LIBRARY_PATH=<root>/usr/lib/x86_64-linux-gnu.
Run: python3 -m unittest tools/dev/test_ui_named_reads.py
"""
import os
import shutil
import subprocess
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
UI_LUA = REPO / "mod" / "Mods" / "BG3AI" / "ScriptExtender" / "Lua" / "Client" / "UI.lua"
TEST = Path(__file__).with_suffix(".lua")
LUA = os.environ.get("LUA") or shutil.which("lua5.3") or shutil.which("lua")


@unittest.skipUnless(LUA, "no Lua 5.3 interpreter ($LUA, lua5.3, lua)")
class NamedReadsTest(unittest.TestCase):
    def test_lua(self):
        r = subprocess.run([LUA, str(TEST), str(UI_LUA)], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("all ok", r.stdout)


if __name__ == "__main__":
    unittest.main()

"""runner/dashboard/serve.py is a shim over the playthrough app: the old flags map onto bg3play-serve's, and running
it serves the old endpoints on the old port. The endpoint tests live in packages/playthrough/tests/test_dashboard.py."""

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(HERE))
import serve as SV


def flags(args: list[str]) -> dict:
    out, i = {}, 0
    while i < len(args):
        if i + 1 < len(args) and not args[i + 1].startswith("--"):
            out[args[i]] = args[i + 1]
            i += 2
        else:
            out[args[i]] = True
            i += 1
    return out


class Translate(unittest.TestCase):
    def test_defaults(self):
        f = flags(SV.translate([]))
        self.assertEqual((f["--port"], f["--host"], f["--status-interval"]), ("8765", "127.0.0.1", "3.0"))
        self.assertTrue(f["--game"] and f["--screen"])
        self.assertEqual(f["--playthroughs"], str(REPO / "playthroughs"))
        self.assertNotIn("--memory-dir", f)
        SV.server.parser().parse_args(SV.translate([]))   # bg3play-serve accepts all of them

    def test_old_flags(self):
        args = SV.translate(["--port", "9000", "--host", "0.0.0.0", "--no-screen", "--memory-dir", "/m",
                             "--debug-port", "1", "--screen-width", "960", "--verbose"])
        f = flags(args)
        self.assertNotIn("--screen", f)
        self.assertEqual((f["--port"], f["--host"], f["--memory-dir"], f["--debug-port"], f["--screen-width"]),
                         ("9000", "0.0.0.0", "/m", "1", "960"))
        opt = SV.server.parser().parse_args(args)
        self.assertEqual((opt.port, opt.memory_dir, opt.screen, opt.verbose), (9000, Path("/m"), False, True))


class Runs(unittest.TestCase):
    def test_serves_the_old_endpoints(self):
        tmp = Path(tempfile.mkdtemp(prefix="bg3dash-rt-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        (tmp / "runner.status.json").write_text(json.dumps({"playthrough": "run1", "deadline": 4e9}))
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        env = dict(os.environ, BG3AI_RUNTIME=str(tmp))
        p = subprocess.Popen([sys.executable, str(HERE / "serve.py"), "--port", str(port), "--no-screen",
                              "--debug-port", "1", "--memory-dir", str(tmp / "memory")],
                             env=env, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        self.addCleanup(p.stdout.close)
        self.addCleanup(p.wait, 10)
        self.addCleanup(p.terminate)
        self.assertIn(f":{port}/", p.stdout.readline())   # the ready line
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/state", timeout=10) as r:
            s = json.loads(r.read())
        self.assertEqual((s["playthrough"], s["clock"]["source"]), ("run1", "runner"))
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/series", timeout=10) as r:
            self.assertIn(b"bench", r.read())


if __name__ == "__main__":
    unittest.main()

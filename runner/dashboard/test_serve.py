"""HTTP-level test of the dashboard server against a temp runtime dir (no game, no screen)."""

import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
TMP = Path(tempfile.mkdtemp(prefix="bg3dash-rt-"))
os.environ["BG3AI_RUNTIME"] = str(TMP)
sys.path.insert(0, str(HERE))
import serve as SV  # noqa: E402


class ServeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        mem = TMP / "memory"
        (mem / "sessions").mkdir(parents=True)
        (mem / "HANDOFF.md").write_text("# Handoff S001 → S002\nSave to load: x\n")
        (mem / ".session.json").write_text(json.dumps({"current": 1, "open": True}))
        (mem / "sessions" / "S001.md").write_text("# S001\n- 10:00 [quest] hi\n")
        (TMP / "actions.jsonl").write_text(
            json.dumps({"t": 1, "tool": "get_status", "args": {}, "ms": 5, "result": "WHERE x"}) + "\n")
        (TMP / "runner.status.json").write_text(json.dumps({"playthrough": "run1", "deadline": 4e9}))
        logs = TMP / "sessions" / "run1"
        logs.mkdir(parents=True)
        (logs / "S001.log").write_text("[say] I will head north.\n[tool] get_status {}\n")
        opt = SV.argparse.Namespace(no_screen=True, memory_dir=str(mem), verbose=False, debug_port=1,
                                    status_interval=3.0, screen_interval=5.0, screen_width=960)
        cls.hub = SV.Hub(opt)
        cls.srv = SV.ThreadingHTTPServer(("127.0.0.1", 0), SV.make_handler(cls.hub))
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        shutil.rmtree(TMP, ignore_errors=True)

    def get(self, path):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=5) as r:
            return r.status, r.headers.get("Content-Type"), r.read()

    def test_index(self):
        code, ctype, body = self.get("/")
        self.assertEqual(code, 200)
        self.assertIn("text/html", ctype)
        self.assertIn(b"BG3 AI Live", body)

    def test_state(self):
        _, _, body = self.get("/api/state")
        s = json.loads(body)
        self.assertEqual(s["playthrough"], "run1")
        self.assertEqual(s["clock"]["source"], "runner")
        self.assertEqual(s["memory"]["session"], "S001")
        self.assertIn("Handoff S001", s["memory"]["handoff"])
        self.assertFalse(s["screen"]["enabled"])
        self.assertTrue(self.hub.watching())

    def test_feed_incremental(self):
        _, _, body = self.get("/api/feed?after=-1")
        r = json.loads(body)
        self.assertEqual([i["tool"] for i in r["items"]], ["get_status"])
        _, _, body = self.get(f"/api/feed?after={r['offset']}")
        self.assertEqual(json.loads(body)["items"], [])

    def test_agentlog(self):
        _, _, body = self.get("/api/agentlog?after=-1&path=")
        r = json.loads(body)
        self.assertEqual(r["path_id"], "S001.log")
        self.assertIn("[say] I will head north.", r["text"])

    def test_story(self):
        (TMP / "memory" / "decisions.md").write_text("## Decisions\n\n## D-01 Freed Shadowheart (S002)\n- Choice: opened the pod\n")
        _, _, body = self.get("/api/story")
        r = json.loads(body)
        self.assertTrue(r["exists"])
        self.assertEqual([d["id"] for d in r["decisions"]], ["D-01"])
        self.assertEqual(r["chapters"], [])
        code, _, body = self.get("/")
        self.assertIn(b'id="storyTabs"', body)

    def test_screen_empty_and_404(self):
        code, _, _ = self.get("/api/screen")
        self.assertEqual(code, 204)
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self.get("/nope")
        self.assertEqual(cm.exception.code, 404)


if __name__ == "__main__":
    unittest.main()

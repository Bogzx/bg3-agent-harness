"""codex_usage reads the Codex CLI's login, the file a Codex profile reads, and refuses an expired token unprinted.

python3 -m unittest tools/dev/test_codex_usage.py
"""

from __future__ import annotations

import base64
import contextlib
import io
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import codex_usage


def login(path: Path, expires: float) -> str:
    body = base64.urlsafe_b64encode(json.dumps({"exp": expires}).encode()).decode().rstrip("=")
    token = f"head.{body}.sig"
    path.write_text(json.dumps({"auth_mode": "chatgpt", "tokens": {"access_token": token, "account_id": "acc-1"}}))
    return token


class Credentials(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.auth = Path(tmp.name) / "auth.json"

    def test_the_cli_login_gives_its_token_and_account(self):
        token = login(self.auth, time.time() + 3600)
        self.assertEqual(codex_usage.credentials(self.auth), (token, "acc-1"))

    def test_an_expired_token_is_refused_without_printing_it(self):
        token = login(self.auth, time.time() - 1)
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as stop:
            codex_usage.credentials(self.auth)
        self.assertIn("expired", str(stop.exception))
        self.assertNotIn(token, str(stop.exception) + out.getvalue())

    def test_no_login_is_refused(self):
        self.auth.write_text("{}")
        with self.assertRaisesRegex(SystemExit, "no Codex CLI login"):
            codex_usage.credentials(self.auth)

    def test_the_default_is_the_file_the_profile_reads(self):
        self.assertEqual(codex_usage.AUTH, Path.home() / ".codex" / "auth.json")


if __name__ == "__main__":
    unittest.main()

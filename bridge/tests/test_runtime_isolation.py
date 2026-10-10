"""Tests never write into the live runtime/ dir (logs pass 4: 1 536 fake lines in runtime/actions.jsonl)."""

import unittest
from pathlib import Path

from bg3ai import server


class RuntimeIsolation(unittest.TestCase):
    def test_runtime_is_not_the_repo_runtime(self):
        self.assertNotEqual(server.RUNTIME.resolve(), (Path(server.REPO) / "runtime").resolve())


if __name__ == "__main__":
    unittest.main()

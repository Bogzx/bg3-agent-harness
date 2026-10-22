"""bg3harness.__version__ is pyproject.toml's version (the one source), read from the source tree when it is there."""

from __future__ import annotations

import tomllib
import unittest
from importlib import metadata
from pathlib import Path
from tempfile import TemporaryDirectory

import bg3harness


class VersionTest(unittest.TestCase):
    def test_version_is_the_pyproject_version(self) -> None:
        pyproject = tomllib.loads((bg3harness.PACKAGE_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertEqual(bg3harness.__version__, pyproject["project"]["version"])

    def test_the_source_tree_wins_over_stale_install_metadata(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "pyproject.toml").write_text('[project]\nname = "x"\nversion = "9.8.7"\n', encoding="utf-8")
            self.assertEqual(bg3harness.package_version("bg3-agent-harness", root), "9.8.7")
            # no source tree (a wheel install): the installed distribution's
            (root / "pyproject.toml").unlink()
            self.assertEqual(
                bg3harness.package_version("bg3-agent-harness", root), metadata.version("bg3-agent-harness")
            )


if __name__ == "__main__":
    unittest.main()

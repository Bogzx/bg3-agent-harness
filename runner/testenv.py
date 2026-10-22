"""Imported by every runner test module before play: the game's profile folders become a temp dir for the process.

bg3harness.runner.env reads SAVE_DIR and SE_BG3AI_DIR (steam_paths: $BG3AI_SAVE_DIR / $BG3AI_SE_DIR, else the real
Larian profile) once, at import. A test that reached the run reset with the real ones moved the live game's Script
Extender BG3AI/nav_*.json files away (live5 §0.4). bg3harness.testing.profile sets them (forced: a shell that
exports the real folders must not leak them into a test). test_play_isolation.RealDirs checks every runner test
module imports this before play, the legacy runner or bg3harness.runner. It also puts packages/harness/src and
bridge/src on sys.path (runner/_harness.py), so a test module can import bg3harness.runner without importing play first.
"""

from pathlib import Path

import _harness  # noqa: F401  (first: bg3harness on sys.path; nothing it imports reads the profile folders)
from bg3harness.testing.profile import temp_profile

ROOT = temp_profile()  # the same dir as packages/harness/tests/conftest.py's when both run in one process
SAVE_DIR = ROOT / "saves"
SE_DIR = ROOT / "se"


def home(name: str):
    """The module a name of the old runner/play.py lives in now (Phase D.3: bg3harness.runner's modules, the legacy
    runner/legacy_*.py, the game's bg3harness.game modules): where a test patches it so the code that runs sees it."""
    import ast
    import importlib

    for mod in (
        "bg3harness.runner.env",
        "bg3harness.runner.policy",
        "bg3harness.runner.codewatch",
        "bg3harness.runner.state",
        "bg3harness.runner.game_side",
        "bg3harness.runner.agent_side",
        "bg3harness.runner.checks_side",
        "bg3harness.runner.core",
        "legacy_playthrough",
        "legacy_series",
        "legacy_cli",
        "play",
    ):
        m = importlib.import_module(mod)
        if name in _defined(m, ast):
            return m
    raise KeyError(name)


_DEFINED: dict = {}


def _defined(m, ast) -> set:
    if m.__name__ not in _DEFINED:
        tree = ast.parse(Path(m.__file__).read_text())
        names = set()
        for n in tree.body:
            if isinstance(n, (ast.FunctionDef, ast.ClassDef)):
                names.add(n.name)
            elif isinstance(n, (ast.Assign, ast.AnnAssign)):
                for t in n.targets if isinstance(n, ast.Assign) else [n.target]:
                    names.update(x.id for x in ast.walk(t) if isinstance(x, ast.Name))
        _DEFINED[m.__name__] = names
    return _DEFINED[m.__name__]

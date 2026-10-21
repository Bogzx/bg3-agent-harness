"""Moved to packages/playthrough/tests/memory/_testutil.py; this shim keeps `import _testutil` working for the
runner's tests (runner/test_play.py) and is that module object."""
import importlib.util
import sys
from pathlib import Path

_PKG = Path(__file__).resolve().parents[2] / "packages" / "playthrough"
sys.path.insert(0, str(_PKG / "src"))
_spec = importlib.util.spec_from_file_location("_testutil", _PKG / "tests" / "memory" / "_testutil.py")
_m = importlib.util.module_from_spec(_spec)
sys.modules[__name__] = _m
_spec.loader.exec_module(_m)

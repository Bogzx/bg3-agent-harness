"""Moved to packages/playthrough: bg3play.memory.keeper. This shim keeps `import memory_keeper` and the old path
working; the module object is the package's, so mock.patch on it patches the code that runs."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "packages" / "playthrough" / "src"))
from bg3play.memory import keeper as _m

sys.modules[__name__] = _m

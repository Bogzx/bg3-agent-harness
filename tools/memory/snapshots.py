"""Moved to packages/playthrough: bg3play.memory.snapshots. This shim keeps `import snapshots` and the old path
working; the module object is the package's, so mock.patch on it patches the code that runs."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "packages" / "playthrough" / "src"))
from bg3play.memory import snapshots as _m

if __name__ == "__main__":
    sys.exit(_m.main())
sys.modules[__name__] = _m

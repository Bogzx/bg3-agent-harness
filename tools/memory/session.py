"""Moved to packages/playthrough: bg3play.memory.session. This shim keeps `import session` and the old path
working; the module object is the package's, so mock.patch on it patches the code that runs."""
import os
import sys
from pathlib import Path

# The clock hook's fast path (see bg3play.memory.session): exit before importing the package.
if __name__ == "__main__" and sys.argv[1:2] == ["clock"] and os.environ.get("BG3AI_SESSION_DEADLINE"):
    sys.exit(0)

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "bridge" / "src"))  # bg3play's dependency (bg3ai)
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "packages" / "playthrough" / "src"))
from bg3play.memory import session as _m

if __name__ == "__main__":
    sys.exit(_m.main())
sys.modules[__name__] = _m

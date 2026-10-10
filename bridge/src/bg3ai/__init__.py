"""bg3ai bridge package.

Under a test runner (python -m unittest / pytest) with no BG3AI_RUNTIME set, every runtime path (actions.jsonl,
metrics.jsonl, levelup_clicks.json, skipper.lock) goes to a throwaway temp dir: before this, tests appended
1 536 fake lines (uuids 11111111-2222-…, coords (30,0,0)) to the live runtime/actions.jsonl.
"""

import os as _os
import sys as _sys


def _under_tests() -> bool:
    argv0 = _sys.argv[0] if _sys.argv else ""
    return "pytest" in _sys.modules or argv0.endswith(("unittest/__main__.py", "unittest")) or \
        "_jb_unittest" in argv0


if _under_tests() and not _os.environ.get("BG3AI_RUNTIME"):
    import atexit as _atexit
    import shutil as _shutil
    import tempfile as _tempfile

    _tmp = _tempfile.mkdtemp(prefix="bg3ai-testrt-")
    _atexit.register(_shutil.rmtree, _tmp, True)
    _os.environ["BG3AI_RUNTIME"] = _tmp

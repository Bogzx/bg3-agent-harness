"""Moved to bg3ai.lsf (the bridge: the lowest layer, which bench and the tools import too). This name stays only for
runner/legacy_playthrough.py's `import lsf` with tools/saves on sys.path; delete it when that imports bg3ai.lsf."""

from bg3ai.lsf import Attr, LsfError, attributes  # noqa: F401  (re-exported)

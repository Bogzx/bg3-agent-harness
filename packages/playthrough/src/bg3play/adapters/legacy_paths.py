"""TEMPORARY (Phase D): this checkout's bridge/src (bg3ai) and runner/ on sys.path, for what the app still reads
from them until the harness package exposes it: bg3ai.metrics / progress / milestones (stdlib only; the live
latency and progress views), bg3ai.game / screen (adapters/bridge_status.py) and runner/ramwatch.py (thermal)."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[5]  # packages/playthrough/src/bg3play/adapters


def ensure() -> None:
    for p in (REPO / "bridge" / "src", REPO / "runner"):
        if str(p) not in sys.path:
            sys.path.append(str(p))


def load(name: str) -> Any:
    """ensure(), then the module (Any to pyright: the bridge and runner/ ship no types)."""
    ensure()
    return importlib.import_module(name)

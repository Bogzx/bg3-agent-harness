"""TEMPORARY (Phase D): this checkout's bridge/src (bg3ai) on sys.path, for what the app still reads from it until
the harness package exposes it: bg3ai.metrics / progress / milestones (stdlib only; the live latency and progress
views), bg3ai.game / screen (adapters/bridge_status.py), and bg3harness.runner's policy (it reads bg3ai)."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[5]  # packages/playthrough/src/bg3play/adapters


def ensure() -> None:
    p = REPO / "bridge" / "src"
    if str(p) not in sys.path:
        sys.path.append(str(p))


def load(name: str) -> Any:
    """ensure(), then the module (Any to pyright: the bridge ships no types)."""
    ensure()
    return importlib.import_module(name)

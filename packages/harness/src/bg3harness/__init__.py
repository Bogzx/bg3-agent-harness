"""The BG3 agent harness (import name bg3harness, distribution bg3-agent-harness).

The prompt files and the data contracts stay where their owners keep them, next to this source tree:

    packages/harness/prompts/    rules, coaching and client files plus assemble.py (stdlib only; the bridge loads it
                                 by path)
    packages/harness/schemas/    facts.v1 / events.v1 JSON Schemas

`PROMPTS` and `SCHEMAS` locate them, `schema(name)` loads one, and `assemble` is prompts/assemble.py's function. The
paths hold for the workspace's editable install, which is the only install today.
"""

from __future__ import annotations

import importlib.util
import json
from functools import cache
from pathlib import Path
from types import ModuleType
from typing import Any

PACKAGE_ROOT = Path(__file__).resolve().parents[2]
PROMPTS = PACKAGE_ROOT / "prompts"
SCHEMAS = PACKAGE_ROOT / "schemas"

__all__ = ["PACKAGE_ROOT", "PROMPTS", "SCHEMAS", "assemble", "prompts", "schema"]


def schema(name: str) -> dict[str, Any]:
    """One JSON Schema by stem, e.g. schema("facts.v1") reads schemas/facts.v1.schema.json."""
    data: dict[str, Any] = json.loads((SCHEMAS / f"{name}.schema.json").read_text(encoding="utf-8"))
    return data


@cache
def prompts() -> ModuleType:
    """prompts/assemble.py as a module (loaded by path, as the bridge does)."""
    spec = importlib.util.spec_from_file_location("bg3harness._assemble", PROMPTS / "assemble.py")
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {PROMPTS / 'assemble.py'}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def assemble(condition: str, client: str = "mcp", **kwargs: Any) -> Any:
    """prompts/assemble.py's assemble(condition, client, ...) -> Assembly(text, sha256, parts)."""
    return prompts().assemble(condition, client, **kwargs)

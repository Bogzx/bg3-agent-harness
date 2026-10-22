"""The BG3 agent harness (import name bg3harness, distribution bg3-agent-harness).

The prompt files and the data contracts stay where their owners keep them, next to this source tree:

    packages/harness/prompts/    rules, coaching and client files plus assemble.py (stdlib only; the bridge loads it
                                 by path)
    packages/harness/schemas/    facts.v1 / events.v1 JSON Schemas

`PROMPTS` and `SCHEMAS` locate them, `schema(name)` loads one, and `assemble` is prompts/assemble.py's function. In the
source tree (the workspace's editable install, SOURCE_TREE) they are those folders; a wheel carries copies inside the
package (bg3harness/prompts, bg3harness/schemas: pyproject's force-include), and an installed package reads those.

`__version__` is the harness version (pyproject.toml's, the one source; CHANGELOG.md says when it bumps): run records,
manifests and results cite it.
"""

from __future__ import annotations

import importlib.util
import json
import tomllib
from functools import cache
from importlib import metadata
from pathlib import Path
from types import ModuleType
from typing import Any

PACKAGE_ROOT = Path(__file__).resolve().parents[2]
SOURCE_TREE = (PACKAGE_ROOT / "src" / "bg3harness" / "__init__.py").resolve() == Path(__file__).resolve()
_DATA = PACKAGE_ROOT if SOURCE_TREE else Path(__file__).resolve().parent  # a wheel: the copies inside the package
PROMPTS = _DATA / "prompts"
SCHEMAS = _DATA / "schemas"

__all__ = [
    "PACKAGE_ROOT",
    "PROMPTS",
    "SCHEMAS",
    "SOURCE_TREE",
    "__version__",
    "assemble",
    "package_version",
    "prompts",
    "schema",
]


def package_version(dist: str, root: Path) -> str:
    """A package's version: its pyproject.toml's under `root` when the source tree is there (the runner imports the
    packages by path, uninstalled; an editable install's metadata keeps the version of its last sync), else the
    installed distribution's."""
    pyproject = root / "pyproject.toml"
    if pyproject.is_file():
        version: str = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]["version"]
        return version
    return metadata.version(dist)


__version__ = package_version("bg3-agent-harness", PACKAGE_ROOT)


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

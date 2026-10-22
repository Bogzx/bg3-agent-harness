"""bg3bench: the BG3 benchmark (graders over facts.jsonl, statistics, manifests, results).

`__version__` is pyproject.toml's version (the one source); the benchmark-suite version (`bench-vX.Y`) is derived
from it in bg3bench.suite.
"""

from pathlib import Path

from bg3harness import package_version

__version__ = package_version("bg3bench", Path(__file__).resolve().parents[2])

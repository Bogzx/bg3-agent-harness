"""Moved to bg3harness.recorder (the bench records its runs too): this name is that module itself, so a monkeypatch
on either name reaches the same code; the star import only gives the type checker its names."""

import sys

from bg3harness import recorder
from bg3harness.recorder import *  # noqa: F403

sys.modules[__name__] = recorder

"""Moved to bg3harness.preflight (bg3harness.doctor runs the same machine checks): this name is that module itself,
so a mock.patch on either name reaches the same code; the star import only gives the type checker its names (and
brings the function `preflight`, hence the alias)."""

import sys

from bg3harness import preflight as _module
from bg3harness.preflight import *

sys.modules[__name__] = _module

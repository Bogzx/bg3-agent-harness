"""The dev scripts' S: one namespace over bg3ai.server and its tool modules (bg3ai/tools/), as S was when every tool
lived in server.py. A read finds the first module (in load order) that has the name; an assignment goes to that
module, so every caller sees it. Import it after bridge/src is on sys.path."""

from __future__ import annotations

from types import ModuleType
from typing import Any

from bg3ai import server, tools


class ServerView:
    def __init__(self, modules: tuple[ModuleType, ...]) -> None:
        object.__setattr__(self, "_modules", modules)

    def _owner(self, name: str) -> ModuleType:
        for m in self._modules:
            if name in vars(m):
                return m
        raise AttributeError(f"no bg3ai server or tools module has {name!r}")

    def __getattr__(self, name: str) -> Any:
        return getattr(self._owner(name), name)

    def __setattr__(self, name: str, value: Any) -> None:
        setattr(self._owner(name), name, value)


S = ServerView((*tools.MODULES, server))

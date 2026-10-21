"""TEMPORARY (remove in Phase D: the harness's game facts replace it): state.status straight from the mod's RPC
port through the bridge's Game client, as runner/dashboard/serve.py game_loop does. Needs the bridge package
(bg3ai) importable; any failure is "no status" and the next call reconnects."""

from __future__ import annotations

import contextlib
from collections.abc import Callable
from typing import Any


def bridge_game(port: int) -> Any:
    from bg3ai.game import Game  # optional dependency: imported only when the adapter is used

    return Game(port=port)


class BridgeStatus:
    def __init__(self, port: int = 9998, factory: Callable[[int], Any] = bridge_game):
        self.port, self.factory = port, factory
        self.game: Any = None

    def status(self) -> dict | None:
        try:
            if self.game is None:
                self.game = self.factory(self.port)
            st = self.game.rpc("state.status", context="server", timeout=6.0)
        except Exception:  # noqa: BLE001 - the game is down, loading or restarting: no sample this time
            self.close()
            return None
        return st if isinstance(st, dict) else None

    def close(self) -> None:
        if self.game is not None:
            with contextlib.suppress(Exception):
                self.game.close()
            self.game = None

# pyright: basic
"""TEMPORARY (remove in Phase D: the harness's game facts replace it): state.status straight from the mod's RPC
port through the bridge's Game client, as the retired dashboard's game loop did. Needs the bridge package
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
        except Exception:
            self.close()
            return None
        return st if isinstance(st, dict) else None

    def close(self) -> None:
        if self.game is not None:
            with contextlib.suppress(Exception):
                self.game.close()
            self.game = None


def bridge_capture(
    shots: Any, max_width: int = 1280
) -> tuple[Callable[[], tuple[bytes, str]] | None, str | None, str | None]:
    """(capture, note, error) for game.ScreenWatch through bg3ai.screen.ScreenCapture, as the retired dashboard
    captured: small JPEGs with Pillow, else full-size PNGs. capture is None when the bridge is missing."""
    try:
        from bg3ai.screen import ScreenCapture  # optional dependency, as above
    except Exception as e:
        return None, None, f"screen capture unavailable: {e}"
    sc = ScreenCapture(shots)
    try:
        import PIL  # noqa: F401
    except ImportError:

        def png() -> tuple[bytes, str]:
            path = sc.capture_png()
            data = path.read_bytes()
            path.unlink(missing_ok=True)
            return data, "image/png"

        return png, "Pillow missing: full-size PNGs (run via `uv run --project bridge`)", None

    def jpeg() -> tuple[bytes, str]:
        jpg, _ = sc.capture(max_width=max_width, quality=70)
        return jpg, "image/jpeg"

    return jpeg, None, None

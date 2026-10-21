import sys
import types
from pathlib import Path
from typing import Any
from unittest import mock

from bg3play.adapters import bridge_status
from bg3play.adapters.bridge_status import BridgeStatus, bridge_capture


class FakeGame:
    def __init__(self, replies: list[Any]):
        self.replies, self.closed = replies, 0

    def rpc(self, op: str, context: str, timeout: float) -> Any:
        assert (op, context) == ("state.status", "server")
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    def close(self) -> None:
        self.closed += 1
        raise OSError("already gone")  # close trouble is swallowed


def test_status_reconnects_after_a_failure():
    games = [FakeGame([{"region": "A"}, ConnectionError("down")]), FakeGame([["not", "a", "dict"]])]
    made: list[int] = []

    def factory(port: int) -> FakeGame:
        made.append(port)
        return games[len(made) - 1]

    bs = BridgeStatus(1234, factory)
    assert bs.status() == {"region": "A"}
    assert bs.status() is None  # the rpc failed: closed, and the next call reconnects
    assert games[0].closed == 1 and bs.game is None
    assert bs.status() is None  # not an object
    assert made == [1234, 1234]
    bs.close()
    assert bs.game is None


def test_status_when_the_bridge_cannot_connect():
    def factory(port: int) -> Any:
        raise ConnectionRefusedError(port)

    assert BridgeStatus(1, factory).status() is None


def test_bridge_game_builds_the_bridge_client():
    game_mod = types.ModuleType("bg3ai.game")
    game_mod.Game = lambda port: ("game", port)  # type: ignore[attr-defined]
    with mock.patch.dict(sys.modules, {"bg3ai.game": game_mod}):
        assert bridge_status.bridge_game(77) == ("game", 77)


class FakeCapture:
    def __init__(self, shots: Path):
        self.shots = shots

    def capture(self, max_width: int, quality: int) -> tuple[bytes, Path]:
        return f"jpg {max_width} {quality}".encode(), self.shots / "x.jpg"

    def capture_png(self) -> Path:
        p = self.shots / "x.png"
        p.write_bytes(b"png")
        return p


def screen_module() -> types.ModuleType:
    m = types.ModuleType("bg3ai.screen")
    m.ScreenCapture = FakeCapture  # type: ignore[attr-defined]
    return m


def test_capture_jpegs_with_pillow(tmp_path: Path):
    with mock.patch.dict(sys.modules, {"bg3ai.screen": screen_module(), "PIL": types.ModuleType("PIL")}):
        capture, note, err = bridge_capture(tmp_path, max_width=640)
    assert (note, err) == (None, None)
    assert capture is not None and capture() == (b"jpg 640 70", "image/jpeg")


def test_capture_pngs_without_pillow(tmp_path: Path):
    with mock.patch.dict(sys.modules, {"bg3ai.screen": screen_module(), "PIL": None}):
        capture, note, err = bridge_capture(tmp_path)
    assert err is None and note is not None and "Pillow missing" in note
    assert capture is not None and capture() == (b"png", "image/png")
    assert not (tmp_path / "x.png").exists()  # the temporary PNG is removed


def test_capture_without_the_bridge(tmp_path: Path):
    with mock.patch.dict(sys.modules, {"bg3ai.screen": None}):
        capture, note, err = bridge_capture(tmp_path)
    assert capture is None and note is None
    assert err is not None and err.startswith("screen capture unavailable:")

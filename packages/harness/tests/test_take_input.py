# pyright: basic
"""gio.take_input: the game's mouse is the harness's from a run's start (live window 7: the operator's real pointer on
the game window's bottom row edge-scrolled the arena's camera, the shim not in agent mode, its cursor at (0, 0))."""

from __future__ import annotations

import os
from typing import Any, ClassVar

import bg3ai.input
import pytest
from bg3harness.game import io as gio
from bg3harness.testing.profile import DEAD_SHIM, SHIM_VAR, temp_profile


class FakeShim:
    """bg3ai.input.InputClient as the shim answers: the commands it got, the window it reports."""

    made: ClassVar[list[FakeShim]] = []
    size = (1920, 1080)

    def __init__(self, socket_name: str = bg3ai.input.DEFAULT_SOCKET, timeout: float = 5.0) -> None:
        self.socket_name = socket_name
        self.sent: list[str] = []
        FakeShim.made.append(self)

    def __enter__(self) -> FakeShim:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.sent.append("close")

    def agent(self, on: bool = True) -> None:
        self.sent.append(f"agent {'on' if on else 'off'}")

    def window_size(self) -> tuple[int, int]:
        return self.size

    def move(self, x: int, y: int, sync: bool = True) -> None:
        self.sent.append(f"move {x} {y}")


@pytest.fixture
def shim(monkeypatch: pytest.MonkeyPatch) -> type[FakeShim]:
    FakeShim.made = []
    monkeypatch.setattr(bg3ai.input, "InputClient", FakeShim)
    return FakeShim


def test_agent_mode_on_and_the_cursor_at_the_windows_centre(shim, monkeypatch):
    monkeypatch.delenv(SHIM_VAR, raising=False)
    assert gio.take_input() == "input shim: agent mode on, cursor at the window's centre (960, 540)"
    (c,) = shim.made
    assert c.socket_name == "bg3ai-input"  # the live game's shim
    assert c.sent == ["agent on", "move 960 540", "close"]


def test_no_shim_is_logged_and_never_raises(shim, monkeypatch):
    def gone(self: FakeShim) -> tuple[int, int]:
        raise bg3ai.input.InputError("the shim has not seen the game window yet")

    monkeypatch.setattr(FakeShim, "window_size", gone)
    out = gio.take_input()
    assert out == "input shim: agent mode or cursor not set (InputError: the shim has not seen the game window yet)"


def test_the_test_profile_points_it_at_a_dead_socket(shim):
    env: dict[str, str] = {}
    temp_profile(env)
    assert env[SHIM_VAR] == DEAD_SHIM
    assert os.environ[SHIM_VAR] == DEAD_SHIM  # this run's
    gio.take_input()
    assert shim.made[0].socket_name == DEAD_SHIM

"""Mouse and keyboard injection into the game through the native SDL shim (native/bg3sdl).

The shim (`libbg3sdl.so`, preloaded into the game by runner/launch_game.sh) listens on the abstract unix
socket `\\0bg3ai-input`. It takes one command per line and answers each with one line, `ok ...` or
`err ...`. Events are queued and handed to the game from its own `SDL_PollEvent`, so they reach only the
game regardless of focus or what the user's real pointer is doing. See native/bg3sdl/bg3sdl.c for the
full command list.

Coordinates are **window coordinates** (SDL's mouse event space). They equal screenshot pixels unless the
window is HiDPI-scaled; `from_screenshot()` converts using the sizes the shim reports in `status`.

Mouse commands put the shim in *agent mode*: the game's cursor becomes the virtual one, the user's real
mouse and focus-loss events are kept from the game, and camera warps stay virtual. `agent(False)` hands the
mouse back to the user.
"""

from __future__ import annotations

import json
import socket
import threading
from collections.abc import Iterable

DEFAULT_SOCKET = "bg3ai-input"
BUTTONS = ("L", "R", "M", "X1", "X2")


class InputError(RuntimeError):
    """The shim rejected a command (bad key name, queue full, sync timeout, ...)."""


class InputClient:
    """Client for the bg3sdl control socket. Thread-safe; reconnects once if the game restarted.

        inp = InputClient()
        w, h = inp.window_size()
        inp.click(w // 2, h // 2)          # left click in the middle of the window
        inp.key("F5")                      # press and release (blocks until delivered)
        inp.key("LCTRL+A")                 # chord
        inp.text("Tav")                    # SDL_TEXTINPUT
    """

    def __init__(self, socket_name: str = DEFAULT_SOCKET, timeout: float = 5.0):
        self.socket_name = socket_name
        self.timeout = timeout
        self._sock: socket.socket | None = None
        self._buf = b""
        self._lock = threading.Lock()

    # ---- connection ----

    def _connect(self) -> socket.socket:
        if self._sock is None:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(self.timeout)
            try:
                s.connect("\0" + self.socket_name)
            except OSError as exc:
                s.close()
                raise ConnectionError(
                    f"input shim not reachable on @{self.socket_name} ({exc}); is the game running with "
                    "libbg3sdl.so preloaded and past its first frame?") from exc
            self._sock = s
            self._buf = b""
        return self._sock

    def close(self) -> None:
        with self._lock:
            if self._sock is not None:
                try:
                    self._sock.close()
                finally:
                    self._sock = None

    def __enter__(self) -> InputClient:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _roundtrip(self, line: str, timeout: float) -> str:
        sock = self._connect()
        sock.settimeout(timeout)
        sock.sendall(line.encode("utf-8") + b"\n")
        while b"\n" not in self._buf:
            chunk = sock.recv(4096)
            if not chunk:
                raise ConnectionError("input shim closed the connection")
            self._buf += chunk
        reply, self._buf = self._buf.split(b"\n", 1)
        return reply.decode("utf-8", "replace")

    def command(self, line: str, timeout: float | None = None) -> str:
        """Send one raw command line; return the text after `ok`, raise InputError on `err`."""
        if "\n" in line or "\r" in line:
            raise ValueError("a command is a single line")
        timeout = self.timeout if timeout is None else timeout
        with self._lock:
            try:
                reply = self._roundtrip(line, timeout)
            except (ConnectionError, BrokenPipeError, ConnectionResetError):
                # The game may have restarted since the last call: one fresh attempt.
                if self._sock is not None:
                    self._sock.close()
                    self._sock = None
                reply = self._roundtrip(line, timeout)
            except OSError:
                if self._sock is not None:
                    self._sock.close()
                    self._sock = None
                raise
        if reply.startswith("ok"):
            return reply[2:].strip()
        raise InputError(reply[4:] if reply.startswith("err ") else reply)

    # ---- queries ----

    def ping(self) -> bool:
        return self.command("ping") == "pong"

    def status(self) -> dict:
        """{version, agent, window, pos, size, drawable, cursor, buttons, queue, frames, injected, dropped, warps, hold}"""
        return json.loads(self.command("status"))

    def window_size(self) -> tuple[int, int]:
        """The game window's size in window coordinates (the space all commands use)."""
        w, h = self.status()["size"]
        if not w or not h:
            raise InputError("the shim has not seen the game window yet")
        return w, h

    def from_screenshot(self, x: float, y: float, image_size: tuple[int, int] | None = None) -> tuple[int, int]:
        """Screenshot pixel -> window coordinate. `image_size` defaults to the swapchain (drawable) size."""
        st = self.status()
        ww, wh = st["size"]
        iw, ih = image_size or tuple(st["drawable"])
        if not iw or not ih or not ww or not wh:
            return round(x), round(y)
        return round(x * ww / iw), round(y * wh / ih)

    # ---- control ----

    def agent(self, on: bool = True) -> None:
        self.command(f"agent {'on' if on else 'off'}")

    def sync(self, timeout_ms: int = 5000) -> None:
        """Block until everything queued so far has reached the game."""
        self.command(f"sync {int(timeout_ms)}", timeout=timeout_ms / 1000 + 2)

    def clear(self) -> int:
        """Drop everything still queued; returns how many steps were dropped."""
        return int(self.command("clear").split()[-1])

    def wait_frames(self, frames: int, sync: bool = True) -> None:
        self._queue(f"wait {int(frames)}", sync)

    def _queue(self, line: str, sync: bool) -> None:
        self.command(line)
        if sync:
            self.sync()

    # ---- mouse ----

    @staticmethod
    def _button(button: str) -> str:
        b = button.upper()
        if b not in BUTTONS:
            raise ValueError(f"button must be one of {BUTTONS}, not {button!r}")
        return b

    def move(self, x: int, y: int, sync: bool = True) -> None:
        self._queue(f"move {int(x)} {int(y)}", sync)

    def click(self, x: int, y: int, button: str = "L", count: int = 1, sync: bool = True) -> None:
        """Move to (x, y), let hover settle, then click `count` times (2 = double click)."""
        self._queue(f"click {self._button(button)} {int(x)} {int(y)} {int(count)}", sync)

    def right_click(self, x: int, y: int, sync: bool = True) -> None:
        self.click(x, y, "R", sync=sync)

    def mouse_down(self, button: str = "L", x: int | None = None, y: int | None = None, sync: bool = True) -> None:
        at = f" {int(x)} {int(y)}" if x is not None and y is not None else ""
        self._queue(f"mdown {self._button(button)}{at}", sync)

    def mouse_up(self, button: str = "L", x: int | None = None, y: int | None = None, sync: bool = True) -> None:
        at = f" {int(x)} {int(y)}" if x is not None and y is not None else ""
        self._queue(f"mup {self._button(button)}{at}", sync)

    def drag(self, x1: int, y1: int, x2: int, y2: int, button: str = "L", steps: int = 12) -> None:
        """Press at (x1, y1), move in `steps` increments, release at (x2, y2)."""
        b = self._button(button)
        self.command(f"mdown {b} {int(x1)} {int(y1)}")
        for i in range(1, steps + 1):
            self.command(f"move {round(x1 + (x2 - x1) * i / steps)} {round(y1 + (y2 - y1) * i / steps)}")
        self.command(f"mup {b} {int(x2)} {int(y2)}")
        self.sync()

    def wheel(self, notches: int, x: int | None = None, y: int | None = None, sync: bool = True) -> None:
        """Positive scrolls up/away (zoom in on the camera), negative down/toward."""
        if notches == 0:
            return
        at = f" {int(x)} {int(y)}" if x is not None and y is not None else ""
        self._queue(f"wheel {int(notches)}{at}", sync)

    # ---- keyboard ----

    def key(self, keys: str | Iterable[str], sync: bool = True) -> None:
        """Press and release a key or chord: "F5", "Escape", "LCTRL+S", ["LSHIFT", "A"].

        Names are SDL scancode names (case-insensitive), plus aliases LCTRL/RCTRL/CTRL, LSHIFT/SHIFT,
        LALT/ALT, ESC, ENTER, SPACE, PGUP/PGDN, DEL, INS, BACKSPACE.
        """
        combo = keys if isinstance(keys, str) else "+".join(keys)
        if not combo or any(c.isspace() for c in combo):
            raise ValueError(f"bad key name {combo!r}")
        self._queue(f"key {combo}", sync)

    def key_down(self, key: str, sync: bool = True) -> None:
        self._queue(f"down {key}", sync)

    def key_up(self, key: str, sync: bool = True) -> None:
        self._queue(f"up {key}", sync)

    def text(self, text: str, sync: bool = True) -> None:
        """Type text through SDL_TEXTINPUT (only reaches text fields with input active). One line only."""
        if not text:
            return
        if "\n" in text or "\r" in text:
            raise ValueError("text() takes a single line; send key('Return') for Enter")
        self._queue(f"text {text}", sync)


def main(argv: list[str] | None = None) -> int:
    """`python -m bg3ai.input "status"`: send raw shim commands, one per argument."""
    import argparse
    import sys

    parser = argparse.ArgumentParser(prog="python -m bg3ai.input", description=__doc__.splitlines()[0])
    parser.add_argument("commands", nargs="+", help='raw commands, e.g. "status" "click L 640 360" "sync"')
    parser.add_argument("--socket", default=DEFAULT_SOCKET)
    args = parser.parse_args(argv)
    try:
        with InputClient(args.socket) as client:
            for cmd in args.commands:
                print(client.command(cmd) or "ok")
    except (InputError, ConnectionError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

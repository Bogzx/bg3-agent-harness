"""Frame capture through Mesa's VK_LAYER_MESA_screenshot.

The game is launched with the layer enabled and a control socket (see
`launch_env`). The layer listens on an abstract unix socket; writing
`:capture=<file>.png;` makes it dump the next presented swapchain image to
`<output_dir>/<file>.png`. This captures exactly what the player sees,
UI included, independent of the compositor and of window focus.

The layer serves ONE control client at a time, and several processes want frames (the MCP server and
the human dashboard). So every capture takes an exclusive file lock (`<output_dir>/.lock`), connects,
captures, and disconnects before releasing the lock. The lock is held for roughly one frame plus the
PNG write.
"""

from __future__ import annotations

import contextlib
import fcntl
import io
import os
import socket
import threading
import time
from pathlib import Path
from typing import Iterator

DEFAULT_SOCKET = "bg3ai-shot"
LOCK_NAME = ".lock"


@contextlib.contextmanager
def shot_lock(path: Path, timeout: float = 10.0) -> Iterator[None]:
    """Exclusive inter-process lock around one capture (flock; released on close even if we crash)."""
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() > deadline:
                    raise TimeoutError("another process is capturing the screen; try again") from None
                time.sleep(0.05)
        yield
    finally:
        os.close(fd)  # closing the descriptor releases the flock


def launch_env(output_dir: Path, socket_name: str = DEFAULT_SOCKET) -> dict[str, str]:
    """Environment variables that enable the capture layer for the game process."""
    return {
        "VK_LOADER_LAYERS_ENABLE": "VK_LAYER_MESA_screenshot",
        "VK_LAYER_MESA_SCREENSHOT_CONFIG": f"comms;control={socket_name};output_dir={output_dir};log_type=error",
    }


class ScreenCapture:
    def __init__(self, output_dir: Path, socket_name: str = DEFAULT_SOCKET):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.socket_name = socket_name
        self._sock: socket.socket | None = None
        self._lock = threading.Lock()
        self._counter = 0
        self.lock_path = self.output_dir / LOCK_NAME

    def _connect(self) -> socket.socket:
        if self._sock is None:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(2.0)
            # The layer accepts one client; it only polls on present, so the
            # handshake arrives after the next frame.
            s.connect("\0" + self.socket_name)
            try:
                s.recv(4096)
            except TimeoutError:
                pass
            self._sock = s
        return self._sock

    def _drop(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            finally:
                self._sock = None

    def capture_png(self, timeout: float = 5.0) -> Path:
        """Ask the layer for the next frame and return the PNG path."""
        with self._lock, shot_lock(self.lock_path):
            self._counter += 1
            name = f"shot_{os.getpid()}_{self._counter:06d}.png"
            target = self.output_dir / name
            try:
                for attempt in range(2):
                    try:
                        self._connect().sendall(f":capture={name};".encode())
                        break
                    except OSError:
                        self._drop()
                        if attempt:
                            raise RuntimeError(
                                "screenshot layer not reachable; is the game running with the capture layer enabled?"
                            )
                deadline = time.monotonic() + timeout
                while time.monotonic() < deadline:
                    # The layer writes <name>.tmp and renames when complete.
                    if target.exists():
                        return target
                    time.sleep(0.02)
                raise TimeoutError("game did not present a frame in time (minimized or loading?)")
            finally:
                # Never keep the layer's single control slot between captures.
                self._drop()

    def capture(self, max_width: int = 1280, quality: int = 80, keep: bool = False) -> tuple[bytes, "Image.Image"]:
        """Capture and return (jpeg_bytes, full_res_image)."""
        from PIL import Image

        path = self.capture_png()
        img = Image.open(path).convert("RGB")
        img.load()
        if not keep:
            path.unlink(missing_ok=True)
        small = img
        if img.width > max_width:
            h = round(img.height * max_width / img.width)
            small = img.resize((max_width, h), Image.LANCZOS)
        buf = io.BytesIO()
        small.save(buf, "JPEG", quality=quality)
        return buf.getvalue(), img

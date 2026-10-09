"""Movie skipper: sends ESCAPE through the SDL input shim when the mod's AutoSkip asks for it.

Cutscenes are always skipped (user requirement). Bink movies only react to real input, so the mod
(Client/AutoSkip.lua) detects a movie on its tick (every 100 ms), presses the engine's ESCAPE, and if
the movie is still up asks for a shim key: `autoskip.status` then carries `want = {seq, key, ...}`.
This loop polls that cheap op (no UI reads in the game) every `fast_s` and sends each new request
once, after checking that the movie is still showing in the same reply. A movie is gone within
about half a second.

Load-time movies (the New Game intro plays during StartLoading) are requested by the mod every
LOAD_RETRY_MS (2 s) for as long as they last; this loop just honours each request.

Fallback, for a mod without `autoskip.status` (or AutoSkip switched off): the old `ui.state` poll
every `fallback_s`, sending ESCAPE when two polls in a row report screen "movie" (ui.state reports
"movie" over a loading screen too).

One skipper per game: every process that runs one (the MCP server, runner/play.py, the new-game test)
takes a flock on runtime/skipper.lock first and idles while another process holds it, so a movie
never gets two ESCAPEs (the second would open the game menu). `start_background()` starts one.
"""

from __future__ import annotations

import fcntl
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable

from .game import GameError, NotReady

DEFAULT_LOCK = Path(os.environ.get("BG3AI_RUNTIME", Path(__file__).resolve().parents[3] / "runtime")) / "skipper.lock"


class MovieSkipper:
    def __init__(self, game: Any, inp: Callable[[], Any], log: Callable[[str], None] = lambda m: None,
                 fast_s: float = 0.15, fallback_s: float = 2.0, lock_path: str | os.PathLike | None = None,
                 lock_retry_s: float = 2.0):
        self.game = game          # its own Game connection (rpc(op, args, context=, timeout=))
        self.inp = inp            # -> InputClient or None
        self.log = log
        self.fast_s = fast_s
        self.fallback_s = fallback_s
        self.sent_seq: int | None = None
        self.fallback_seen = 0
        self.mode = "fast"        # "fast" (autoskip.status) or "fallback" (ui.state)
        self.next_fast_probe = 0.0
        self.sent = 0
        self.lock_path = lock_path  # None: no cross-process lock (tests, single process)
        self.lock_retry_s = lock_retry_s
        self._lock_file = None

    def acquire(self) -> bool:
        """True once this process holds the skipper lock (or no lock is configured)."""
        if self.lock_path is None or self._lock_file is not None:
            return True
        try:
            Path(self.lock_path).parent.mkdir(parents=True, exist_ok=True)
            f = open(self.lock_path, "a+")
        except OSError:
            return True  # can't lock (read-only runtime dir): better two skippers than none
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            f.close()
            return False
        f.seek(0)
        f.truncate()
        f.write(str(os.getpid()))
        f.flush()
        self._lock_file = f
        self.log(f"movie skipper active (pid {os.getpid()})")
        return True

    def release(self) -> None:
        if self._lock_file is not None:
            try:
                fcntl.flock(self._lock_file, fcntl.LOCK_UN)
                self._lock_file.close()
            except OSError:
                pass
            self._lock_file = None

    def _press(self, reason: str) -> bool:
        inp = self.inp()
        if inp is None:
            return False
        try:
            inp.key("ESCAPE")
        except Exception:
            return False
        self.sent += 1
        self.log(reason)
        return True

    def step(self) -> float:
        """One poll; returns the seconds to wait before the next."""
        now = time.monotonic()
        if self.mode == "fallback" and now >= self.next_fast_probe:
            self.mode = "fast"  # retry the fast path now and then (the mod may have been hot-reloaded)
        if self.mode == "fast":
            try:
                st = self.game.rpc("autoskip.status", context="client", timeout=2.0)
            except NotReady:
                return 1.0  # no mod (loading, main menu before the client state): back off a little
            except GameError as e:
                if "unknown op" not in str(e):
                    return self.fast_s
                st = None
            except Exception:
                return 1.0
            if isinstance(st, dict) and st.get("on") is not False:
                want = st.get("want")
                if isinstance(want, dict) and st.get("movie") and want.get("seq") != self.sent_seq:
                    self.sent_seq = want.get("seq")
                    self._press(f"movie skipped (ESCAPE via shim, try {want.get('try')})")
                return self.fast_s
            self.mode = "fallback"
            self.next_fast_probe = now + 30.0
        return self._fallback_step()

    def _fallback_step(self) -> float:
        try:
            st = self.game.rpc("ui.state", context="client", timeout=3.0)
        except Exception:
            self.fallback_seen = 0
            return self.fallback_s
        if isinstance(st, dict) and st.get("screen") == "movie":
            self.fallback_seen += 1
            if self.fallback_seen >= 2:
                self._press("movie skipped (ESCAPE via shim, fallback poll)")
                self.fallback_seen = 0
                return self.fallback_s + 1.0
        else:
            self.fallback_seen = 0
        return self.fallback_s

    def run(self, stop: threading.Event) -> None:
        delay = self.fast_s
        try:
            while not stop.wait(delay):
                delay = self.step() if self.acquire() else self.lock_retry_s
        finally:
            self.release()


def start_background(port: int = 9998, log: Callable[[str], None] = lambda m: None,
                     lock_path: str | os.PathLike | None = DEFAULT_LOCK) -> threading.Event | None:
    """Run a MovieSkipper on its own Game connection in a daemon thread; set the returned event to stop it.

    None when $BG3AI_MOVIE_SKIP=0. Cutscenes are always skipped (user requirement), so every process that
    drives the game outside the MCP server (runner/play.py, tools/dev/test_new_game.py) starts one; the lock
    keeps it to one active skipper per game."""
    if os.environ.get("BG3AI_MOVIE_SKIP", "1") == "0":
        return None
    from .game import Game

    client: dict[str, Any] = {"c": None}

    def inp():
        if client["c"] is None:
            try:
                from .input import InputClient
                c = InputClient()
                c.agent(True)
                client["c"] = c
            except Exception:
                return None
        return client["c"]

    stop = threading.Event()
    sk = MovieSkipper(Game(port=port), inp, log, lock_path=lock_path)
    threading.Thread(target=sk.run, args=(stop,), name="movie-skipper", daemon=True).start()
    return stop

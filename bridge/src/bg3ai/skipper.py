"""Movie skipper: sends ESCAPE through the SDL input shim when the mod's AutoSkip asks for it.

Cutscenes are always skipped (user requirement). Bink movies only react to real input, so the mod
(Client/AutoSkip.lua) detects a movie on its tick (every 100 ms), presses the engine's ESCAPE, and if
the movie is still up asks for a shim key: `autoskip.status` then carries `want = {seq, key, ...}`.
This loop polls that cheap op (no UI reads in the game) every `fast_s` and sends each new request
once, after checking that the movie is still showing in the same reply. A movie is gone within
about half a second.

Fallback, for a mod without `autoskip.status` (or AutoSkip switched off): the old `ui.state` poll
every `fallback_s`, sending ESCAPE when two polls in a row report screen "movie".
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable

from .game import GameError, NotReady


class MovieSkipper:
    def __init__(self, game: Any, inp: Callable[[], Any], log: Callable[[str], None] = lambda m: None,
                 fast_s: float = 0.15, fallback_s: float = 2.0):
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
        while not stop.wait(delay):
            delay = self.step()

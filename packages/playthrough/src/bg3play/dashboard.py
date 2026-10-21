"""What the retired runner/dashboard served, from the playthrough app.

v1 (the Stats and Story screens):
    /api/v1/live                       session clock, live game status + visible events, screen state, thermal,
                                       check_all; counts as a viewer (the game and screen pollers run while watched)
    /api/v1/live/screen                the latest captured frame (204 without one)
    /api/v1/latency, /progress, /goals the running session's latency / story progress and the goal scorecard
    /api/v1/playthroughs/<pt>/story    decisions, chapters, PLAYTHROUGH.md overview, party snapshot

Legacy paths, same payloads as the old dashboard so scripts keep working: /api/state, /api/feed?after=N,
/api/agentlog?after=N&path=ID, /api/screen, /api/latency, /api/progress, /api/goals, /api/story. /series and
/api/series answer that the series view moves to the bench package.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import live, story
from .game import GameWatch, ScreenWatch, Viewers
from .store import RunStore
from .tail import read_new

LIVE_V1 = ("/api/v1/latency", "/api/v1/progress", "/api/v1/goals")
SERIES_MOVED = "The series view moves to the benchmark package (bg3bench); until then run runner/play.py --runs."
SERIES_HTML = b"<!doctype html><meta charset=utf-8><title>BG3 AI Series</title><p>" + SERIES_MOVED.encode() + b"</p>"
RUNNER_KEYS = (
    "session",
    "started",
    "deadline",
    "hard_stop",
    "runner_pid",
    "run_end",
    "updated",
    "claude_session",
    "check_all",
)


class Dashboard:
    def __init__(
        self,
        store: RunStore,
        game: GameWatch | None,
        screen: ScreenWatch | None,
        viewers: Viewers,
        memory_dir: Path | None = None,
        clock: Callable[[], float] = time.time,
    ):
        self.store, self.game, self.screen, self.viewers = store, game, screen, viewers
        self.memory_override, self.clock = memory_dir, clock

    # ------------------------------------------------------------ inputs

    def memory(self) -> Path:
        """--memory-dir, else the runner's playthrough's memory, else play/memory (the old dashboard's order)."""
        if self.memory_override is not None:
            return self.memory_override
        pt = self.store.current()
        if pt and self.store.memory_dir(pt).is_dir():
            return self.store.memory_dir(pt)
        return self.store.playthroughs_dir.parent / "play" / "memory"

    def _game(self) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        if self.game is None:
            return {"reachable": False, "error": "game polling is off (start the server with --game)"}, []
        return self.game.snapshot()

    def _screen(self) -> tuple[dict[str, Any], bytes | None]:
        if self.screen is None:
            return {"enabled": False, "error": None, "t": None, "mime": None}, None
        return self.screen.snapshot()

    # ------------------------------------------------------------ payloads

    def state(self) -> dict[str, Any]:
        """/api/state (legacy) and the base of /api/v1/live."""
        self.viewers.seen()
        rt = self.store.runtime
        runner = self.store.status()
        mem = self.memory()
        game, events = self._game()
        screen, _ = self._screen()
        log = live.resolve_agent_log(rt, runner)
        return {
            "now": self.clock(),
            "playthrough": runner.get("playthrough"),
            "runner": {k: runner.get(k) for k in RUNNER_KEYS if k in runner},
            "clock": live.session_clock(
                runner, live.read_json(rt / "session.json"), live.read_json(mem / ".session.json"), now=self.clock()
            ),
            "game": game,
            "events": events,
            "screen": screen,
            "memory": live.memory_view(mem),
            "agent_log": {"path_id": log.name if log else None},
            "thermal": live.thermal_view(rt / "thermal.log"),
        }

    def live(self) -> dict[str, Any]:
        s = self.state()
        return {
            "v": 1,
            **{k: s[k] for k in ("now", "playthrough", "runner", "clock", "game", "events", "screen", "thermal")},
        }

    def stats(self, what: str) -> dict[str, Any]:
        runner, rt = self.store.status(), self.store.runtime
        fn = {"latency": live.latency_payload, "progress": live.progress_payload, "goals": live.goals_payload}[what]
        return fn(rt, runner, now=self.clock())

    def story(self, pt: str) -> dict[str, Any]:
        """v1: story_payload with its PLAYTHROUGH.md overview under "overview" ("playthrough" is the name here)."""
        s = dict(story.story_payload(self.store.memory_dir(pt)))
        return {"v": 1, "playthrough": pt, "overview": s.pop("playthrough"), **s}

    def frame(self) -> tuple[bytes | None, str | None]:
        self.viewers.seen()
        view, frame = self._screen()
        return frame, view.get("mime")

    def feed(self, after: int) -> dict[str, Any]:
        off, text, reset = read_new(self.store.actions_path, after)
        return {"offset": off, "reset": reset, "items": live.parse_actions(text)}

    def agentlog(self, after: int, path_id: str | None) -> dict[str, Any]:
        log = live.resolve_agent_log(self.store.runtime, self.store.status())
        if log is None:
            return {"offset": 0, "reset": True, "text": "", "path_id": None}
        if path_id != log.name:
            after = -1  # a new session log: start over near its end
        off, text, reset = read_new(log, after, initial_bytes=32 * 1024)
        return {"offset": off, "reset": reset, "text": text, "path_id": log.name}

    # ------------------------------------------------------------ routing

    def route(self, path: str, q: dict[str, list[str]]) -> dict[str, Any] | None:
        """A JSON path (v1 live or legacy) -> its payload; None when it isn't one of these."""
        if path == "/api/v1/live":
            return self.live()
        if path in LIVE_V1:
            return {"v": 1, **self.stats(path.rsplit("/", 1)[1])}
        if path == "/api/state":
            return self.state()
        if path in ("/api/latency", "/api/progress", "/api/goals"):
            return self.stats(path.rsplit("/", 1)[1])
        if path == "/api/story":
            return story.story_payload(self.memory())
        if path == "/api/feed":
            return self.feed(_int(q, "after"))
        if path == "/api/agentlog":
            return self.agentlog(_int(q, "after"), q.get("path", [None])[0])
        if path == "/api/series":
            return {"series": None, "rows": [], "live": None, "error": SERIES_MOVED}
        return None


def _int(q: dict[str, list[str]], key: str, default: int = -1) -> int:
    try:
        return int(q.get(key, [str(default)])[0])
    except ValueError:
        return default

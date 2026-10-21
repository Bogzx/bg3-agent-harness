from bg3play.game import VIEWER_IDLE_S, GameWatch, ScreenWatch, Viewers

STATUS = {
    "region": "WLD_Main_A",
    "gold": 12,
    "in_combat": False,
    "active": "u-1",
    "party": [{"uuid": "u-1", "name": "Tav", "hp": 10, "max_hp": 12, "level": 2}],
}


class FakeGame:
    def __init__(self, ops=("state.status", "events.since"), fail: str | None = None):
        self.ops, self.fail, self.calls, self.closed = list(ops), fail, [], False
        self.events: list[dict] = []
        self.latest = 7

    def rpc(self, op, params=None, context="server", timeout=5.0):
        self.calls.append((op, params))
        if op == self.fail:
            raise ConnectionError("game went away")
        if op == "sys.info":
            return {"ops": self.ops}
        if op == "state.status":
            return STATUS
        if op == "events.since":
            evs = [e for e in self.events if e["seq"] > params["seq"]][: params["limit"]]
            return {"events": evs, "latest": self.latest}
        raise AssertionError(op)

    def close(self):
        self.closed = True


class Clock:
    def __init__(self, t: float = 1000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t


def test_no_viewer_no_rpc():
    games = []
    w = GameWatch(lambda: games.append(FakeGame()) or games[-1], Viewers(Clock()))
    assert w.poll(1000.0) == 1.0
    assert games == []
    assert w.snapshot()[0] == {"reachable": False, "error": "not polled yet"}


def test_status_shaped_and_events_from_now_on():
    clock = Clock()
    viewers = Viewers(clock)
    viewers.seen()
    game = FakeGame()
    game.events = [{"seq": 5, "t": 1, "kind": "old", "data": {}}]  # before the first look: skipped
    w = GameWatch(lambda: game, viewers, interval=3.0)
    assert w.poll(clock()) == 3.0
    view, events = w.snapshot()
    assert view["reachable"] and view["status"]["party"][0]["name"] == "Tav"
    assert view["status"]["party"][0]["active"] is True
    assert "uuid" not in view["status"]["party"][0]
    assert events == []
    game.events.append({"seq": 8, "t": 2, "kind": "LevelUp", "data": {"who": "Tav", "uuid": "x"}})
    game.latest = 8
    w.poll(clock())
    assert w.snapshot()[1] == [{"t": 2, "kind": "LevelUp", "text": "LevelUp who=Tav"}]
    w.poll(clock())
    assert len(w.snapshot()[1]) == 1  # not repeated


def test_ring_reset_after_reload_and_missing_ops():
    clock = Clock()
    viewers = Viewers(clock)
    viewers.seen()
    game = FakeGame()
    w = GameWatch(lambda: game, viewers)
    w.poll(clock())
    game.latest = 2  # the mod's ring restarted below our cursor
    w.poll(clock())
    assert w.event_seq == 2
    bare = FakeGame(ops=())
    w2 = GameWatch(lambda: bare, viewers)
    w2.poll(clock())
    view, _ = w2.snapshot()
    assert view["status"] is None and view["note"] == "state.status not available yet"


def test_failure_reconnects_with_backoff():
    clock = Clock()
    viewers = Viewers(clock)
    viewers.seen()
    made = []

    def connect():
        made.append(FakeGame(fail="state.status" if not made else None))
        return made[-1]

    w = GameWatch(connect, viewers, interval=3.0)
    assert w.poll(clock()) == 10.0
    view, _ = w.snapshot()
    assert not view["reachable"] and "game went away" in view["error"]
    assert made[0].closed
    assert w.poll(clock()) == 3.0
    assert len(made) == 2 and w.snapshot()[0]["reachable"]


def test_viewer_goes_idle():
    clock = Clock()
    viewers = Viewers(clock)
    viewers.seen()
    assert viewers.watching(clock() + VIEWER_IDLE_S - 1)
    assert not viewers.watching(clock() + VIEWER_IDLE_S)


def test_screen_frames_only_while_watched():
    clock = Clock()
    viewers = Viewers(clock)
    shots = []

    def capture():
        shots.append(1)
        if len(shots) == 2:
            raise OSError("slot busy")
        return b"jpg", "image/jpeg"

    s = ScreenWatch(capture, viewers, interval=5.0)
    assert s.poll(clock()) == 1.0 and shots == []
    viewers.seen()
    assert s.poll(clock()) == 5.0
    view, frame = s.snapshot()
    assert frame == b"jpg" and view["mime"] == "image/jpeg" and view["enabled"]
    assert s.poll(clock()) == 15.0
    view, frame = s.snapshot()
    assert "slot busy" in view["error"] and frame == b"jpg"


def test_screen_disabled():
    s = ScreenWatch(None, Viewers(Clock()), error="screen capture unavailable: no bridge")
    assert s.poll(0.0) == 1.0
    view, frame = s.snapshot()
    assert (view["enabled"], view["error"], frame) == (False, "screen capture unavailable: no bridge", None)

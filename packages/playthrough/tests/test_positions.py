import json
from pathlib import Path

from bg3play import api, positions
from bg3play.adapters.bridge_status import BridgeStatus
from bg3play.store import RunStore

from .conftest import PT


def status(level="WLD_Main_A", pos=(225.7, 26.3, 382.2), **kw) -> dict:
    party = [
        {"name": "Gale", "uuid": "g", "pos": list(pos), "region": level},
        {"name": "Tav", "uuid": "t", "pos": [pos[0] + 1, pos[1], pos[2]], "region": level},
    ]
    return {"level": level, "region": "Wilderness", "active": "t", "party": party, **kw}


def test_snapshot_shape_and_filters():
    row = positions.snapshot(100.0, status())
    assert row == {
        "v": 1,
        "t": 100.0,
        "level": "WLD_Main_A",
        "region": "Wilderness",
        "party": [
            {"name": "Gale", "x": 225.7, "y": 26.3, "z": 382.2},
            {"name": "Tav", "x": 226.7, "y": 26.3, "z": 382.2, "active": True},
        ],
    }
    st = status()
    st["party"][0]["region"] = "CRE_Main_A"  # another level: other coordinate space, left out
    st["party"].append({"name": "Karlach", "pos": None})
    assert [m["name"] for m in positions.snapshot(1.0, st)["party"]] == ["Tav"]
    assert positions.snapshot(1.0, {"level": None, "party": status()["party"]}) is None
    assert positions.snapshot(1.0, status() | {"party": [{"name": "x", "pos": [1, 2]}]}) is None


def test_keep_policy():
    a = positions.snapshot(100.0, status())
    assert positions.keep(None, a)
    assert not positions.keep(a, positions.snapshot(102.0, status(pos=(225.9, 26.3, 382.2))))  # 0.2 m
    assert positions.keep(a, positions.snapshot(102.0, status(pos=(226.3, 26.3, 382.2))))  # 0.6 m
    assert positions.keep(a, positions.snapshot(130.0, status()))  # heartbeat
    assert positions.keep(a, positions.snapshot(101.0, status(level="CRE_Main_A")))  # level change
    b = positions.snapshot(101.0, status())
    b["party"] = b["party"][:1]
    assert positions.keep(a, b)  # party changed


def rows(*specs: tuple[float, str, float]) -> list[dict]:
    return [positions.snapshot(t, status(level=lv, pos=(x, 0.0, -x))) for t, lv, x in specs]


def test_tracks_split_at_gaps_and_levels():
    rs = rows((10, "A", 0), (12, "A", 1), (20, "B", 5), (22, "B", 6), (30, "A", 2), (500, "A", 3), (502, "A", 4))
    tr = positions.tracks(rs)
    assert tr["level"] == "A" and [lv["level"] for lv in tr["levels"]] == ["A", "B"]
    assert tr["levels"][0]["samples"] == 5 and tr["levels"][1] == {
        "level": "B",
        "region": "Wilderness",
        "samples": 2,
        "first_t": 20,
        "last_t": 22,
    }
    # lead = the active member (Tav, x + 1); a level visit and a 470 s gap each start a new segment
    assert tr["segments"] == [[[10, 1, 0], [12, 2, -1]], [[30, 3, -2]], [[500, 4, -3], [502, 5, -4]]]
    assert tr["party"] == [
        {"name": "Gale", "x": 4, "z": -4, "active": False},
        {"name": "Tav", "x": 5, "z": -4, "active": True},
    ]
    assert tr["t"] == 502 and tr["bounds"] == {"min_x": 1, "max_x": 5, "min_z": -4, "max_z": 0}
    b = positions.tracks(rs, "B")
    assert b["segments"] == [[[20, 6, -5], [22, 7, -6]]] and b["t"] == 22
    assert positions.tracks([], None) == {
        "level": None,
        "levels": [],
        "segments": [],
        "party": [],
        "t": None,
        "bounds": None,
    }
    assert positions.tracks(rs, "nope")["segments"] == []


def test_tracks_downsample_keeps_ends():
    rs = rows(*[(float(i), "A", float(i)) for i in range(101)])
    tr = positions.tracks(rs, max_points=10)
    seg = tr["segments"][0]
    assert len(seg) <= 12 and seg[0][0] == 0 and seg[-1][0] == 100


def test_read_skips_bad_lines(tmp_path: Path):
    f = tmp_path / "positions.jsonl"
    good = json.dumps(positions.snapshot(1.0, status()))
    f.write_text(f'{good}\nnot json\n{{"t": 1}}\n[1]\n{good}\n')
    assert len(positions.read(f)) == 2 and positions.read(tmp_path / "missing") == []


class Source:
    def __init__(self):
        self.st: dict | None = None

    def status(self) -> dict | None:
        return self.st


def test_sampler_appends_kept_rows_and_resumes(tmp_path: Path):
    src, clock, seen = Source(), {"t": 100.0}, []
    target = {"p": None}
    s = positions.Sampler(src, lambda: target["p"], lambda: clock["t"], seen.append)
    assert s.poll() is None  # no playthrough
    target["p"] = tmp_path / "pt" / "positions.jsonl"
    assert s.poll() is None  # no status
    src.st = status()
    assert s.poll()["t"] == 100.0
    clock["t"] = 102.0
    assert s.poll() is None  # didn't move
    src.st = status(pos=(230.0, 26.3, 382.2))
    assert s.poll()["t"] == 102.0
    assert [r["t"] for r in positions.read(target["p"])] == [100.0, 102.0] and len(seen) == 2
    # a new sampler (app restart) continues from the file's last row
    s2 = positions.Sampler(src, lambda: target["p"], lambda: 104.0)
    assert s2.poll() is None


def test_bridge_status_adapter_reconnects():
    calls = []

    class Game:
        def __init__(self, port):
            calls.append(port)
            self.fail = len(calls) == 1

        def rpc(self, op, context, timeout):
            assert op == "state.status" and context == "server"
            if self.fail:
                raise ConnectionError("down")
            return {"level": "A"}

        def close(self):
            calls.append("closed")

    b = BridgeStatus(9998, Game)
    assert b.status() is None and calls == [9998, "closed"]
    assert b.status() == {"level": "A"} and calls == [9998, "closed", 9998]


def test_positions_api_on_the_fixture(store: RunStore):
    p = api.positions(store, PT)
    assert p["v"] == 1 and p["level"] == "EPI_Main_A"
    assert [lv["level"] for lv in p["levels"]] == ["END_Main", "EPI_Main_A"]
    end = api.positions(store, PT, "END_Main")
    assert len(end["segments"]) == 2  # the 300 s gap splits the walk
    assert all(m["name"] for m in end["party"]) and end["bounds"]["min_x"] < end["bounds"]["max_x"]

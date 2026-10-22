"""stress_io's save copy on a tmp Story folder, and stress_exec.wait_loaded (the probe's wait for the launch's
save) on a fake clock."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
import stress_exec as ex
from stress_io import DirSaves

pytestmark = pytest.mark.unit


def save(root: Path, folder: str, label: str, data: bytes = b"lsv bytes") -> Path:
    d = root / folder
    d.mkdir()
    (d / f"{label}.lsv").write_bytes(data)
    (d / f"{label}.WebP").write_bytes(b"img")
    return d


def test_the_copy_names_its_lsv_and_webp_after_its_own_label(tmp_path):
    src = save(tmp_path, "Claude-12__S025-end", "S025-end")
    name, owner = DirSaves(tmp_path).copy("S025-end", "SCN-stress_c30")
    assert (name, owner) == ("Claude-12__SCN-stress_c30", "Claude")
    assert sorted(p.name for p in (tmp_path / name).iterdir()) == ["SCN-stress_c30.WebP", "SCN-stress_c30.lsv"]
    assert (tmp_path / name / "SCN-stress_c30.lsv").read_bytes() == b"lsv bytes"
    assert sorted(p.name for p in src.iterdir()) == ["S025-end.WebP", "S025-end.lsv"]  # the original untouched


def test_an_earlier_copy_with_the_source_names_is_renamed_and_kept(tmp_path):
    save(tmp_path, "Claude-12__S025-end", "S025-end")
    save(tmp_path, "Claude-12__SCN-stress_c30", "S025-end")  # an older tool's copy: same bytes, source names
    name, _ = DirSaves(tmp_path).copy("S025-end", "SCN-stress_c30")
    assert sorted(p.name for p in (tmp_path / name).iterdir()) == ["SCN-stress_c30.WebP", "SCN-stress_c30.lsv"]


def test_a_copy_of_another_start_is_replaced(tmp_path):
    save(tmp_path, "Claude-12__S025-end", "S025-end", b"new")
    save(tmp_path, "Claude-12__SCN-stress_c30", "SCN-stress_c30", b"old")
    DirSaves(tmp_path).copy("S025-end", "SCN-stress_c30")
    assert (tmp_path / "Claude-12__SCN-stress_c30" / "SCN-stress_c30.lsv").read_bytes() == b"new"


class FakeClock:
    def __init__(self):
        self.t = 100.0
        self.slept: list[float] = []

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.t += seconds


def seq[T](first: T, *rest: T) -> Callable[[], T]:
    it = iter((first, *rest))
    last = [first]

    def nxt() -> T:
        last[0] = next(it, last[0])
        return last[0]

    return nxt


def test_the_probe_waits_past_the_menu_and_the_loading_screen_for_the_server_context():
    clock = FakeClock()
    ready = seq(False, False, False, True)  # "the server context runs once a save is loaded"
    screen = seq("main_menu", "loading", "loading", "game")
    assert ex.wait_loaded(ready, screen, clock, timeout_s=60, poll_s=3) == (
        True,
        "save loaded after 9 s (server context up, screen game)",
    )
    assert clock.slept == [3, 3, 3]


def test_a_server_context_still_on_the_loading_screen_is_not_loaded():
    clock = FakeClock()
    ok, why = ex.wait_loaded(lambda: True, lambda: "loading", clock, timeout_s=9, poll_s=3)
    assert not ok and why == "no save loaded after 9 s (server context up, screen loading)"
    assert clock.slept == [3, 3, 3]


def test_the_wait_is_bounded_and_says_what_it_saw():
    ok, why = ex.wait_loaded(lambda: False, lambda: None, FakeClock(), timeout_s=300, poll_s=3)
    assert not ok and why == "no save loaded after 300 s (server context down, screen None)"

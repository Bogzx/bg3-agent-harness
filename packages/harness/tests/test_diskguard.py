from pathlib import Path

from bg3harness import diskguard


def test_the_floor_is_4_gb_plus_16_mb_a_minute_of_the_cap():
    assert diskguard.need_mb(60, {}) == (4096 + 960, 4096, 16)
    assert diskguard.need_mb(7.5, {diskguard.FLOOR_MB_ENV: "1000", diskguard.PER_MIN_ENV: "20"}) == (1150, 1000, 20)


def test_start_refusal(tmp_path: Path):
    assert diskguard.start_refusal(tmp_path, 60, {}, free=lambda p: 5056) is None
    assert diskguard.start_refusal(tmp_path, 60, {}, free=lambda p: 5055) == (
        f"5055 MB free on {tmp_path} < 5056 MB (4096 + 60 min x 16 MB)"
    )
    assert diskguard.start_refusal(tmp_path, 60, {}, free=lambda p: None) is None  # unreadable: not refused
    assert diskguard.free_mb(tmp_path) is not None and diskguard.free_mb(tmp_path / "gone") is None

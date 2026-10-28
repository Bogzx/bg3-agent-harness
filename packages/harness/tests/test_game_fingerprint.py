"""bg3harness.game_fingerprint on a fake game dir in a temp dir (nothing of the real install is read), and the
checkout's recorded fingerprint of the pinned build."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from bg3harness import game_fingerprint as gf
from bg3harness import preflight


def fake_game(root: Path) -> Path:
    for rel, data in (("bin/bg3", b"exe"), ("Data/Game.pak", b"game"), ("Data/GustavX.pak", b"gx"),
                      ("Data/Shared.pak", b"shared"), ("Data/Patch8_HotFix10.pak", b"hf"),
                      ("Data/Textures.pak", b"big asset"), ("Data/Patch8_HotFix9.pak", b"hf9")):  # fmt: skip
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_bytes(data)
    return root


@pytest.fixture
def game(tmp_path: Path) -> Path:
    return fake_game(tmp_path / "game")


def pinned(tmp_path: Path, game: Path, build: str = "25605617") -> Path:
    pins = tmp_path / "fingerprints.json"
    pins.write_text(json.dumps({build: gf.make(game, gf.default_files(game))}))
    return pins


def test_make_takes_the_exe_the_main_paks_and_the_hotfixes_present(game: Path):
    assert gf.default_files(game) == ["bin/bg3", *gf.DATA_PAKS, "Data/Patch8_HotFix10.pak", "Data/Patch8_HotFix9.pak"]
    fp = gf.make(game, gf.default_files(game))
    assert [f["path"] for f in fp["files"]] == sorted(gf.default_files(game))
    assert "Data/Textures.pak" not in [f["path"] for f in fp["files"]]
    assert fp["files"][0]["bytes"] == len(b"game") and fp["sha256"] == gf.digest(fp["files"])


def test_the_digest_is_over_the_sorted_list_of_size_and_sha256(game: Path):
    a = gf.make(game, ["bin/bg3", "Data/Game.pak"])
    b = gf.make(game, ["Data/Game.pak", "bin/bg3"])
    assert a == b
    assert gf.digest(list(reversed(a["files"]))) == a["sha256"]
    other: list[dict[str, Any]] = [{**a["files"][0], "bytes": 5}, a["files"][1]]
    assert gf.digest(other) != a["sha256"]


def test_a_copy_with_the_same_files_matches(tmp_path: Path, game: Path):
    want = gf.make(game, ["bin/bg3", "Data/Game.pak"])
    copy = fake_game(tmp_path / "copy")  # download_depot output elsewhere: same bytes
    assert gf.problems(copy, want) == []


def test_other_bytes_of_the_same_size_differ(game: Path):
    want = gf.make(game, ["bin/bg3", "Data/Game.pak"])
    (game / "Data/Game.pak").write_bytes(b"gam3")
    assert gf.problems(game, want) == ["Data/Game.pak differs (sha256)"]


def test_a_size_difference_is_refused_without_hashing(game: Path, monkeypatch: pytest.MonkeyPatch):
    want = gf.make(game, ["bin/bg3", "Data/Game.pak"])
    (game / "Data/Game.pak").write_bytes(b"a newer, bigger pak")
    (game / "bin/bg3").unlink()

    def no_hash(path: Path) -> str:
        raise AssertionError(f"hashed {path}")

    monkeypatch.setattr(gf, "sha256_file", no_hash)
    assert gf.problems(game, want) == ["Data/Game.pak is 19 bytes, the pinned build's 4", "no bin/bg3"]


def test_a_tampered_record_matches_nothing(game: Path):
    want = gf.make(game, ["bin/bg3"])
    assert gf.problems(game, {**want, "sha256": "0" * 64})[0].startswith("the recorded fingerprint is malformed")
    assert gf.problems(game, {})[0].startswith("the recorded fingerprint is malformed")


def test_installed_build(tmp_path: Path, game: Path):
    pins = pinned(tmp_path, game)
    assert gf.installed_build("25605617", game, "25605617", pins) == ("25605617", "appmanifest")
    assert gf.installed_build("25605617", game, "26000000", pins) == ("25605617", "fingerprint")
    assert gf.installed_build("25605617", game, "", pins) == ("25605617", "fingerprint")
    assert gf.installed_build("25700000", game, "26000000", pins) == ("26000000", "appmanifest")  # no record
    assert gf.installed_build("25605617", game, "26000000", tmp_path / "none.json") == ("26000000", "appmanifest")
    (game / "bin/bg3").write_bytes(b"new")
    assert gf.installed_build("25605617", game, "26000000", pins) == ("26000000", "appmanifest")


def test_cli(tmp_path: Path, game: Path, capsys: pytest.CaptureFixture[str]):
    assert gf.main(["make", "--game", str(game), "--file", "bin/bg3"]) == 0
    assert [f["path"] for f in json.loads(capsys.readouterr().out)["files"]] == ["bin/bg3"]
    pins = pinned(tmp_path, game)
    args = ["check", "--game", str(game), "--pins", str(pins), "--build", "25605617"]
    assert gf.main(args) == 0
    assert "matches build 25605617's fingerprint" in capsys.readouterr().out
    assert gf.main([*args[:-1], "1"]) == 3
    (game / "bin/bg3").write_bytes(b"EXE")
    assert gf.main(args) == 1


def test_the_checkout_records_the_pinned_builds_fingerprint():
    record = gf.load().get(preflight.pinned_build(gf.REPO))
    assert record is not None, f"no fingerprint for the pinned build in {gf.PINS}"
    paths = [f["path"] for f in record["files"]]
    assert gf.EXE in paths and "Data/Game.pak" in paths and paths == sorted(paths)
    assert record["sha256"] == gf.digest(record["files"])

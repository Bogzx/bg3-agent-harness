# pyright: basic
# ruff: noqa: F811  (the tests take test_run_host's rig fixture by name)
"""bg3harness.runner.run_host's pins: the game build, bg3le, mod tree and repo a bench run started with, in its record
(diverse-2026-10-25: a matrix run's manifest pins held only the versions; the legacy series path had them all)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from bg3harness import game_fingerprint
from bg3harness import pins as hpins
from bg3harness.runner import run_host as H  # noqa: N812
from test_run_host import JOB, PINS, SAVE_SC, Rig, outcome, rig, run_dir  # noqa: F401  (rig: fixture)


def test_record_carries_the_pins_the_run_started_with(rig: Rig):  # diverse-2026-10-25: pins held only versions
    reads: list[int] = []

    def read_pins(env: Any) -> dict[str, Any]:
        reads.append(1)
        return {**PINS, "game_build": str(len(reads))}

    rig.h.read_pins = read_pins
    rig.h.start(JOB, SAVE_SC, run_dir("m-grove"))
    read_pins({})  # the game is patched mid-run: the record keeps the start's build
    rec = rig.h.record(JOB, SAVE_SC, run_dir("m-grove"), outcome())
    assert rec["pins"] == {**PINS, "game_build": "1"}
    assert "versions" not in rec["pins"]  # the bench adds them (bg3bench.pins.versions)
    assert rig.h.pins == {}


def test_machine_pins_read_the_game_build_bg3le_the_live_mod_tree_and_the_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    game = tmp_path / "steamapps" / "common" / "Baldurs Gate 3"
    game.mkdir(parents=True)
    (tmp_path / "steamapps" / "appmanifest_1086940.acf").write_text('"AppState"\n{\n\t"buildid"\t\t"25605617"\n}\n')
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "BUILDINFO").write_text("bg3le commit: abc\npatch: 0001-x.patch 1\nsha256: feed\n")
    live = tmp_path / "mod-live"
    (live / "Mods").mkdir(parents=True)
    (live / "Mods" / "x.lua").write_text("return 1")
    monkeypatch.setattr(H.rcode, "MOD_LIVE", live)
    monkeypatch.setattr(H.rcode, "MOD_DIR", tmp_path / "mod")  # no meta.lsx: no version
    monkeypatch.setattr(H.renv, "REPO", tmp_path)  # not a git repository: no commit
    pins = H.machine_pins({"BG3_DIR": str(game), "BG3AI_BG3LE_LIB": str(dist / "libbg3le.so")})
    assert pins["game_build"] == "25605617"
    assert (pins["bg3le"]["sha256"], pins["bg3le"]["commit"], pins["bg3le"]["patches"]) == ("feed", "abc", 1)
    assert pins["mod"] == {
        "version": None,
        "sha1": hpins.tree_digest({"Mods/x.lua": hashlib.sha1(b"return 1").hexdigest()}),
    }
    assert pins["repo"]["commit"] is None
    assert pins["game_build_by"] == "appmanifest"


def test_a_depot_copy_of_the_pinned_build_pins_the_pinned_build(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    # its appmanifest names the build Steam updated to; its files are the pinned build's (download_depot)
    game = tmp_path / "steamapps" / "common" / "Baldurs Gate 3"
    (game / "bin").mkdir(parents=True)
    (game / "bin" / "bg3").write_bytes(b"pinned exe")
    (tmp_path / "steamapps" / "appmanifest_1086940.acf").write_text('"AppState"\n{\n\t"buildid"\t\t"26000000"\n}\n')
    (tmp_path / "tools" / "dev").mkdir(parents=True)
    (tmp_path / "tools" / "dev" / "first_launch.sh").write_text("WANT_BUILD=25605617\n")
    fp = game_fingerprint.make(game, ["bin/bg3"])
    (tmp_path / "tools" / "dev" / "game_fingerprints.json").write_text(json.dumps({"25605617": fp}))
    monkeypatch.setattr(H.rcode, "MOD_LIVE", tmp_path / "mod-live")
    monkeypatch.setattr(H.rcode, "MOD_DIR", tmp_path / "mod")
    monkeypatch.setattr(H.renv, "REPO", tmp_path)
    env = {"BG3_DIR": str(game), "BG3AI_BG3LE_LIB": str(tmp_path / "dist" / "libbg3le.so")}
    pins = H.machine_pins(env)
    assert (pins["game_build"], pins["game_build_by"]) == ("25605617", "fingerprint")
    (game / "bin" / "bg3").write_bytes(b"other  exe")  # same size, other bytes: the appmanifest's build again
    assert (H.machine_pins(env)["game_build"], H.machine_pins(env)["game_build_by"]) == ("26000000", "appmanifest")

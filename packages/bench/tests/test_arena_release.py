"""packages/bench/releases/arena/arena-v0.json (REVIEW-16 A2; its own folder, as releases/*.json are bench suites):
arena v0's release fixes the arena definition, the matrix and the pins its published result played with; an edit to
the arena or the matrix file is a new release."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from bg3bench import suite

ROOT = suite.PACKAGE_ROOT
RELEASE = ROOT / "releases" / "arena" / "arena-v0.json"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_arena_v0s_release_matches_the_arena_and_matrix_files_and_cites_every_pin():
    rel = json.loads(RELEASE.read_text(encoding="utf-8"))
    assert (rel["schema"], rel["kind"], rel["arena"]) == (1, "bg3bench.arena_release", "arena-v0")
    assert set(rel["versions"]) == {"harness", "bench"}
    assert {"game_build", "bg3le_sha256", "mod_sha1", "code_sha1", "commit"} <= set(rel["pins"])
    assert all(isinstance(v, str) and v for v in rel["pins"].values())
    arena_file = ROOT / "arenas" / "arena-v0.json"
    arena = json.loads(arena_file.read_text(encoding="utf-8"))
    entry = rel["arenas"]["arena-v0"]
    assert entry["sha256"] == sha256(arena_file)
    assert (entry["save"], entry["save_sha1"]) == (arena["save"]["name"], arena["save"]["sha1"])
    for name, m in rel["matrices"].items():
        assert m["sha256"] == sha256(ROOT / "matrices" / f"{name}.toml")

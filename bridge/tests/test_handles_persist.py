"""Handles kept in a file: a restarted bridge gives the same ids, so a resumed agent's conversation still means the same
characters (arena, BG3AI_HANDLES_PATH; v011-pilot L12 m1: renumbered ids after the restart)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from bg3ai.render_base import Handles

A = "11111111-1111-1111-1111-111111111111"
B = "22222222-2222-2222-2222-222222222222"
C = "33333333-3333-3333-3333-333333333333"


def test_a_new_process_gives_the_same_ids_and_goes_on_numbering(tmp_path: Path) -> None:
    path = str(tmp_path / "B.handles.json")
    first = Handles(path)
    assert (first.get(A, "Red Ash"), first.get(B, "Blue Dune")) == ("e1", "e2")
    second = Handles(path)
    assert (second.resolve("e1"), second.names["e2"], second.resolve("Red Ash")) == (A, "Blue Dune", A)
    assert second.get(B) == "e2" and second.get(C, "Goblin") == "e3"
    assert Handles(path).get(C) == "e3"


def test_a_renamed_character_is_saved(tmp_path: Path) -> None:
    path = str(tmp_path / "h.json")
    h = Handles(path)
    h.get(A, "Old")
    h.get(A, "New")
    assert Handles(path).names["e1"] == "New"


def test_without_a_path_nothing_is_written_and_a_new_map_starts_at_one(tmp_path: Path) -> None:
    h = Handles()
    h.get(A, "x")
    assert list(tmp_path.iterdir()) == [] and Handles().get(B) == "e1"


def test_no_temporary_file_is_left(tmp_path: Path) -> None:
    path = tmp_path / "h.json"
    Handles(str(path)).get(A, "x")
    assert [p.name for p in tmp_path.iterdir()] == ["h.json"]
    assert json.loads(path.read_text()) == {"handles": {A: "e1"}, "names": {"e1": "x"}}


def test_the_server_reads_its_path_from_the_environment(tmp_path: Path) -> None:
    path = tmp_path / "B.handles.json"
    env = {**os.environ, "BG3AI_RUNTIME": str(tmp_path), "BG3AI_HANDLES_PATH": str(path)}
    code = "from bg3ai.tools import core; print(core.H.path)"
    out = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, check=True, timeout=60)
    assert out.stdout.strip().splitlines()[-1] == str(path)

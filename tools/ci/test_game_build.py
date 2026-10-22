"""Tests for tools/ci/game_build.py on fixture appmanifests (no Steam).

Run: uv run pytest tools/ci/test_game_build.py
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

import game_build as gb
import pytest

ACF = """"AppState"
{{
\t"appid"\t\t"1086940"
\t"StateFlags"\t\t"{flags}"
\t"installdir"\t\t"Baldurs Gate 3"
\t"buildid"\t\t"{build}"
\t"TargetBuildID"\t\t"{target}"
}}
"""
PIN = "#!/usr/bin/env bash\nPORT=9998\nWANT_BUILD=25605617\nPORT_TIMEOUT=300\n"


def files(tmp: Path, build: str = "25605617", target: str = "0", flags: str = "4", pin: str = PIN) -> tuple[Path, Path]:
    m, p = tmp / "appmanifest_1086940.acf", tmp / "first_launch.sh"
    m.write_text(ACF.format(build=build, target=target, flags=flags))
    p.write_text(pin)
    return m, p


@pytest.mark.parametrize(
    ("kw", "code", "says"),
    [
        ({}, gb.SAME, "= the pin"),
        ({"target": "25605617"}, gb.SAME, "= the pin"),
        ({"target": ""}, gb.SAME, "= the pin"),
        ({"build": "26000000", "target": "26000000"}, gb.OTHER_BUILD, "26000000 is not the pinned 25605617"),
        ({"target": "26000000"}, gb.UPDATE_PENDING, "TargetBuildID 26000000"),
        ({"flags": "6"}, gb.UPDATE_PENDING, "StateFlags 6"),
        ({"build": ""}, gb.UNKNOWN, "no buildid"),
        ({"pin": "WANT_BUILD=$X\n"}, gb.UNKNOWN, "no WANT_BUILD"),
        ({"pin": "# WANT_BUILD=25605617\n"}, gb.UNKNOWN, "no WANT_BUILD"),
    ],
)
def test_check(tmp_path: Path, kw: dict[str, Any], code: int, says: str):
    v = gb.check(*files(tmp_path, **kw))
    assert v.code == code, v.line
    assert says in v.line


def test_other_build_wins_over_a_pending_update(tmp_path: Path):
    assert gb.check(*files(tmp_path, build="26000000", target="27000000", flags="6")).code == gb.OTHER_BUILD


def test_missing_files_are_unknown(tmp_path: Path):
    m, p = files(tmp_path)
    assert gb.check(tmp_path / "nope.acf", p).code == gb.UNKNOWN
    assert gb.check(m, tmp_path / "nope.sh").code == gb.UNKNOWN


def test_the_repo_pin_is_readable():
    assert gb.read_pin(gb.PIN_FILE.read_text()) is not None


def test_cli_exit_codes(tmp_path: Path):
    script = Path(gb.__file__)

    def run(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, str(script), *args], capture_output=True, text=True, check=False)

    m, p = files(tmp_path, build="26000000")
    r = run("--manifest", str(m), "--pin-file", str(p))
    assert r.returncode == gb.OTHER_BUILD
    assert "not the pinned" in r.stdout
    assert run("--bogus").returncode == gb.USAGE

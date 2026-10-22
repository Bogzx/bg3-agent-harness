"""Tests for tools/ci/lua_lint.py: parsing luacheck's output, the shrink-only baseline, and main on a fake docker."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import lua_lint
import pytest

OUT = (
    "mod/A.lua:3:7: (W211) unused variable 'x'\n"
    "mod/A.lua:9:121: (W631) line is too long (130 > 120)\n"
    "mod/A.lua:12:121: (W631) line is too long (125 > 120)\n"
    "tools/dev/test_b.lua:1:1: (E011) expected expression near 'end'\n"
    "\n"
    "Total: 4 warnings / 1 error in 2 files\n"
)


@pytest.mark.unit
def test_parse_counts_per_file_and_code() -> None:
    counts, lines = lua_lint.parse(OUT)
    assert counts == {"mod/A.lua": {"W211": 1, "W631": 2}, "tools/dev/test_b.lua": {"E011": 1}}
    assert lines[("mod/A.lua", "W631")] == OUT.splitlines()[1:3]


@pytest.mark.unit
def test_more_warnings_than_the_baseline_fail() -> None:
    diffs = lua_lint.compare({"a.lua": {"W211": 1}}, {"a.lua": {"W211": 2}, "b.lua": {"W113": 1}})
    assert diffs == [("a.lua", "W211", 1, 2), ("b.lua", "W113", 0, 1)]
    assert lua_lint.describe(diffs[1]) == "b.lua: 1 W113 warnings, the baseline allows 0: fix the new ones"


@pytest.mark.unit
def test_fewer_warnings_than_the_baseline_fail_until_it_shrinks() -> None:
    diffs = lua_lint.compare({"a.lua": {"W211": 2, "W631": 1}, "gone.lua": {"W113": 1}}, {"a.lua": {"W211": 1}})
    assert diffs == [("a.lua", "W211", 2, 1), ("a.lua", "W631", 1, 0), ("gone.lua", "W113", 1, 0)]
    assert lua_lint.describe(diffs[0]) == "a.lua: W211 down to 1 from 2: lower the baseline (--needed)"


@pytest.mark.unit
def test_exact_baseline_passes() -> None:
    assert lua_lint.compare({"a.lua": {"W211": 1}}, {"a.lua": {"W211": 1}}) == []


@pytest.mark.unit
def test_baseline_round_trip_and_version() -> None:
    text = lua_lint.dump_baseline({"b.lua": {"W631": 2, "W211": 1}, "a.lua": {"W113": 1}, "clean.lua": {}})
    assert text.index('"a.lua"') < text.index('"b.lua"') and "clean.lua" not in text
    assert lua_lint.load_baseline(text) == {"a.lua": {"W113": 1}, "b.lua": {"W211": 1, "W631": 2}}
    with pytest.raises(ValueError, match="version 2"):
        lua_lint.load_baseline('{"version": 2, "warnings": {}}')


@pytest.mark.unit
def test_image_tag_follows_the_dockerfile() -> None:
    assert lua_lint.image_tag("FROM a") == lua_lint.image_tag("FROM a") != lua_lint.image_tag("FROM b")
    assert lua_lint.image_tag("FROM a").startswith("bg3ai-luacheck:")


class FakeDocker:
    """Answers docker image inspect / build / run like docker would, recording each call."""

    def __init__(self, outputs: dict[str, tuple[int, str]], have_image: bool = True) -> None:
        self.outputs, self.have_image = outputs, have_image
        self.calls: list[list[str]] = []

    def __call__(self, argv: Sequence[str]) -> tuple[int, str, str]:
        self.calls.append(list(argv))
        if argv[:3] == ["docker", "image", "inspect"]:
            return (0 if self.have_image else 1), "", ""
        if argv[0] == "sh":
            self.have_image = True
            return 0, "sha256:x\n", ""
        group = lua_lint.MOD if lua_lint.MOD in argv else "tests"
        rc, out = self.outputs[group]
        return rc, out, ""


@pytest.fixture
def tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / "tools/dev").mkdir(parents=True)
    (tmp_path / "tools/dev/test_b.lua").write_text("")
    (tmp_path / "tools/dev/helper.lua").write_text("")
    monkeypatch.setattr(lua_lint, "BASELINE", tmp_path / "baseline.json")
    return tmp_path


MOD_OUT = "mod/Mods/BG3AI/ScriptExtender/Lua/Shared/U.lua:1:1: (W211) unused variable 'x'\n"
TEST_OUT = "tools/dev/test_b.lua:2:1: (W311) value assigned to variable 'j' is unused\n"


@pytest.mark.integration
def test_main_passes_on_the_baseline_and_checks_mod_and_tests_apart(
    tree: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    docker = FakeDocker({lua_lint.MOD: (1, MOD_OUT), "tests": (1, TEST_OUT)})
    (tree / "baseline.json").write_text(lua_lint.dump_baseline(lua_lint.parse(MOD_OUT + TEST_OUT)[0]))
    assert lua_lint.main([], run=docker, root=tree) == 0
    assert capsys.readouterr().out == "luacheck: OK (2 baselined warnings)\n"
    runs = [c for c in docker.calls if c[:2] == ["docker", "run"]]
    assert [c[-1] for c in runs] == [lua_lint.MOD, "tools/dev/test_b.lua"]  # two runs; helpers aren't tests


@pytest.mark.integration
def test_main_fails_on_a_new_warning_and_prints_its_lines(tree: Path, capsys: pytest.CaptureFixture[str]) -> None:
    docker = FakeDocker({lua_lint.MOD: (1, MOD_OUT), "tests": (1, TEST_OUT)})
    (tree / "baseline.json").write_text(lua_lint.dump_baseline(lua_lint.parse(TEST_OUT)[0]))
    assert lua_lint.main([], run=docker, root=tree) == 1
    out = capsys.readouterr().out.splitlines()
    assert out[0].endswith("1 W211 warnings, the baseline allows 0: fix the new ones")
    assert out[1] == "    " + MOD_OUT.strip()
    assert out[-1] == "luacheck: 1 problems"


@pytest.mark.integration
def test_main_fails_on_a_stale_baseline(tree: Path, capsys: pytest.CaptureFixture[str]) -> None:
    docker = FakeDocker({lua_lint.MOD: (0, ""), "tests": (1, TEST_OUT)})
    (tree / "baseline.json").write_text(lua_lint.dump_baseline(lua_lint.parse(MOD_OUT + TEST_OUT)[0]))
    assert lua_lint.main([], run=docker, root=tree) == 1
    assert "W211 down to 0 from 1: lower the baseline" in capsys.readouterr().out


@pytest.mark.integration
def test_needed_prints_the_baseline_and_builds_a_missing_image(tree: Path, capsys: pytest.CaptureFixture[str]) -> None:
    docker = FakeDocker({lua_lint.MOD: (1, MOD_OUT), "tests": (0, "")}, have_image=False)
    assert lua_lint.main(["--needed"], run=docker, root=tree) == 0
    assert lua_lint.load_baseline(capsys.readouterr().out) == lua_lint.parse(MOD_OUT)[0]
    assert docker.calls[1][0] == "sh" and "docker build" in docker.calls[1][2]


@pytest.mark.integration
def test_luacheck_crash_is_an_error(tree: Path) -> None:
    docker = FakeDocker({lua_lint.MOD: (4, "fatal"), "tests": (0, "")})
    with pytest.raises(RuntimeError, match="exit 4"):
        lua_lint.main([], run=docker, root=tree)

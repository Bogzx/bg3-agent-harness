"""Tests for tools/ci/import_contracts.py: config filtering, the INI form, and report vs enforce on a tmp tree."""

from __future__ import annotations

import configparser
import sys
from pathlib import Path

import import_contracts
import pytest

CFG = {
    "root_packages": ["bg3ai", "bg3harness", "bg3bench", "bg3play"],
    "include_external_packages": False,
    "contracts": [
        {
            "name": "harness",
            "type": "forbidden",
            "source_modules": ["bg3harness", "bg3ai"],
            "forbidden_modules": ["bg3bench", "bg3play"],
        },
        {"name": "independent", "type": "independence", "modules": ["bg3bench", "bg3play"]},
    ],
}


@pytest.mark.unit
def test_everything_present_keeps_every_contract() -> None:
    filtered, dropped = import_contracts.filter_config(CFG, lambda _: True)
    assert filtered == CFG
    assert dropped == []


@pytest.mark.unit
def test_missing_packages_drop_the_contracts_with_nothing_left() -> None:
    filtered, dropped = import_contracts.filter_config(CFG, lambda name: name in {"bg3ai", "bg3harness", "bg3bench"})
    assert filtered["root_packages"] == ["bg3ai", "bg3harness", "bg3bench"]
    assert filtered["contracts"] == [
        {
            "name": "harness",
            "type": "forbidden",
            "source_modules": ["bg3harness", "bg3ai"],
            "forbidden_modules": ["bg3bench"],
        }
    ]
    assert dropped == ["independent"]


@pytest.mark.unit
def test_submodules_follow_their_root_package() -> None:
    cfg = {
        "root_packages": ["a"],
        "contracts": [
            {"name": "c", "type": "forbidden", "source_modules": ["a.x"], "forbidden_modules": ["b.y", "a.z"]}
        ],
    }
    filtered, _ = import_contracts.filter_config(cfg, lambda name: name == "a")
    assert filtered["contracts"][0]["forbidden_modules"] == ["a.z"]


@pytest.mark.unit
def test_ini_form() -> None:
    ini = configparser.ConfigParser()
    ini.read_string(import_contracts.to_ini(CFG))
    assert ini["importlinter"]["root_packages"].split() == CFG["root_packages"]
    assert ini["importlinter"]["include_external_packages"] == "False"
    assert ini["importlinter:contract:1"]["type"] == "independence"
    assert ini["importlinter:contract:1"]["modules"].split() == ["bg3bench", "bg3play"]


@pytest.fixture
def tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / "pyproject.toml").write_text(
        '[tool.importlinter]\nroot_packages = ["ilt_core", "ilt_app", "ilt_missing"]\n'
        '[[tool.importlinter.contracts]]\nname = "core imports no app"\ntype = "forbidden"\n'
        'source_modules = ["ilt_core"]\nforbidden_modules = ["ilt_app", "ilt_missing"]\n'
    )
    for pkg in ("ilt_core", "ilt_app"):
        (tmp_path / pkg).mkdir()
        (tmp_path / pkg / "__init__.py").write_text("")
    (tmp_path / "ilt_core" / "bad.py").write_text("import ilt_app\n")
    monkeypatch.setattr(import_contracts, "ROOT", tmp_path)
    monkeypatch.setattr(sys, "path", [str(tmp_path), *sys.path])
    return tmp_path


@pytest.mark.integration
def test_report_mode_prints_a_broken_contract_and_passes(tree: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert import_contracts.main([]) == 0
    out = capsys.readouterr().out
    assert "not yet importable, skipped: ilt_missing" in out
    assert "ilt_core.bad -> ilt_app" in out
    assert "REPORT MODE" in out


@pytest.mark.integration
def test_enforce_fails_on_a_broken_contract(tree: Path) -> None:
    assert import_contracts.main(["--enforce"]) == 1


@pytest.mark.integration
def test_enforce_passes_a_kept_contract(tree: Path) -> None:
    (tree / "ilt_core" / "bad.py").write_text("")
    assert import_contracts.main(["--enforce"]) == 0

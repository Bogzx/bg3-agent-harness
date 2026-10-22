"""The harness as an installed wheel, not the workspace's editable install (harness analysis O6, bug H5): `uv build` the
package, install it into a fresh venv (offline, no workspace on its path) and use it there. Before the fix the wheel
had no prompts/ and no schemas/ (bg3harness.assemble and schema failed installed), declared no dependencies although it
imports bg3ai, and its pytest11 plugin rewrote the game-profile env of every pytest run in the venv that installed it.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

pytestmark = pytest.mark.e2e

PACKAGE = Path(__file__).resolve().parents[1]
UV = shutil.which("uv")

USE = r"""
import json, bg3harness
a = bg3harness.assemble("rules")
print(json.dumps({"source_tree": bg3harness.SOURCE_TREE, "prompts": str(bg3harness.PROMPTS), "text": len(a.text),
                  "sha": a.sha256, "facts": bg3harness.schema("facts.v1")["$id"], "version": bg3harness.__version__}))
"""
PROBE = """
import os

def test_env():
    print("SAVE_DIR=" + os.environ.get("BG3AI_SAVE_DIR", "<unset>"))
"""


def run(*argv: str, env: dict[str, str] | None = None, cwd: Path | None = None) -> str:
    out = subprocess.run(argv, capture_output=True, text=True, timeout=300, env=env, cwd=cwd)
    assert out.returncode == 0, f"{argv}: {out.stdout}\n{out.stderr}"
    return out.stdout


@pytest.fixture(scope="module")
def venv(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    """(the built wheel, the venv's python) with the wheel and pytest installed, offline. pytest and the harness's
    other dependencies come from uv.lock (exported as a pylock.toml: wheel URLs, no index lookup), so they install from
    the cache `uv sync --locked` filled; a bare `pytest` needs the registry's index, which a cold CI cache lacks."""
    if UV is None:
        pytest.skip("uv is not on PATH")
    d = tmp_path_factory.mktemp("wheel")
    run(UV, "build", "--offline", "--wheel", str(PACKAGE), "-o", str(d / "dist"))
    (wheel,) = (d / "dist").glob("bg3_agent_harness-*.whl")
    run(UV, "venv", "--offline", "--python", sys.executable, str(d / "venv"))
    py = d / "venv" / "bin" / "python"
    run(UV, "pip", "install", "--offline", "--python", str(py), "--no-deps", str(wheel))  # bg3ai: not on PyPI
    lock = d / "pylock.toml"
    export = ("--frozen", "--format", "pylock.toml", "--package", "bg3-agent-harness", "--extra", "testing")
    run(UV, "export", *export, "--no-emit-workspace", "--no-dev", "-q", "-o", str(lock), cwd=PACKAGE)
    run(UV, "pip", "install", "--offline", "--python", str(py), "-r", str(lock))
    return wheel, py


def clean_env() -> dict[str, str]:
    """No workspace leaking in: no PYTHONPATH, no game-profile vars from this pytest run's own plugin."""
    drop = ("PYTHONPATH", "VIRTUAL_ENV", "BG3AI_SAVE_DIR", "BG3AI_SE_DIR", "BG3AI_PROFILE_DIR", "BG3AI_TEST_PROFILE")
    return {k: v for k, v in os.environ.items() if k not in drop and not k.startswith("UV_")}


def test_the_wheel_carries_the_prompts_and_schemas_and_declares_its_dependencies(venv: tuple[Path, Path]):
    wheel, _ = venv
    with zipfile.ZipFile(wheel) as z:
        names = z.namelist()
        meta = z.read(next(n for n in names if n.endswith(".dist-info/METADATA"))).decode()
    assert "bg3harness/prompts/assemble.py" in names and "bg3harness/schemas/facts.v1.schema.json" in names
    assert any(n.startswith("bg3harness/prompts/rules/") for n in names)
    assert not [n for n in names if "/tests/" in n or "__pycache__" in n]
    assert "Requires-Dist: bg3ai-bridge" in meta and "Requires-Dist: pytest>=8; extra == 'testing'" in meta


def test_an_installed_harness_assembles_the_rules_and_reads_the_schemas(venv: tuple[Path, Path], tmp_path: Path):
    _, py = venv
    got = json.loads(run(str(py), "-I", "-c", USE, env=clean_env(), cwd=tmp_path))
    assert not got["source_tree"] and "site-packages/bg3harness/prompts" in got["prompts"]
    assert got["facts"].endswith("/schemas/facts.v1.schema.json") and got["text"] > 1000
    import bg3harness

    assert (got["sha"], got["version"]) == (bg3harness.assemble("rules").sha256, bg3harness.__version__)  # same files


def test_an_installed_harness_leaves_other_pytest_runs_alone_unless_asked(venv: tuple[Path, Path], tmp_path: Path):
    _, py = venv
    (tmp_path / "test_probe.py").write_text(PROBE)
    argv = (str(py), "-m", "pytest", "-q", "-s", "-p", "no:cacheprovider", "test_probe.py")
    assert "SAVE_DIR=<unset>" in run(*argv, env=clean_env(), cwd=tmp_path)
    asked = run(*argv, env=clean_env() | {"BG3HARNESS_TEMP_PROFILE": "1"}, cwd=tmp_path)
    assert "SAVE_DIR=" in asked and "SAVE_DIR=<unset>" not in asked and "bg3ai-test-profile-" in asked

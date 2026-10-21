import os
import shutil
import subprocess
from pathlib import Path

import pytest

from bg3play.store import RunStore

FIXTURE = Path(__file__).parent / "fixtures" / "run"
PT = "claude-run-2"


def git(repo: Path, *args: str, date: int | None = None) -> None:
    env = dict(os.environ, GIT_AUTHOR_DATE=f"@{date}", GIT_COMMITTER_DATE=f"@{date}") if date else None
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false", *args],
        check=True,
        capture_output=True,
        env=env,
    )


@pytest.fixture
def run_dir(tmp_path: Path) -> Path:
    """A writable copy of the recorded fixture run."""
    dst = tmp_path / "run"
    shutil.copytree(FIXTURE, dst)
    return dst


@pytest.fixture
def store(run_dir: Path) -> RunStore:
    return RunStore(run_dir / "runtime", run_dir / "playthroughs")


@pytest.fixture
def git_memory(run_dir: Path) -> Path:
    """The fixture memory as a git repo with two commits (S095, then an S096 keeper edit of HANDOFF.md)."""
    mem = run_dir / "playthroughs" / PT / "memory"
    git(mem, "init", "-q")
    git(mem, "add", "-A")
    git(mem, "commit", "-q", "-m", "S095", date=1792311056)
    (mem / "HANDOFF.md").write_text((mem / "HANDOFF.md").read_text() + "\n- keeper line\n")
    git(mem, "commit", "-q", "-am", "S096 memory-keeper: HANDOFF.md", date=1792312786)
    return mem

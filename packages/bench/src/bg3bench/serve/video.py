# pyright: basic
"""Render video from the panel: a run's evidence video through a `Renderer`, one render at a time.

`VideoProcess`, the real renderer, starts the harness's renderer in a process of its own
(`python -m bg3harness.video <run_dir> --index <run_dir>/recording/index.json --out <run_dir> ...`), with its output
in <run_dir>/render.log. It writes render.json, evidence.mp4 and frames/ into the run dir, where the pack step picks
render.json and the contact sheet up. The id line names the bundle the user validated last (Submit's prepare).
"""

from __future__ import annotations

import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlparse

from bg3bench.serve.launch import Popen, Proc
from bg3bench.serve.panel import PanelError

RECORDING_INDEX = Path("recording") / "index.json"
LOG = "render.log"
VIDEO = "evidence.mp4"


@dataclass(frozen=True)
class Job:
    run_dir: Path
    run_id: str
    bundle_sha256: str
    title: str = ""
    task: str = ""
    cap_min: float | None = None
    cost_kind: str = "billed"
    t_pass_s: float | None = None
    site: str = ""


class Renderer(Protocol):
    def start(self, job: Job) -> None:
        """Start rendering; PanelError when a render runs or the run has no recording."""
        ...

    def state(self) -> dict[str, Any] | None:
        """{"run", "running", "returncode", "started", "log", "video", "video_bytes"} of the last start, or None."""
        ...


def job_for(run_dir: Path, man: Mapping[str, Any], res: Mapping[str, Any], sc: Mapping[str, Any] | None, *,
            bundle_sha256: str, site: str | None) -> Job:  # fmt: skip
    """The renderer's inputs from a run's manifest and result and its scenario file."""
    sc = sc or {}
    hours = (sc.get("caps") or {}).get("run_hours")
    return Job(
        run_dir=run_dir,
        run_id=str(res.get("run_id") or man.get("run_id") or run_dir.name),
        bundle_sha256=bundle_sha256,
        title=str(sc.get("title") or (man.get("scenario") or {}).get("name") or ""),
        task=str(sc.get("task") or ""),
        cap_min=hours * 60 if hours else None,
        cost_kind=str(res.get("cost_kind") or "billed"),
        t_pass_s=res.get("t_pass_s"),
        site=(urlparse(site).hostname or "") if site else "",
    )


def command(python: str, job: Job) -> list[str]:
    argv = [python, "-m", "bg3harness.video", str(job.run_dir), "--index", str(job.run_dir / RECORDING_INDEX),
            "--out", str(job.run_dir), "--run-id", job.run_id, "--bundle", job.bundle_sha256,
            "--cost-kind", job.cost_kind]  # fmt: skip
    for flag, v in (("--site", job.site), ("--title", job.title), ("--task", job.task)):
        if v:
            argv += [flag, v]
    if job.cap_min is not None:
        argv += ["--cap-min", f"{job.cap_min:g}"]
    if job.t_pass_s is not None:
        argv += ["--t-pass-s", f"{job.t_pass_s:g}"]
    return argv


@dataclass
class _Started:
    job: Job
    proc: Proc
    started: float


class VideoProcess:
    def __init__(
        self, *, popen: Popen = subprocess.Popen, python: str = sys.executable, now: Callable[[], float] = time.time
    ):
        self.popen, self.python, self.now = popen, python, now
        self.current: _Started | None = None

    def start(self, job: Job) -> None:
        if self.current is not None and self.current.proc.poll() is None:
            raise PanelError(f"rendering {self.current.job.run_id} already: one render at a time")
        if not (job.run_dir / RECORDING_INDEX).is_file():
            raise PanelError(f"{job.run_id} has no recording ({RECORDING_INDEX} in its run dir)")
        with (job.run_dir / LOG).open("ab") as out:
            proc = self.popen(
                command(self.python, job), stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                start_new_session=True,
            )  # fmt: skip
        self.current = _Started(job, proc, self.now())

    def state(self) -> dict[str, Any] | None:
        cur = self.current
        if cur is None:
            return None
        rc = cur.proc.poll()
        video = cur.job.run_dir / VIDEO
        return {
            "run": cur.job.run_dir.name,
            "running": rc is None,
            "returncode": rc,
            "started": cur.started,
            "log": str(cur.job.run_dir / LOG),
            "video": str(video) if rc == 0 and video.is_file() else None,
            "video_bytes": video.stat().st_size if rc == 0 and video.is_file() else None,
        }

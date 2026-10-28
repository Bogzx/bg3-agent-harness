# pyright: basic
"""Render video from the panel: a run's evidence video through a `Renderer`, one render at a time.

`VideoProcess`, the real renderer, starts the harness's renderer in a process of its own
(`python -m bg3harness.video <bundle_dir> --index <run_dir>/recording/index.json --out <out> --run-hash H ...`), with
its output in <out>/render.log. It reads the record from the bundle the user validated last (Submit's prepare: the
packed, scrubbed files, so the side panel shows no path or key the bundle doesn't) and the recording from the run dir,
and writes render.json, evidence.mp4 and frames/ into <out>, a new render-<n>/ beside that bundle. The id line shows
the bundle's run_hash (bundle.json), a hash over the record files that the render's own outputs don't change; once
the render ends well, the panel adds render.json and frames/ to the bundle (submit.add_render).
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlparse

RUN_HASH = re.compile(r"[0-9a-f]{64}")

from bg3bench.serve.launch import Popen, Proc
from bg3bench.serve.panel import PanelError

RECORDING_INDEX = Path("recording") / "index.json"
LOG = "render.log"
VIDEO = "evidence.mp4"


@dataclass(frozen=True)
class Job:
    run_dir: Path
    source: Path  # the prepared bundle's dir: the record files the video shows
    out: Path  # where the renderer writes render.json, evidence.mp4, frames/ and its log
    run_id: str
    run_hash: str
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
        """{"run", "source", "out", "running", "returncode", "started", "log", "video", "video_bytes"} of the last
        start, or None."""
        ...


def job_for(run_dir: Path, man: Mapping[str, Any], res: Mapping[str, Any], sc: Mapping[str, Any] | None, *,
            source: Path, out: Path, run_hash: str, site: str | None) -> Job:  # fmt: skip
    """The renderer's inputs from a run's manifest and result and its scenario file."""
    sc = sc or {}
    hours = (sc.get("caps") or {}).get("run_hours")
    return Job(
        run_dir=run_dir,
        source=source,
        out=out,
        run_id=str(res.get("run_id") or man.get("run_id") or run_dir.name),
        run_hash=run_hash,
        title=str(sc.get("title") or (man.get("scenario") or {}).get("name") or ""),
        task=str(sc.get("task") or ""),
        cap_min=hours * 60 if hours else None,
        cost_kind=str(res.get("cost_kind") or "billed"),
        t_pass_s=res.get("t_pass_s"),
        site=(urlparse(site).hostname or "") if site else "",
    )


def run_hash(bundle_dir: Path) -> str:
    """bundle.json's run_hash; PanelError when the bundle has none (a pack from before run_hash: validate again)."""
    try:
        h = json.loads((bundle_dir / "bundle.json").read_text(encoding="utf-8")).get("run_hash")
    except (OSError, ValueError, AttributeError):
        h = None
    if not isinstance(h, str) or not RUN_HASH.fullmatch(h):
        raise PanelError(f"{bundle_dir} has no run_hash in bundle.json: validate the run again")
    return h


def command(python: str, job: Job) -> list[str]:
    argv = [python, "-m", "bg3harness.video", str(job.source), "--index", str(job.run_dir / RECORDING_INDEX),
            "--out", str(job.out), "--run-id", job.run_id, "--run-hash", job.run_hash,
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
        job.out.mkdir(parents=True, exist_ok=True)
        with (job.out / LOG).open("ab") as out:
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
        video = cur.job.out / VIDEO
        return {
            "run": cur.job.run_dir.name,
            "source": str(cur.job.source),
            "out": str(cur.job.out),
            "running": rc is None,
            "returncode": rc,
            "started": cur.started,
            "log": str(cur.job.out / LOG),
            "video": str(video) if rc == 0 and video.is_file() else None,
            "video_bytes": video.stat().st_size if rc == 0 and video.is_file() else None,
        }


def out_dir(bundle_dir: Path) -> Path:
    """A new render-<n>/ beside the bundle (out_dir/bundle): each render starts with no frames of an earlier one."""
    base = bundle_dir.parent
    n = 1 + max((int(p.name[7:]) for p in base.glob("render-*") if p.name[7:].isdigit()), default=0)
    return base / f"render-{n}"

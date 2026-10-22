"""U13 the fps cost of recording the game window (live2 §4): client frame times (a Tick listener's
Ext.Utils.MonotonicTime deltas, drained every window) before, while and after ffmpeg x11grab -window_id records the
BG3 window (found by name with xwininfo; rootless Xwayland has no root content, live1 §7). PASS: ffmpeg exited 0
and the recording phase's mean fps is within MAX_DROP_PCT of the phase before. Frame stats in fps.json, the video
in rec.mp4, ffmpeg's log in rec.ffmpeg.txt. A window's fps is its frames over their own summed frame times, so the
drain's RPC timing doesn't enter it.

    u13_rec_fps.py [<before s>=60] [<record s>=110] [<after s>=60] [<window s>=10]
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

from probelib import Probe, Verdict, arg, main

MAX_DROP_PCT = 5.0  # live2: 55.1 -> 54.5 fps (1 %)
WINDOW_RE = re.compile(r"^\s*(0x[0-9a-f]+) \"Baldur's Gate 3", re.M)
INSTALL = r"""if not _G.PROBE_FT then
  _G.PROBE_FT = {}
  _G.PROBE_LAST = Ext.Utils.MonotonicTime()
  Ext.Events.Tick:Subscribe(function()
    local t = Ext.Utils.MonotonicTime(); local ft = _G.PROBE_FT; ft[#ft+1] = t - _G.PROBE_LAST; _G.PROBE_LAST = t
  end)
end
_G.PROBE_FT = {}
return 'ok'"""
DRAIN = "local ft=_G.PROBE_FT; _G.PROBE_FT={}; return Ext.Json.Stringify(ft)"


def stats(ft: list[float], wall: float) -> dict:
    s = sorted(ft)
    n = len(s)

    def q(p: float) -> float | None:
        return s[min(n - 1, int(p * n))] if n else None

    return {
        "frames": n,
        "wall_s": round(wall, 2),
        "fps": round(1000.0 * n / sum(s), 1) if n and sum(s) > 0 else 0.0,
        "ft_p50_ms": q(0.5),
        "ft_p95_ms": q(0.95),
        "ft_max_ms": s[-1] if n else None,
        "over_50ms": sum(1 for x in s if x > 50),
    }


def summary(name: str, windows: list[dict]) -> dict:
    fps = [w["fps"] for w in windows] or [0.0]
    return {"phase": name, "windows": windows, "fps_mean": round(sum(fps) / len(fps), 1), "fps_min": min(fps)}


def judge(phases: list[dict], rc: int | None) -> Verdict:
    by = {ph["phase"]: ph for ph in phases}
    shown = ", ".join(f"{n} {by[n]['fps_mean']} (min {by[n]['fps_min']})" for n in ("before", "recording", "after"))
    if rc != 0:
        return "FAIL", f"ffmpeg exit {rc} (rec.ffmpeg.txt); fps {shown}"
    b, r = by["before"]["fps_mean"], by["recording"]["fps_mean"]
    if b <= 0:
        return "FAIL", f"no frames measured before recording: {shown}"
    drop = 100.0 * (b - r) / b
    v = "PASS" if drop <= MAX_DROP_PCT else "FAIL"
    return v, f"fps {shown}; recording costs {drop:.1f} % (limit {MAX_DROP_PCT:.0f} %)"


def window_id() -> str | None:
    out = subprocess.run(["xwininfo", "-root", "-tree"], capture_output=True, text=True, check=False).stdout
    m = WINDOW_RE.search(out)
    return m.group(1) if m else None


def recorder(wid: str, secs: float, out: Path) -> subprocess.Popen:
    argv = [
        "nice",
        "-n",
        "15",
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "warning",
        "-y",
        "-f",
        "x11grab",
        "-framerate",
        "15",
        "-window_id",
        wid,
        "-i",
        os.environ.get("DISPLAY", ":1"),
        "-t",
        f"{secs:g}",
        "-vf",
        "scale=1280:720",
        "-r",
        "15",
        "-c:v",
        "h264_nvenc",
        "-preset",
        "p4",
        "-b:v",
        "4M",
        str(out / "rec.mp4"),
    ]
    return subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=(out / "rec.ffmpeg.txt").open("w"))


def run(p: Probe) -> Verdict:
    before_s, rec_s, after_s, win_s = (float(arg(p, i, d)) for i, d in enumerate(("60", "110", "60", "10")))
    wid = window_id()
    if wid is None:
        return "FAIL", "no Baldur's Gate 3 window in xwininfo -root -tree"
    p.log(f"window {wid}: {p.lua(INSTALL, 'client')}")

    def phase(name: str, secs: float) -> dict:
        p.lua(DRAIN, "client")
        t0 = start = p.clock()
        windows = []
        while p.clock() - start < secs:
            p.sleep(win_s)
            ft = json.loads(p.lua(DRAIN, "client"))
            t1 = p.clock()
            windows.append(stats(ft if isinstance(ft, list) else [], t1 - t0))
            t0 = t1
        ph = summary(name, windows)
        p.log(f"{name} {ph['fps_mean']} {ph['fps_min']}")
        return ph

    phases = [phase("before", before_s)]
    ff = recorder(wid, rec_s + 10.0, p.out)
    phases.append(phase("recording", rec_s))
    rc = ff.wait()
    phases.append(phase("after", after_s))
    (p.out / "fps.json").write_text(json.dumps(phases, indent=1))
    return judge(phases, rc)


if __name__ == "__main__":
    raise SystemExit(main(run))

"""Can a new machine run the harness? Every check a stranger's machine needs, each failure with the fix. Stdlib only and
read-only: it reads files and runs version queries (and one 0.1 s ffmpeg test encode), never installs or changes
anything.

    python3 tools/doctor.py [--json]          # or: uv run python -m bg3harness.doctor [--json]

`doctor()` returns the checks as `Check(name, status, detail, fix)` (the bench panel calls it and `report()`); the
CLI prints one PASS / WARN / FAIL line per check with its fix under it, and exits 1 on any FAIL.

The game, Steam, bg3le, the mod, the clients, RAM and VRAM are bg3harness.preflight's checks (the bench preflight);
this adds what a first install misses:
  bg3le      as preflight's, and a downloaded prebuilt (tools/bg3le_prebuilt.py) is verified against its manifest
  screenshot VK_LAYER_MESA_screenshot (the agent's screenshots go through it; runner/launch_game.sh enables it)
  shim       the SDL input shim build/bg3sdl/libbg3sdl.so ($BG3AI_INPUT_LIB): the agent's clicks and keys
  display    DISPLAY / XAUTHORITY the game can open a window with, as the launcher fills them (bg3harness.display_env)
  ffmpeg     ffmpeg with libx264 (the evidence video), and the bench recording's encoder encoding a test frame
             ($BG3AI_BENCH_RECORD_ENCODER, default h264_nvenc)
  disk       free space for the runtime dir: recordings take 1-2 GB per hour
  uv/python  uv on PATH and a Python >= 3.14 it can use (the packages require it)
  node/pi    Node.js >= 20 and tools/pi's pinned pi (only for the non-Claude clients: WARN, never FAIL)
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from bg3harness import bg3le_buildinfo, bg3le_prebuilt, display_env, preflight, steam_paths
from bg3harness.preflight import FAIL, PASS, WARN

MIN_PYTHON = (3, 14)
MIN_NODE = 20
DISK_WARN_GB, DISK_FAIL_GB = 20, 5
LAYER = "VkLayer_MESA_screenshot.json"
RECORD_ENCODER_ENV = "BG3AI_BENCH_RECORD_ENCODER"  # bg3harness.runner.run_host reads the same variable


@dataclass(frozen=True)
class Check:
    name: str
    status: str  # PASS, WARN or FAIL
    detail: str
    fix: str = ""  # what to do about a WARN or FAIL; "" for a PASS

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(kw_only=True)
class Ctx(preflight.Ctx):
    """preflight's context (repo, Steam/bg3le helpers, home, env, run, which, meminfo) plus what the doctor reads."""

    disk_usage: Callable[[Path], Any] = shutil.disk_usage
    xwayland: Callable[[], str] = display_env.pgrep_xwayland  # `pgrep -a Xwayland` output
    x11_dir: str = "/tmp/.X11-unix"
    layer_dirs: tuple[Path, ...] = field(
        default=(Path("/usr/share/vulkan/explicit_layer.d"), Path("/etc/vulkan/explicit_layer.d"))
    )


def default_ctx() -> Ctx:
    return Ctx(repo=bg3le_buildinfo.REPO, steam=steam_paths, bg3le=bg3le_buildinfo)


def _fix_build(ctx: Ctx, detail: str) -> str:
    want = preflight.pinned_build(ctx.repo)
    if "series" in detail:
        return "finish the series on the build it was frozen on, or start a new series (docs/GAME-UPDATES.md)"
    return (
        f"install the pinned build {want}: docs/GAME-UPDATES.md 'Getting the pinned build' (Steam's download_depot "
        "with the depot and manifest ids recorded there)"
    )


def _fix_bg3le(ctx: Ctx, detail: str) -> str:
    url, _ = bg3le_prebuilt.config(ctx.env)
    fetch = f"python3 {bg3le_buildinfo.FETCH}"
    build = f"{bg3le_buildinfo.REBUILD} (Docker, ~10 GB RAM, 10-20 min, never while the game runs)"
    if url:
        return f"download the verified prebuilt: {fetch}; or build it: {build}"
    return f"build it: {build} (no prebuilt URL in patches/bg3le-prebuilt.toml yet)"


# A fix per preflight check name, for its WARN or FAIL line (the line's detail says what is wrong)
PREFLIGHT_FIXES: dict[str, Callable[[Ctx, str], str]] = {
    "install": lambda c, d: (
        "install Baldur's Gate 3 through Steam (native Linux build) and let Steam finish"
        if "not fully" in d or "no Steam manifest" in d
        else "keep Steam offline so it can't apply the pending update (docs/GAME-UPDATES.md)"
    ),
    "native": lambda c, d: (
        "Steam > BG3 > Properties > Compatibility: untick 'Force the use of a specific Steam Play "
        "compatibility tool', then let Steam install the native build"
    ),
    "sniper": lambda c, d: "install 'Steam Linux Runtime 3.0 (sniper)' from the Steam library (Tools)",
    "build": _fix_build,
    "updates": lambda c, d: (
        "Steam > Go Offline before a run or series (touch ~/.config/bg3ai/require-steam-offline "
        "makes the launcher refuse an online Steam)"
    ),
    "steam": lambda c, d: "start Steam (in offline mode) and leave it running: the game's Steam API attaches to it",
    "bg3le": _fix_bg3le,
    "mod": lambda c, d: "check out the repo's mod/ (Mods/BG3AI) or point BG3AI_MOD_PATH at a copy of it",
    "claude": lambda c, d: "install Claude Code (the `claude` CLI) and log in; it plays the Claude sessions",
    "docker": lambda c, d: (
        "install docker and add your user to the docker group (only the container recipe of the "
        "non-Claude clients and the bg3le build need it)"
    ),
    "ram": lambda c, d: "close other programs before a run (the game peaks ~6.3 GB); 16 GB of RAM is the minimum",
    "vram": lambda c, d: "BG3 needs a 4 GB GPU; only NVIDIA (nvidia-smi) can be checked here",
}


def from_preflight(ctx: Ctx) -> list[Check]:
    out: list[Check] = []
    for status, name, detail in preflight.preflight(ctx):
        fix = "" if status == PASS else PREFLIGHT_FIXES.get(name, lambda c, d: "")(ctx, detail)
        if (
            name == "bg3le"
            and status == PASS
            and bg3le_prebuilt.verify(_dist(ctx), ctx.repo / "patches" / "bg3le") == []
        ):
            detail += " (a prebuilt, verified against its PREBUILT.json)"
        out.append(Check(name, status, detail, fix))
    return out


def _dist(ctx: Ctx) -> Path:
    return Path(ctx.env.get("BG3AI_BG3LE_LIB") or ctx.repo / "build/bg3le/dist/libbg3le.so").parent


def check_screenshot(ctx: Ctx) -> Check:
    data = Path(ctx.env.get("XDG_DATA_HOME") or ctx.home / ".local/share")
    dirs = (*ctx.layer_dirs, data / "vulkan/explicit_layer.d")
    found = next((d / LAYER for d in dirs if (d / LAYER).is_file()), None)
    if found:
        return Check("screenshot", PASS, f"VK_LAYER_MESA_screenshot: {found}")
    return Check(
        "screenshot",
        FAIL,
        f"no {LAYER} in {', '.join(map(str, dirs))}: the game's screenshots (what the agent sees) need it",
        "install Mesa >= 26.1's Vulkan drivers (Debian 13: apt install -t trixie-backports mesa-vulkan-drivers), "
        "then check it with tools/dev/check_screenshot_layer.sh (runs vkcube)",
    )


def check_shim(ctx: Ctx) -> Check:
    lib = Path(ctx.env.get("BG3AI_INPUT_LIB") or ctx.repo / "build/bg3sdl/libbg3sdl.so")
    if preflight.is_elf(lib):
        return Check("shim", PASS, f"SDL input shim {lib}")
    return Check(
        "shim",
        FAIL,
        f"no input shim at {lib} (the agent's clicks and keys go through it)",
        "native/bg3sdl/build.sh (needs gcc and the SDL2 headers: apt install gcc libsdl2-dev)",
    )


def check_display(ctx: Ctx) -> Check:
    env = {k: v for k, v in ctx.env.items() if k in display_env.KEYS}
    got = display_env.fill(env, ps_text=ctx.xwayland())
    why = display_env.problem(env, home=str(ctx.home), x11_dir=ctx.x11_dir)
    found = f" (from the desktop's Xwayland: {', '.join(sorted(got))})" if got else ""
    if why is None:
        return Check("display", PASS, f"{display_env.describe(env)}{found}")
    return Check(
        "display",
        FAIL,
        f"the game can't open a window: {why}",
        "run from a terminal inside the desktop session, or export the desktop's DISPLAY and XAUTHORITY "
        "(`pgrep -a Xwayland` shows `:N` and `-auth <file>`; e.g. DISPLAY=:1 XAUTHORITY=/run/user/1000/xauth_XXXX); "
        "the launcher fills unset ones from a running Xwayland",
    )


def _test_encode(ctx: Ctx, ffmpeg: str, encoder: str) -> tuple[int, str]:
    return ctx.run(
        [ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=256x144:rate=15:duration=0.1",
         "-c:v", encoder, "-f", "null", "-"]
    )  # fmt: skip


def check_ffmpeg(ctx: Ctx) -> list[Check]:
    ffmpeg = ctx.which("ffmpeg")
    if not ffmpeg:
        return [Check("ffmpeg", FAIL, "no ffmpeg on PATH (records every bench run, renders the evidence video)",
                      "apt install ffmpeg")]  # fmt: skip
    rc, out = ctx.run([ffmpeg, "-hide_banner", "-encoders"])
    encoders: set[str] = set(re.findall(r"^\s*V\S*\s+(\S+)", out, re.M)) if rc == 0 else set()
    checks = [
        Check("ffmpeg", PASS, f"{ffmpeg} with libx264 (the evidence video's default encoder)")
        if "libx264" in encoders
        else Check(
            "ffmpeg", FAIL, f"{ffmpeg} has no libx264 encoder", "install a full ffmpeg build (apt install ffmpeg)"
        )
    ]
    encoder = ctx.env.get(RECORD_ENCODER_ENV) or "h264_nvenc"
    rc, out = _test_encode(ctx, ffmpeg, encoder) if encoder in encoders else (1, f"ffmpeg lists no {encoder} encoder")
    if rc == 0:
        checks.append(Check("record", PASS, f"the bench recording's encoder {encoder} encodes a test frame"))
    else:
        fix = (
            f"set {RECORD_ENCODER_ENV}=libx264 (CPU encoding) or install the NVIDIA driver with NVENC"
            if encoder == "h264_nvenc"
            else f"set {RECORD_ENCODER_ENV} to an encoder that works here (h264_nvenc or libx264)"
        )
        last = out.strip().splitlines()[-1][:160] if out.strip() else f"exit {rc}"
        checks.append(Check("record", FAIL, f"{encoder} can't encode here: {last}", fix))
    return checks


def _nearest_dir(path: Path) -> Path:
    while not path.exists() and path != path.parent:
        path = path.parent
    return path


def check_disk(ctx: Ctx) -> Check:
    runtime = Path(ctx.env.get("BG3AI_RUNTIME") or ctx.repo / "runtime")
    where = _nearest_dir(runtime)
    free_gb = ctx.disk_usage(where).free / 1e9
    detail = f"{free_gb:.0f} GB free for {runtime} (recordings take 1-2 GB per recorded hour)"
    if free_gb < DISK_FAIL_GB:
        return Check("disk", FAIL, detail, f"free space on {where}'s disk, or point BG3AI_RUNTIME at a bigger one")
    if free_gb < DISK_WARN_GB:
        return Check("disk", WARN, detail, f"free space on {where}'s disk before long or recorded series")
    return Check("disk", PASS, detail)


def _version(text: str) -> tuple[int, ...]:
    m = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", text)
    return tuple(int(g) for g in m.groups() if g is not None) if m else ()


def check_python(ctx: Ctx) -> list[Check]:
    uv = ctx.which("uv")
    if not uv:
        return [Check("uv", FAIL, "no uv on PATH (installs the packages and their Python)",
                      "install uv: https://docs.astral.sh/uv/getting-started/installation/")]  # fmt: skip
    rc, out = ctx.run([uv, "--version"])
    checks = [Check("uv", PASS, out.splitlines()[0] if rc == 0 and out else uv)]
    want = ".".join(map(str, MIN_PYTHON))
    rc, out = ctx.run([uv, "python", "find", f">={want}"])
    if rc == 0 and out:
        checks.append(Check("python", PASS, f"Python >= {want} for uv: {out.splitlines()[-1]}"))
    else:
        checks.append(Check("python", WARN, f"uv finds no Python >= {want} yet",
                            f"uv sync (in the repo) downloads it, or: uv python install {want}"))  # fmt: skip
    return checks


def check_node(ctx: Ctx) -> list[Check]:
    why = "only for the non-Claude clients (pi)"
    node = ctx.which("node")
    if not node:
        return [Check("node", WARN, f"no node on PATH: {why}", f"install Node.js >= {MIN_NODE}")]
    rc, out = ctx.run([node, "--version"])
    ver = _version(out) if rc == 0 else ()
    if not ver or ver[0] < MIN_NODE:
        return [Check("node", WARN, f"node {out.strip() or '?'} < {MIN_NODE}: {why}", f"install Node.js >= {MIN_NODE}")]
    checks = [Check("node", PASS, f"node {out.strip()}")]
    pi = ctx.repo / "tools/pi/node_modules/.bin/pi"
    if pi.exists():
        checks.append(Check("pi", PASS, f"the pinned pi: {pi}"))
    else:
        checks.append(Check("pi", WARN, f"no {pi}: {why}", "cd tools/pi && npm ci --ignore-scripts"))
    return checks


def doctor(ctx: Ctx | None = None) -> list[Check]:
    """Every check, in order; a check that crashes is a FAIL of its own, never an abort."""
    ctx = ctx or default_ctx()
    out: list[Check] = []
    steps: list[tuple[str, Callable[[Ctx], Check | list[Check]]]] = [
        ("preflight", from_preflight),
        ("screenshot", check_screenshot),
        ("shim", check_shim),
        ("display", check_display),
        ("ffmpeg", check_ffmpeg),
        ("disk", check_disk),
        ("uv", check_python),
        ("node", check_node),
    ]
    for name, step in steps:
        try:
            got = step(ctx)
        except Exception as e:  # one broken check must not hide the others
            got = Check(name, FAIL, f"check crashed: {e!r}", "a bug in bg3harness.doctor: report it")
        out += got if isinstance(got, list) else [got]
    return out


def report(checks: list[Check]) -> dict[str, Any]:
    """The panel's view: ready (no FAIL), the counts, every check as a dict."""
    fails = sum(c.status == FAIL for c in checks)
    return {
        "ready": fails == 0,
        "fails": fails,
        "warns": sum(c.status == WARN for c in checks),
        "checks": [c.to_dict() for c in checks],
    }


def main(argv: list[str] | None = None, ctx: Ctx | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--json", action="store_true", help="print report() as JSON")
    a = ap.parse_args(argv)
    checks = doctor(ctx)
    rep = report(checks)
    if a.json:
        print(json.dumps(rep, indent=1))
    else:
        width = max(len(c.name) for c in checks)
        for c in checks:
            print(f"{c.status}  {c.name:<{width}}  {c.detail}")
            if c.fix:
                print(f"{'':6}{'':<{width}}  fix: {c.fix}")
        print(f"\n{'READY' if rep['ready'] else 'NOT READY'}: {rep['fails']} FAIL, {rep['warns']} WARN")
    return 0 if rep["ready"] else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

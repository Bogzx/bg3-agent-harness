"""Can this machine run the benchmark (docs/BENCHMARK.md)? Stdlib only, read-only: it reads files and runs version
queries, never writes or changes anything (Steam's files and settings least of all).

    python3 tools/bench_preflight.py [--series NAME]

Moved from bg3bench.preflight (which is now this module) so that bg3harness.doctor runs the same machine checks.
The CLI stays at tools/bench_preflight.py: it passes in the harness's Steam and bg3le helpers (tools/steam_paths.py,
tools/bg3le_buildinfo.py) as Ctx.steam and Ctx.bg3le; doctor passes bg3harness.steam_paths and bg3le_buildinfo.

One PASS / WARN / FAIL line per check; exit 1 on any FAIL, else 0.
  install    appmanifest_1086940.acf in a Steam library (tools/steam_paths.py): BG3 installed through Steam, fully
             (StateFlags), no update pending (TargetBuildID). The honest ownership check we can make offline
  native     <game>/bin/bg3 (what runner/launch_game.sh starts) is an ELF, not a Proton install; the sniper runtime
  build      buildid = the pinned one (tools/dev/first_launch.sh WANT_BUILD: the build bg3le was verified on), and
             with --series the series' frozen pins.game_build (runtime/sessions/<series>/series.json)
  updates    Steam offline (the last login, as the runner checks it, and loginusers.vdf WantsOfflineMode) and the
             game's AutoUpdateBehavior: WARN when Steam could change the build in the middle of a series
  steam      a Steam client is running (the runner refuses to launch the game without one)
  bg3le      build/bg3le/dist/libbg3le.so built with every patches/bg3le patch (tools/bg3le_buildinfo.py, the
             runner's own stale check)
  mod        the loose mod the launcher passes ($BG3AI_MOD_PATH, else <repo>/mod): Mods/BG3AI/meta.lsx + ScriptExtender
  claude     the claude CLI (runs the play sessions, the memory keeper and the persona judge)
  docker     docker and its daemon (only for the container recipe of non-Claude clients: WARN, never FAIL)
  ram, vram  vs the game's cgroup cap ($BG3AI_GAME_MEMORY_MAX, default 10G; the game peaks ~6.3 GB RSS) and BG3's
             minimum spec (4 GB VRAM)

AutoUpdateBehavior: Steam does not document the appmanifest format. The values below match the per-game
"Automatic updates" choice in the client (Properties > Updates) as the community documents it (SteamDB, Steam
forums) and as this machine's manifest shows for its default: fairly sure of 0 and 1, less of 2.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

PINNED_BUILD = "25605617"  # fallback only: tools/dev/first_launch.sh's WANT_BUILD is the source
UPDATE_BEHAVIOR = {
    "0": "always keep this game updated (background updates)",
    "1": "only update this game when I launch it (through Steam)",
    "2": "high priority: always auto-update this game before others",
}
STATE_UPDATE_REQUIRED, STATE_FULLY_INSTALLED, STATE_UPDATE_RUNNING = 2, 4, 256  # EAppState bits
DEFAULT_GAME_CAP = "10G"
MIN_VRAM_MIB = 4000  # BG3's minimum spec is a 4 GB card (nvidia-smi reports 4096 MiB for one)
PASS, WARN, FAIL = "PASS", "WARN", "FAIL"


def _run(cmd: list[str], timeout: float = 20) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError) as e:
        return 127, str(e)
    return p.returncode, (p.stdout or p.stderr).strip()


@dataclass(kw_only=True)
class Ctx:
    """Everything the checks read, so the tests can point it at a temp dir and stub the commands."""

    repo: Path  # the harness checkout (patches/, tools/dev/first_launch.sh, mod/)
    steam: Any  # tools/steam_paths.py (Steam libraries, manifests, offline state)
    bg3le: Any  # tools/bg3le_buildinfo.py (the stale-build check)
    home: Path = field(default_factory=Path.home)
    env: dict = field(default_factory=lambda: dict(os.environ))
    run: object = _run  # (cmd) -> (returncode, output)
    which: object = shutil.which
    meminfo: Path = Path("/proc/meminfo")


def read_vdf(ctx: Ctx, path: Path) -> dict:
    try:
        return ctx.steam.parse_vdf(path.read_text(errors="replace"))
    except OSError:
        return {}


def pinned_build(repo: Path) -> str:
    try:
        m = re.search(r"^WANT_BUILD=(\d+)", (repo / "tools/dev/first_launch.sh").read_text(), re.MULTILINE)
    except OSError:
        m = None
    return m.group(1) if m else PINNED_BUILD


def app_state(ctx: Ctx) -> tuple[Path, dict]:
    m = ctx.steam.manifest(ctx.env, ctx.home)
    return m, read_vdf(ctx, m).get("AppState", {})


def check_install(ctx: Ctx) -> list[tuple[str, str, str]]:
    m, st = app_state(ctx)
    if not st:
        return [
            (
                FAIL,
                "install",
                (
                    f"no Steam manifest at {m}: BG3 (app {ctx.steam.GAME_APPID}) isn't installed "
                    "through Steam in any library libraryfolders.vdf lists"
                ),
            )
        ]
    flags = int(st.get("StateFlags", "0") or 0)
    build, target = st.get("buildid", ""), st.get("TargetBuildID", "")
    if not flags & STATE_FULLY_INSTALLED or flags & STATE_UPDATE_RUNNING:
        return [(FAIL, "install", f"not fully installed (StateFlags {flags}): let Steam finish ({m})")]
    if flags & STATE_UPDATE_REQUIRED or (target not in ("", "0") and target != build):
        return [
            (
                WARN,
                "install",
                (
                    f"an update is pending (StateFlags {flags}, buildid {build}, TargetBuildID "
                    f"{target}): Steam will change the build when it next updates"
                ),
            )
        ]
    return [(PASS, "install", f"installed through Steam, fully ({m})")]


def is_elf(path: Path) -> bool:
    try:
        with path.open("rb") as f:
            return f.read(4) == b"\x7fELF"
    except OSError:
        return False


def check_native(ctx: Ctx) -> list[tuple[str, str, str]]:
    game = ctx.steam.game_dir(ctx.env, ctx.home)
    binary = game / "bin" / "bg3"
    out = []
    if is_elf(binary):
        out.append((PASS, "native", f"native Linux build: {binary} is an ELF"))
    elif binary.exists():
        out.append((FAIL, "native", f"{binary} is not an ELF binary: not the native Linux build"))
    else:
        exe = [p.name for p in (game / "bin").glob("*.exe")] if (game / "bin").is_dir() else []
        why = f" (found {', '.join(exe[:3])}: a Proton/Windows install)" if exe else ""
        out.append(
            (
                FAIL,
                "native",
                f"no {binary}{why}: install the native Linux build (no Proton compatibility tool forced for BG3)",
            )
        )
    sniper = ctx.steam.sniper_dir(ctx.env, ctx.home)
    if (sniper / "run").is_file():
        out.append((PASS, "sniper", f"Steam Linux Runtime 3.0 (sniper): {sniper}"))
    else:
        out.append((FAIL, "sniper", f"no {sniper}/run: install 'Steam Linux Runtime 3.0 (sniper)' in Steam"))
    return out


def series_build(ctx: Ctx, series: str) -> tuple[str | None, str]:
    path = Path(ctx.env.get("BG3AI_RUNTIME", ctx.repo / "runtime")) / "sessions" / series / "series.json"
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None, f"no readable {path}"
    build = (data.get("pins") or {}).get("game_build")
    return build, (str(path) if build else f"{path} has no pins.game_build (a series from before pins)")


def check_build(ctx: Ctx, series: str | None = None) -> list[tuple[str, str, str]]:
    _, st = app_state(ctx)
    build, want = st.get("buildid", ""), pinned_build(ctx.repo)
    if not build:
        return [(FAIL, "build", "no buildid (no manifest)")]
    out = [
        (PASS, "build", f"buildid {build} = the pinned build (tools/dev/first_launch.sh)")
        if build == want
        else (
            FAIL,
            "build",
            (
                f"buildid {build}, pinned {want}: bg3le was verified on {want} only, and runs on another "
                "build don't compare with published ones"
            ),
        )
    ]
    if series:
        frozen, where = series_build(ctx, series)
        if frozen is None:
            out.append((WARN, "build", f"series {series}: {where}"))
        elif frozen == build:
            out.append((PASS, "build", f"series {series} was frozen on {frozen} too"))
        else:
            out.append(
                (
                    FAIL,
                    "build",
                    (
                        f"series {series} was frozen on {frozen}, installed {build}: the runner "
                        "refuses the next run (version drift)"
                    ),
                )
            )
    return out


def login_users(ctx: Ctx) -> dict:
    for root in ctx.steam.steam_roots(ctx.home):
        data = read_vdf(ctx, root / "config" / "loginusers.vdf")
        if data:
            return data.get("users") or {}
    return {}


def wants_offline(ctx: Ctx) -> bool | None:
    """loginusers.vdf WantsOfflineMode of the most recent user (the only one if none is marked); None: unknown."""
    users = [u for u in login_users(ctx).values() if isinstance(u, dict)]
    if not users:
        return None
    user = next((u for u in users if u.get("MostRecent") == "1"), users[0] if len(users) == 1 else None)
    return None if user is None else user.get("WantsOfflineMode") == "1"


def check_updates(ctx: Ctx) -> list[tuple[str, str, str]]:
    _, st = app_state(ctx)
    behavior = st.get("AutoUpdateBehavior", "0")
    meaning = UPDATE_BEHAVIOR.get(behavior, "unknown value")
    offline, why = ctx.steam.steam_offline(ctx.home)
    wants = wants_offline(ctx)
    setting = (
        f"AutoUpdateBehavior {behavior} ({meaning}); loginusers.vdf WantsOfflineMode "
        + {True: "1", False: "0", None: "unknown"}[wants]
    )
    if offline:
        return [
            (
                PASS,
                "updates",
                f"Steam's last login was offline, so it downloads no update while it stays offline; {setting}",
            )
        ]
    if behavior == "1":
        risk = (
            "Steam updates the game when it is launched through Steam (the runner starts bin/bg3 directly, "
            "which doesn't), and on going offline->online it may still queue one"
        )
    else:
        risk = "Steam online may update the game between (or during) the runs of a series"
    return [
        (
            WARN,
            "updates",
            (
                f"Steam is not offline ({why}): {risk}; {setting}. Start Steam in offline mode for "
                "a series (the runner refuses to launch otherwise where the machine requires it: "
                "~/.config/bg3ai/require-steam-offline or BG3AI_REQUIRE_STEAM_OFFLINE=1)"
            ),
        )
    ]


def check_steam_running(ctx: Ctx) -> list[tuple[str, str, str]]:
    rc, out = ctx.run(["pgrep", "-x", "steam"])
    if rc == 0 and out.strip():
        return [(PASS, "steam", "a Steam client is running")]
    return [
        (
            WARN,
            "steam",
            "no Steam client running: start it (offline) before a run; the runner won't launch the game without one",
        )
    ]


def check_bg3le(ctx: Ctx) -> list[tuple[str, str, str]]:
    lib = Path(ctx.env.get("BG3AI_BG3LE_LIB", ctx.repo / "build/bg3le/dist/libbg3le.so"))
    if not lib.is_file():
        return [(FAIL, "bg3le", f"no {lib}: run {ctx.bg3le.REBUILD} (needs ~10 GB RAM, never while the game runs)")]
    bad = ctx.bg3le.problems(lib.parent, ctx.repo / "patches" / "bg3le")
    if bad:
        return [(FAIL, "bg3le", f"stale build {lib.parent}: " + "; ".join(bad) + f"; rebuild: {ctx.bg3le.REBUILD}")]
    return [(PASS, "bg3le", f"{lib} built with every patches/bg3le patch")]


def mod_version(meta: str) -> str | None:
    """As bg3harness.pins.mod_version (meta.lsx Version64, Larian's bit layout); copied to keep this stdlib-only."""
    m = re.search(r'id="Version64"[^>]*value="(\d+)"', meta)
    if not m:
        return None
    v = int(m.group(1))
    return f"{v >> 55}.{(v >> 47) & 0xFF}.{(v >> 31) & 0xFFFF}.{v & 0x7FFFFFFF}"


def check_mod(ctx: Ctx) -> list[tuple[str, str, str]]:
    roots = [Path(p) for p in ctx.env.get("BG3AI_MOD_PATH", "").split(":") if p] or [ctx.repo / "mod"]
    for root in roots:
        mod = root / "Mods" / "BG3AI"
        meta = mod / "meta.lsx"
        if meta.is_file() and (mod / "ScriptExtender").is_dir():
            try:
                ver = mod_version(meta.read_text(errors="replace"))
            except OSError:
                ver = None
            return [(PASS, "mod", f"{mod} (version {ver or '?'}), loaded loose by bg3le via BG3LE_MOD_PATH")]
    return [(FAIL, "mod", "no Mods/BG3AI/meta.lsx + ScriptExtender/ under " + ":".join(map(str, roots)))]


def check_claude(ctx: Ctx) -> list[tuple[str, str, str]]:
    path = ctx.which("claude")
    if not path:
        return [(FAIL, "claude", "no claude CLI on PATH (plays the sessions; also the keeper and the judge)")]
    rc, out = ctx.run([path, "--version"])
    if rc != 0:
        return [(FAIL, "claude", f"{path} --version failed: {out[:120]}")]
    return [(PASS, "claude", f"{path}: {out.splitlines()[0] if out else '?'}")]


def check_docker(ctx: Ctx) -> list[tuple[str, str, str]]:
    path = ctx.which("docker")
    if not path:
        return [(WARN, "docker", "no docker: needed only for non-Claude clients (tools/container/)")]
    rc, out = ctx.run([path, "version", "--format", "{{.Server.Version}}"])
    if rc != 0:
        return [
            (
                WARN,
                "docker",
                f"docker present but its daemon isn't reachable ({out[:100]}): needed only for tools/container/",
            )
        ]
    return [(PASS, "docker", f"docker server {out}")]


def parse_size(s: str) -> int | None:
    """systemd-style size ('10G', '512M', bytes) -> bytes; None for 'infinity' or junk."""
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([KMGT]?)\s*", s or "", re.IGNORECASE)
    if not m:
        return None
    return int(float(m.group(1)) * 1024 ** "_KMGT".index(m.group(2).upper() or "_"))


def meminfo(path: Path) -> dict[str, int]:
    out = {}
    try:
        for ln in path.read_text().splitlines():
            k, _, v = ln.partition(":")
            if v.strip().endswith("kB"):
                out[k] = int(v.split()[0]) * 1024
    except OSError:
        pass
    return out


def check_ram(ctx: Ctx) -> list[tuple[str, str, str]]:
    cap_s = ctx.env.get("BG3AI_GAME_MEMORY_MAX", DEFAULT_GAME_CAP)
    cap = parse_size(cap_s) or parse_size(DEFAULT_GAME_CAP)
    mi = meminfo(ctx.meminfo)
    total, avail = mi.get("MemTotal"), mi.get("MemAvailable")
    if not total:
        return [(WARN, "ram", f"can't read {ctx.meminfo}")]
    gib = lambda b: f"{b / 2**30:.1f} GiB"
    if total < cap:
        return [
            (
                FAIL,
                "ram",
                (
                    f"{gib(total)} total < the game's {cap_s} cap (it peaks ~6.3 GB; the cap is what a "
                    "fair series allows it)"
                ),
            )
        ]
    if avail is not None and avail < cap:
        return [
            (
                WARN,
                "ram",
                (
                    f"{gib(avail)} available of {gib(total)} < the game's {cap_s} cap: close other "
                    "programs before a series (the game peaks ~6.3 GB, an OOM ends the run as a harness "
                    "failure)"
                ),
            )
        ]
    return [(PASS, "ram", f"{gib(avail or total)} available of {gib(total)}, game cap {cap_s}")]


def check_vram(ctx: Ctx) -> list[tuple[str, str, str]]:
    smi = ctx.which("nvidia-smi")
    if not smi:
        return [(WARN, "vram", "no nvidia-smi: VRAM unknown (only NVIDIA was tested; BG3 needs a 4 GB card)")]
    rc, out = ctx.run([smi, "--query-gpu=name,memory.total,memory.free", "--format=csv,noheader,nounits"])
    rows = [[c.strip() for c in ln.split(",")] for ln in out.splitlines() if ln.count(",") == 2] if rc == 0 else []
    if not rows:
        return [(WARN, "vram", f"nvidia-smi gave no GPU ({out[:100]})")]
    name, total, free = max(rows, key=lambda r: int(r[1]) if r[1].isdigit() else 0)
    if not total.isdigit():
        return [(WARN, "vram", f"{name}: can't read its memory ({total})")]
    if int(total) < MIN_VRAM_MIB:
        return [(WARN, "vram", f"{name}: {total} MiB < BG3's 4 GB minimum")]
    note = " (BG3's minimum)" if int(total) < 6000 else ""
    return [(PASS, "vram", f"{name}: {total} MiB{note}, {free} MiB free now")]


def preflight(ctx: Ctx, series: str | None = None) -> list[tuple[str, str, str]]:
    out = []
    for check in (
        check_install,
        check_native,
        lambda c: check_build(c, series),
        check_updates,
        check_steam_running,
        check_bg3le,
        check_mod,
        check_claude,
        check_docker,
        check_ram,
        check_vram,
    ):
        try:
            out += check(ctx)
        except Exception as e:  # noqa: BLE001  (one broken check must not hide the others)
            out.append((FAIL, getattr(check, "__name__", "check").removeprefix("check_"), f"check crashed: {e!r}"))
    return out


def main(argv: list[str] | None, ctx: Ctx) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--series", help="also compare the installed build with this series' frozen pins")
    a = ap.parse_args(argv)
    lines = preflight(ctx, a.series)
    width = max(len(n) for _, n, _ in lines)
    for status, name, detail in lines:
        print(f"{status}  {name:<{width}}  {detail}")
    fails = sum(s == FAIL for s, _, _ in lines)
    warns = sum(s == WARN for s, _, _ in lines)
    print(f"\n{'NOT READY' if fails else 'READY'}: {fails} FAIL, {warns} WARN")
    return 1 if fails else 0

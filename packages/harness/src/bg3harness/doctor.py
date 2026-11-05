"""Can a new machine run the harness? Every check a stranger's machine needs, each failure with the fix. Stdlib only and
read-only but for one file: it reads files and runs version queries (and 0.1 s ffmpeg test encodes), never installs
anything, and writes only the bench recording's encoder setting, once, when none is set (check_record).

    python3 tools/doctor.py [--json] [--claude-probe]   # or: uv run python -m bg3harness.doctor [...]

`doctor()` returns the checks as `Check(name, status, detail, fix)` (the bench panel calls it and `report()`); the
CLI prints one PASS / WARN / FAIL line per check with its fix under it, and exits 1 on any FAIL.

The game, Steam, bg3le, the mod, the clients, RAM and VRAM are bg3harness.preflight's checks (the bench preflight);
this adds what a first install misses:
  bg3le      as preflight's, and a downloaded prebuilt (tools/bg3le_prebuilt.py) is verified against its manifest
  screenshot VK_LAYER_MESA_screenshot (the agent's screenshots go through it; runner/launch_game.sh enables it)
  shim       the SDL input shim build/bg3sdl/libbg3sdl.so ($BG3AI_INPUT_LIB): the agent's clicks and keys
  display    DISPLAY / XAUTHORITY the game can open a window with, as the launcher fills them (bg3harness.display_env)
  ffmpeg     ffmpeg with libx264 (the evidence video), and the bench recording's encoder encoding a test frame
             ($BG3AI_BENCH_RECORD_ENCODER, else ~/.config/bg3ai/record-encoder; with neither, the first of h264_nvenc
             and libx264 that encodes is written there)
  disk       free space for the runtime dir: recordings take 1-2 GB per hour
  uv/python  uv on PATH and a Python >= 3.14 it can use (the packages require it)
  node/pi    Node.js >= 22.19 and tools/pi's pinned pi (only for the non-Claude clients: WARN, never FAIL)
and, only with --claude-probe (it spends about $0.005 and needs a logged-in claude):
  claude-probe  the real play command (clients.ClaudeClient, same flags and --settings) with haiku in play/, asked
             to call mcp__bg3__session_clock (it answers without the game): FAIL when claude is missing or not
             logged in, the bg3 MCP server doesn't connect, Claude Code denies the call, the call errors or it times
             out. preflight's claude check only finds the binary: #101's reporter passed it, then every bg3 call
             was refused.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, cast

from bg3harness import bg3le_buildinfo, bg3le_prebuilt, clients, display_env, preflight, steam_paths
from bg3harness import recorder as hrec
from bg3harness.preflight import FAIL, PASS, WARN

MIN_PYTHON = (3, 14)
MIN_NODE = (22, 19)  # pi 1.3.0's engines (the web apps' Vite needs 20.19+ or 22.12+)
NODE = ".".join(map(str, MIN_NODE))
DISK_WARN_GB, DISK_FAIL_GB = 20, 5
LAYER = "VkLayer_MESA_screenshot.json"
RECORD_ENCODER_ENV = hrec.ENCODER_ENV  # bg3harness.runner.run_host reads the same variable
RECORD_PROBE = ("h264_nvenc", "libx264")  # the encoder check_record writes when none is set: the GPU's first
PROBE = "claude-probe"
PROBE_MODEL = "haiku"
PROBE_TOOL = "session_clock"  # answers without the game (runs.GAME_FREE)
PROBE_TIMEOUT_S = 60.0
PROBE_PROMPT = f"Call the {clients.MCP_PREFIX}{PROBE_TOOL} tool once, then reply with what it returned."


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
    build = "native/bg3sdl/build.sh after tools/build_bg3le.sh (it compiles against that build's sysroot, in Docker)"
    url, _ = bg3le_prebuilt.config(ctx.env)
    fix = f"python3 {bg3le_buildinfo.FETCH} (the prebuilt carries the shim), or {build}" if url else build
    return Check("shim", FAIL, f"no input shim at {lib} (the agent's clicks and keys go through it)", fix)


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
    checks.append(check_record(ctx, ffmpeg, encoders))
    return checks


def check_record(ctx: Ctx, ffmpeg: str, encoders: set[str]) -> Check:
    """The bench recording's encoder (recorder.machine_encoder) encodes a test frame. With none set, the first of
    RECORD_PROBE that does is written to the machine's setting (recorder.encoder_file), once: the bench reads it."""

    def encode(encoder: str) -> tuple[int, str]:
        return _test_encode(ctx, ffmpeg, encoder) if encoder in encoders else (1, f"ffmpeg lists no {encoder} encoder")

    encoder, where = hrec.machine_encoder(ctx.env, ctx.home)
    if encoder is None:
        tried: list[str] = []
        for encoder in RECORD_PROBE:
            rc, out = encode(encoder)
            if rc == 0:
                try:
                    Path(where).parent.mkdir(parents=True, exist_ok=True)
                    Path(where).write_text(encoder + "\n")
                except OSError as e:
                    detail = f"{encoder} encodes a test frame but {where} can't be written: {e}"
                    return Check("record", FAIL, detail, f"set {RECORD_ENCODER_ENV}={encoder}, or make it writable")
                return Check("record", PASS, f"{encoder} encodes a test frame: wrote it to {where} (bench recordings)")
            tried.append(f"{encoder}: {_last_line(out, rc)}")
        fix = "install the NVIDIA driver with NVENC, or an ffmpeg with libx264 (apt install ffmpeg)"
        return Check("record", FAIL, f"no recording encoder encodes here ({'; '.join(tried)})", fix)
    rc, out = encode(encoder)
    if rc == 0:
        return Check("record", PASS, f"the bench recording's encoder {encoder} ({where}) encodes a test frame")
    fix = (
        f"set {RECORD_ENCODER_ENV} to an encoder that works here (h264_nvenc or libx264)"
        if where == RECORD_ENCODER_ENV
        else f"delete {where} and rerun the doctor (it writes the encoder that works), or write one there"
    )
    return Check("record", FAIL, f"{encoder} ({where}) can't encode here: {_last_line(out, rc)}", fix)


def _last_line(out: str, rc: int) -> str:
    return out.strip().splitlines()[-1][:160] if out.strip() else f"exit {rc}"


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
    why = "only for the non-Claude clients (pi) and to build the web apps (the bench panel, the playthrough app)"
    node = ctx.which("node")
    if not node:
        return [Check("node", WARN, f"no node on PATH: {why}", f"install Node.js >= {NODE}")]
    rc, out = ctx.run([node, "--version"])
    ver = _version(out) if rc == 0 else ()
    if not ver or ver < MIN_NODE:
        return [Check("node", WARN, f"node {out.strip() or '?'} < {NODE}: {why}", f"install Node.js >= {NODE}")]
    checks = [Check("node", PASS, f"node {out.strip()}")]
    pi = ctx.repo / "tools/pi/node_modules/.bin/pi"
    if pi.exists():
        checks.append(Check("pi", PASS, f"the pinned pi: {pi}"))
    else:
        checks.append(
            Check("pi", WARN, f"no {pi}: only for the non-Claude clients", "cd tools/pi && npm ci --ignore-scripts")
        )
    return checks


def _stderr(err: str) -> str:
    one = " ".join(err.split())
    return f" (claude's stderr: {one[-400:]})" if one else " (claude's stderr is empty)"


def check_claude_probe(ctx: Ctx, timeout: float = PROBE_TIMEOUT_S) -> Check:
    """--claude-probe: one real play command (ClaudeClient().command, the runner's cwd play/ and client env) with
    haiku, asked to call mcp__bg3__session_clock. Its stream decides; the fix line quotes claude's stderr."""
    binary = ctx.which("claude")
    if not binary:
        return Check(PROBE, FAIL, "no claude on PATH", PREFLIGHT_FIXES["claude"](ctx, ""))
    play = ctx.repo / "play"
    client = clients.ClaudeClient(
        mcp_config=play / ".mcp.json", binary=binary, settings=clients.play_settings(play / ".claude/settings.json")
    )
    req = clients.Request(prompt=PROBE_PROMPT, model=PROBE_MODEL, session_dir=play, session=str(uuid.uuid4()))
    start = time.monotonic()
    try:
        proc = subprocess.Popen(
            client.command(req), cwd=play, env={**ctx.env, **client.env(req)}, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True,
        )  # fmt: skip
    except OSError as e:
        return Check(PROBE, FAIL, f"can't start {binary}: {e}", PREFLIGHT_FIXES["claude"](ctx, ""))
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)  # claude and the MCP server it started
        out, err = proc.communicate()
        return Check(
            PROBE, FAIL, f"claude didn't finish in {timeout:.0f} s",
            "run the command by hand in play/ (claude -p ... --mcp-config .mcp.json) to see where it hangs: a login "
            "prompt, or the bg3 MCP server not starting (uv sync in bridge/)" + _stderr(err),
        )  # fmt: skip
    return judge_probe(client, out, err, proc.returncode, time.monotonic() - start)


def judge_probe(client: clients.ClaudeClient, out: str, err: str, rc: int, secs: float) -> Check:
    mcp: list[str] | None = None
    calls: set[str] = set()  # the session_clock calls' ids: their results are the probe's answer
    denials: list[Any] = []  # the result event's permission_denials
    errors: list[str] = []  # every error tool_result's text
    results: list[str] = []  # the session_clock calls' results
    end: clients.Event | None = None
    for line in out.splitlines():
        for ev in client.parse(line):
            if ev.kind == "init":
                mcp = ev.mcp or []
            elif ev.kind == "tool_call" and ev.tool == PROBE_TOOL:
                calls.add(ev.call_id or "")
            elif ev.kind == "tool_result" and ev.is_error:  # any tool's: ToolSearch's too
                errors.append(ev.text or "")
            elif ev.kind == "tool_result" and ev.call_id in calls:
                results.append(ev.text or "")
            elif ev.kind == "result":
                end = ev
                raw: dict[str, Any] = json.loads(line)  # parse() read it: a JSON object
                denials = raw.get("permission_denials") or []
    said = f": {' '.join(end.text.split())[:300]}" if end and end.text else ""

    def fail(detail: str, fix: str) -> Check:
        return Check(PROBE, FAIL, detail, fix + _stderr(err))

    if mcp is None or (end and end.is_error and "login" in end.text.lower()):
        login = "log in: run `claude` once and /login (CLAUDE_CONFIG_DIR picks the account), then rerun"
        if mcp is None:
            return fail(f"claude exited {rc} without its init event{said}", login)
        return fail(f"claude isn't logged in{said}", login)
    if "bg3:connected" not in mcp:
        return fail(
            f"the bg3 MCP server didn't connect (init: {mcp})",
            "in play/, `uv run --project ../bridge bg3ai-mcp` must start and wait on stdin: fix what it prints "
            "(uv sync in bridge/)",
        )
    if denials:
        names = sorted({str(cast(dict[str, Any], d).get("tool_name") if isinstance(d, dict) else d) for d in denials})
        return fail(
            f"Claude Code denied {', '.join(names)}: every bg3 call of a play session would be refused",
            "play/.claude/settings.json must allow mcp__bg3__*; ClaudeClient passes it as --settings (#101): "
            "update the checkout",
        )
    if errors:
        return fail(f"a tool call failed: {' '.join(errors[0].split())[:300]}", "see the error")
    if not calls or not results:
        return fail(
            f"{PROBE_MODEL} never called {clients.MCP_PREFIX}{PROBE_TOOL} "
            f"(calls {len(calls)}, results {len(results)}){said}",
            "rerun; if it repeats, claude doesn't see the bg3 tools",
        )
    if rc != 0 or (end and end.is_error) or end is None:
        return fail(f"claude exited {rc}{said}", "see the error")
    cost = f"${end.cost_usd:.4f}" if end.cost_usd is not None else "$?"
    shown = " ".join(results[0].split())[:120]
    return Check(PROBE, PASS, f"claude {PROBE_MODEL} in play/ called {PROBE_TOOL}: {shown!r} ({cost}, {secs:.0f} s)")


def doctor(ctx: Ctx | None = None, claude_probe: bool = False) -> list[Check]:
    """Every check, in order; a check that crashes is a FAIL of its own, never an abort. claude_probe adds
    check_claude_probe (it spends money: the CLI's --claude-probe only)."""
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
    if claude_probe:
        steps.append((PROBE, check_claude_probe))
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
    ap.add_argument(
        "--claude-probe",
        action="store_true",
        help="also run one real play command (claude haiku in play/ calling mcp__bg3__session_clock): spends about "
        "$0.005 and needs a logged-in claude",
    )
    a = ap.parse_args(argv)
    checks = doctor(ctx, claude_probe=a.claude_probe)
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

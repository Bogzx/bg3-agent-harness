#!/bin/bash
# One command from "BG3 finished downloading" to "ready to play". It stops at the first failure and says why.
#
#   tools/dev/first_launch.sh [--attach] [--skip-shim] [-- <runner/launch_game.sh args, e.g. --continue>]
#
#   --attach      the game is already running (started by launch_game.sh): skip builds and launch, run the checks
#   --skip-shim   don't rebuild or test the input shim
#
# Steps:
#   1 install     the Steam manifest says fully installed; buildid is the one bg3le was verified on (25605617;
#                 another build only warns: bg3le may still resolve, step 5 checks its log)
#   2 builds      build/bg3le/dist/libbg3le.so exists (this script never builds bg3le: ~10 GB RAM, and never
#                 while the game runs); rebuild the input shim + sdltest against the game's libSDL2 and run
#                 native/bg3sdl/test/test_shim.py --chain, then --sniper
#   3 launch      a usable display (unset DISPLAY/XAUTHORITY from the desktop's Xwayland, else FAIL), then
#                 runner/launch_game.sh in the background (PRIME offload to NVIDIA when present)
#   4 bg3le       wait for the Lua debug server on 127.0.0.1:$BG3LE_DEBUG_PORT, then sys.info from the mod
#   5 health      no "WARNING: resolve:" in bg3le's log; the game is on the NVIDIA GPU (nvidia-smi); the game runs
#                 in its own systemd scope bg3ai-game-<stamp>.scope (launch_game.sh: OOMPolicy=continue, MemoryMax
#                 10G), so an OOM kills only the game, never the agents' unit (2026-10-10: one did)
#   6 screen      one frame through the screenshot layer (bridge/src/bg3ai/screen.py)
# Then it prints what to do next. The game is left running.
set -euo pipefail
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
RUNTIME="${BG3AI_RUNTIME:-$REPO/runtime}"
PORT="${BG3LE_DEBUG_PORT:-9998}"
WANT_BUILD=25605617
PORT_TIMEOUT="${FIRST_LAUNCH_PORT_TIMEOUT:-300}"
MOD_TIMEOUT="${FIRST_LAUNCH_MOD_TIMEOUT:-180}"

attach=0
shim=1
launch_args=()
while [ $# -gt 0 ]; do
    case "$1" in
        --attach) attach=1; shift ;;
        --skip-shim) shim=0; shift ;;
        --) shift; launch_args=("$@"); break ;;
        -h|--help) sed -n '2,/^set -euo/p' "$0" | sed '$d; s/^# \{0,1\}//'; exit 0 ;;
        *) echo "unknown option '$1' (see --help)" >&2; exit 2 ;;
    esac
done

step() { printf '\n== %s\n' "$*"; }
ok() { echo "  ok: $*"; }
warn() { echo "  WARN: $*"; }
die() { echo "  FAIL: $*" >&2; exit 1; }
port_up() { python3 -c "import socket,sys; s=socket.socket(); s.settimeout(1); sys.exit(s.connect_ex(('127.0.0.1', $PORT)))"; }
luaeval() {  # luaeval <timeout> <lua>; prints the result
    (cd "$REPO/bridge" && PYTHONPATH=src python3 -m bg3ai.luadebug --port "$PORT" --timeout "$1" "$2")
}

# ---------------------------------------------------------------- 1 install
step "1 install"
GAME="$(python3 "$REPO/tools/steam_paths.py" game)"
echo "  game: $GAME"
python3 -I - "$REPO/tools" "$(python3 "$REPO/tools/steam_paths.py" manifest)" "$WANT_BUILD" <<'EOF' || exit 1
import sys
sys.path.insert(0, sys.argv[1])
from steam_paths import parse_vdf
path, want = sys.argv[2], sys.argv[3]
try:
    st = parse_vdf(open(path, errors="replace").read()).get("AppState", {})
except OSError:
    sys.exit(f"  FAIL: no Steam manifest at {path}: is BG3 installed in a Steam library?")
flags = int(st.get("StateFlags", "0") or 0)
have, total = int(st.get("BytesDownloaded", "0") or 0), int(st.get("BytesToDownload", "0") or 0)
if flags & 4 == 0 or flags & 1024 or (total and have < total):
    sys.exit(f"  FAIL: still downloading/updating (StateFlags {flags}, {have / 2**30:.0f}/{total / 2**30:.0f} GiB);"
             " wait for Steam to finish")
build = st.get("buildid", "")
if build == want:
    print(f"  ok: fully installed, buildid {build} (the build bg3le was verified on)")
else:
    print(f"  WARN: buildid {build}, not {want}: bg3le may fail to resolve symbols (checked in step 5)")
EOF
[ -x "$GAME/bin/bg3" ] || die "no native binary at $GAME/bin/bg3 (is the Linux build installed, not Proton?)"
SNIPER="$(python3 "$REPO/tools/steam_paths.py" sniper)"
[ -x "$SNIPER/run" ] || die "no sniper runtime at $SNIPER: install 'Steam Linux Runtime 3.0 (sniper)' in Steam"
ok "sniper runtime: $SNIPER"
[ -f /usr/share/vulkan/explicit_layer.d/VkLayer_MESA_screenshot.json ] ||
    die "no VK_LAYER_MESA_screenshot: install mesa-vulkan-drivers >= 26.1 (trixie-backports)"
ok "screenshot layer installed"

# ---------------------------------------------------------------- 2 builds
step "2 builds"
mkdir -p "$RUNTIME"
[ -f "$REPO/build/bg3le/dist/libbg3le.so" ] ||
    die "no build/bg3le/dist/libbg3le.so: run tools/build_bg3le.sh first (not while the game runs)"
ok "libbg3le.so: $(grep -m1 -i commit "$REPO/build/bg3le/dist/BUILDINFO" 2>/dev/null || echo present)"
if [ "$attach" = 1 ] || [ "$shim" = 0 ]; then
    [ -f "$REPO/build/bg3sdl/libbg3sdl.so" ] || die "no build/bg3sdl/libbg3sdl.so: run native/bg3sdl/build.sh"
    ok "input shim present (not rebuilt)"
else
    pgrep -x bg3 >/dev/null && die "a bg3 process is already running: stop it, or pass --attach"
    "$REPO/native/bg3sdl/build.sh" > "$RUNTIME/first_launch-bg3sdl.log" 2>&1 ||
        { tail -20 "$RUNTIME/first_launch-bg3sdl.log" >&2; die "native/bg3sdl/build.sh failed"; }
    grep -q "against $GAME/bin/libSDL2.so" "$RUNTIME/first_launch-bg3sdl.log" ||
        warn "sdltest was not linked against the game's libSDL2.so (see $RUNTIME/first_launch-bg3sdl.log)"
    for mode in --chain "--chain --sniper"; do
        # shellcheck disable=SC2086
        out="$(python3 "$REPO/native/bg3sdl/test/test_shim.py" $mode 2>&1)" ||
            { echo "$out" | grep -E 'FAIL|rror' | head -10 >&2; die "test_shim.py $mode failed"; }
        ok "test_shim.py $mode: $(grep -c '^PASS' <<<"$out") checks passed"
    done
fi

# ---------------------------------------------------------------- 3 launch
step "3 launch"
mkdir -p "$RUNTIME/logs"
since="$(date +%s)"
if [ "$attach" = 1 ]; then
    port_up || die "--attach, but nothing listens on 127.0.0.1:$PORT"
    ok "attaching to the running game"
else
    port_up && die "port $PORT is already in use (a game is running?): pass --attach, or stop it first"
    # The desktop's display env when this shell lacks it (ssh, a multiplexer); none usable = no launch (L8: the game
    # fell back to the AMD iGPU and dumped cores). launch_game.sh checks again (exit 5).
    display_problem=""
    display_sh="$(python3 "$REPO/runner/display_env.py" shell)" || [ -n "$display_sh" ] || display_problem="display_env.py failed"
    eval "$display_sh"
    [ -z "$display_problem" ] ||
        die "no usable display: $display_problem. Export DISPLAY/XAUTHORITY of the desktop session (runner/display_env.py)"
    ok "display: DISPLAY=$DISPLAY XAUTHORITY=${XAUTHORITY:-~/.Xauthority}"
    LAUNCH_LOG="$RUNTIME/logs/first_launch-$(date +%Y%m%d-%H%M%S).log"
    # Close inherited fds > 2 first: run under `flock runtime/live.lock`, the launcher (which lives as long as the
    # game) kept the lock fd and held the live lock for the whole game (2026-10-10 10:52, every worker blocked).
    setsid nohup bash -c 'for f in /proc/$$/fd/*; do n=${f##*/}; [ "$n" -gt 2 ] 2>/dev/null && eval "exec $n>&-" 2>/dev/null; done; exec "$@"' \
        _ "$REPO/runner/launch_game.sh" "${launch_args[@]}" > "$LAUNCH_LOG" 2>&1 < /dev/null &
    launcher=$!
    ok "runner/launch_game.sh started (launcher log: $LAUNCH_LOG)"
fi

# ---------------------------------------------------------------- 4 bg3le
step "4 bg3le"
t0=$SECONDS
until port_up; do
    if [ "$attach" = 0 ] && ! kill -0 "$launcher" 2>/dev/null; then
        cat "$LAUNCH_LOG" >&2
        glog="$(ls -t "$RUNTIME"/logs/game-*.log 2>/dev/null | head -1)"
        [ -n "$glog" ] && { echo "--- tail of $glog" >&2; tail -30 "$glog" >&2; }
        die "the game exited before bg3le's debug server came up"
    fi
    [ $((SECONDS - t0)) -lt "$PORT_TIMEOUT" ] ||
        die "no debug server on 127.0.0.1:$PORT after ${PORT_TIMEOUT}s (is bg3le injected? see $RUNTIME/logs/bg3le-*.log.*)"
    sleep 2
done
ok "debug server up after $((SECONDS - t0))s"
t0=$SECONDS
sysinfo() {  # one line summary of sys.info, or exit 1
    python3 -c 'import json,sys
r = json.loads(sys.stdin.read())
if not r.get("ok"): sys.exit(1)
i = r["result"]
g = lambda k: i.get(k, "?")
print("mod %s, bg3le %s, game %s, state %s, %d ops (%s context)" % (g("version"), g("extender_version"),
      g("game_version"), g("game_state"), len(i.get("ops") or []), g("context")))'
}
until info="$(luaeval 10 'return Mods.BG3AI.BG3AI.rpc([[{"op":"sys.info"}]])' 2>&1)" && summary="$(sysinfo <<<"$info" 2>/dev/null)"; do
    [ $((SECONDS - t0)) -lt "$MOD_TIMEOUT" ] ||
        die "the BG3AI mod didn't answer sys.info within ${MOD_TIMEOUT}s; last reply: ${info:0:300}"
    sleep 3
done
ok "sys.info: $summary"

# ---------------------------------------------------------------- 5 health
step "5 health"
blog="$(find "$RUNTIME/logs" -name 'bg3le-*.log.*' -newermt "@$((since - 5))" 2>/dev/null | head -50 || true)"
[ "$attach" = 1 ] && blog="$(ls -t "$RUNTIME"/logs/bg3le-*.log.* 2>/dev/null | head -3 || true)"  # || true: ls gets SIGPIPE (pipefail, set -e)
if [ -z "$blog" ]; then
    warn "no bg3le log found under $RUNTIME/logs"
elif n="$(cat $blog | grep -c 'WARNING: resolve:' || true)"; [ "$n" -gt 0 ]; then
    warn "$n 'WARNING: resolve:' lines in bg3le's log (game build differs?); first ones:"
    cat $blog | grep -m5 'WARNING: resolve:' | sed 's/^/    /'
else
    ok "bg3le resolved everything ($(echo $blog | wc -w) log file(s))"
fi
if command -v nvidia-smi >/dev/null; then
    if nvidia-smi 2>/dev/null | grep -q 'bin/bg3\|[[:space:]]bg3[[:space:]]\|/bg3$'; then
        ok "the game is on the NVIDIA GPU"
    else
        warn "bg3 isn't in nvidia-smi's process list: it may be rendering on the iGPU (check launch_game's PRIME line;"
        echo "        try --env MESA_VK_DEVICE_SELECT=10de:* or --env DRI_PRIME=1)"
    fi
fi
gpid="$(pgrep -x bg3 | head -1 || true)"
if [ -z "$gpid" ]; then
    warn "no bg3 process to check the cgroup of"
elif cg="$(cat /proc/$gpid/cgroup 2>/dev/null)"; [[ "$cg" == *bg3ai-game-*.scope* ]]; then
    ok "the game has its own cgroup (${cg##*/}): an OOM kills only the game"
else
    warn "bg3 shares a cgroup with ${cg##*/}: an OOM there can stop that whole unit (relaunch via launch_game.sh)"
fi

# ---------------------------------------------------------------- 6 screen
step "6 screen"
PYTHONPATH="$REPO/bridge/src" python3 - "$RUNTIME/shots" <<'EOF' || die "no frame from the screenshot layer (is the window minimized? see the game log for mesa-screenshot errors)"
import sys, time
from pathlib import Path
from bg3ai.screen import ScreenCapture
t = time.monotonic()
png = ScreenCapture(Path(sys.argv[1])).capture_png(timeout=10)
data = png.read_bytes()
w, h = int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
print(f"  ok: {w}x{h} frame in {time.monotonic() - t:.2f}s: {png}")
EOF

cat <<EOF

== READY. The game is running (stop it: kill -9 \$(cat $RUNTIME/game.pid); it ignores SIGTERM). Next:
  1. Load or start a game:
       existing Claude save: python3 runner/play.py run --playthrough claude-run-1 --save-owner Claude --dry-run
                             (loads the playthrough's save, prints the claude command instead of playing)
       new game:             python3 tools/dev/test_new_game.py   (New Game -> character creation; see its docstring)
  2. With a save loaded, the live checks (each prints PASS/WARN/FAIL):
       uv run --project bridge python tools/dev/smoke.py
       uv run --project bridge python tools/dev/check_vis.py
EOF
for c in check_act check_se; do
    if [ -f "$REPO/tools/dev/$c.py" ]; then
        echo "       uv run --project bridge python tools/dev/$c.py"
    else
        echo "       uv run --project bridge python tools/dev/$c.py   (not written yet)"
    fi
done
cat <<EOF
  3. A supervised session (docs/SETUP.md §5): BG3AI_SAVE_PREFIX=Claude- python3 runner/play.py run \\
       --playthrough claude-run-1 --save-owner Claude --session-minutes 30 --effort medium
     with the dashboard: uv run --project bridge python runner/dashboard/serve.py --port 8765
EOF

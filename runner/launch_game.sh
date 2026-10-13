#!/bin/bash
#
# Launch the NATIVE Linux BG3 build with bg3le preloaded, inside Steam's sniper runtime container, from a script.
#
# Leaves Steam's configuration alone: no install.py, no localconfig.vdf edits, no launch options, no
# steam_appid.txt written into the game directory. The approach follows bg3le's run-native.sh:
#
#   cd <game>; SteamLinuxRuntime_sniper/run -- env LD_PRELOAD=libbg3le.so:libbg3sdl.so ./bin/bg3 <args>
#
# with the preload set on the game only (not exported to the container's own tooling). Steam must be running so
# that libsteam_api can attach. Instead of steam_appid.txt, the app id goes through SteamAppId/SteamGameId, the
# variables Steam itself sets for a game it launches.
#
# Usage:
#   runner/launch_game.sh [--load <save>] [--continue] [--env KEY=VALUE]... [--mod-path DIR]
#                         [--no-bg3le] [--no-input-shim] [--dry-run] [-- <extra game args>]
#
#   --load <save>      passes -loadSaveGame <save> to the game (the save's folder name under
#                      PlayerProfiles/Public/Savegames/Story, e.g. "pibooool-52412617951__AutoSave_33";
#                      see research/01b-bg3le-practical.md on the argument format)
#   --continue         passes -continueGame (loads the most recent save)
#   --env KEY=VALUE    extra environment for the game process (repeatable)
#   --mod-path DIR     loose bg3le mod root(s), ':'-separated, each containing Mods/<Name>/ScriptExtender/
#                      (default: $BG3AI_MOD_PATH, else <repo>/mod if it exists)
#   --no-bg3le         run the game without bg3le (control run)
#   --no-input-shim    run the game without the SDL input shim (native/bg3sdl; control socket @bg3ai-input)
#   --dry-run          print the environment and command, launch nothing
#
# Environment:
#   BG3AI_RUNTIME      runtime dir (default <repo>/runtime): logs/, shots/
#   BG3AI_LAUNCH_STAMP the <stamp> in logs/{game,bg3le}-<stamp>.log* (default: now; runner/play.py passes its own)
#   BG3_DIR            game install (BG3AI_GAME_DIR is the older name). Default: found by tools/steam_paths.py
#                      from Steam's libraryfolders.vdf, else ~/.local/share/Steam/steamapps/common/Baldurs Gate 3
#   BG3AI_SNIPER_DIR   SteamLinuxRuntime_sniper (default: its library per libraryfolders.vdf, else next to the game)
#   BG3AI_PRIME        PRIME render offload to the NVIDIA GPU: auto (default; on when an NVIDIA GPU and its
#                      Vulkan ICD are present), 1 (always), 0 (never)
#   BG3AI_BG3LE_LIB    the preload (default <repo>/build/bg3le/dist/libbg3le.so, from tools/build_bg3le.sh)
#   BG3AI_INPUT_LIB    the input shim (default <repo>/build/bg3sdl/libbg3sdl.so, from native/bg3sdl/build.sh)
#   BG3LE_DEBUG_PORT   bg3le's Lua debug server port (default 9998)
#   BG3AI_ALLOW_STEAM_ONLINE=1  launch even when Steam's last login was online (default: refuse; see below)
#   BG3AI_ALLOW_STALE_BG3LE=1   launch even when the bg3le build lacks a patches/bg3le patch (default: refuse,
#                      exit 4, also with --dry-run; tools/bg3le_buildinfo.py compares BUILDINFO next to the library)
#   BG3AI_GAME_SCOPE   1 (default): run the game in its own systemd user scope bg3ai-game-<stamp>.scope with
#                      OOMPolicy=continue and a memory cap, so an OOM kills only the game, never the caller's unit
#                      (20:48: the game hit 13.3 GB inside the multiplexer's unit, OOMPolicy=stop, and systemd killed every
#                      agent). 0: no scope (and no cap).
#   BG3AI_GAME_MEMORY_MAX / BG3AI_GAME_SWAP_MAX   the scope's MemoryMax / MemorySwapMax (default 10G / 2G; the
#                      game normally peaks at ~6.3 GB RSS)
set -euo pipefail

# Never carry a flock fd into the game's lifetime: 10:52 the game was launched from inside a
# `flock runtime/live.lock …` hold, this script inherited fd 3 on live.lock and every worker's live test waited
# behind it for the whole game session. Close any inherited *.lock fd (the game needs none of ours).
for _fd in /proc/$$/fd/*; do
    _n=${_fd##*/}
    [[ $_n =~ ^[0-9]+$ && $_n -gt 2 ]] || continue
    [[ $(readlink "$_fd" 2>/dev/null) == *.lock ]] && eval "exec $_n>&-"
done
unset _fd _n

REPO="$(cd "$(dirname "$0")/.." && pwd)"
RUNTIME="${BG3AI_RUNTIME:-$REPO/runtime}"
GAME="$(python3 "$REPO/tools/steam_paths.py" game)"
SNIPER_DIR="$(python3 "$REPO/tools/steam_paths.py" sniper)"
LIB="${BG3AI_BG3LE_LIB:-$REPO/build/bg3le/dist/libbg3le.so}"
INPUT_LIB="${BG3AI_INPUT_LIB:-$REPO/build/bg3sdl/libbg3sdl.so}"
PROFILE="$HOME/.local/share/Larian Studios/Baldur's Gate 3"
PORT="${BG3LE_DEBUG_PORT:-9998}"

die() { echo "launch_game: $*" >&2; exit 1; }

game_args=()
extra_env=()
mod_path="${BG3AI_MOD_PATH:-}"
[ -z "$mod_path" ] && [ -d "$REPO/mod" ] && mod_path="$REPO/mod"
use_bg3le=1
use_input=1
dry_run=0

while [ $# -gt 0 ]; do
    case "$1" in
        --load)
            [ $# -ge 2 ] || die "--load needs a save"
            game_args+=(-loadSaveGame "$2"); shift 2 ;;
        --continue)
            game_args+=(-continueGame); shift ;;
        --env)
            [ $# -ge 2 ] || die "--env needs KEY=VALUE"
            [[ "$2" == *=* ]] || die "--env expects KEY=VALUE, got '$2'"
            extra_env+=("$2"); shift 2 ;;
        --mod-path)
            [ $# -ge 2 ] || die "--mod-path needs a directory"
            mod_path="$2"; shift 2 ;;
        --no-bg3le)
            use_bg3le=0; shift ;;
        --no-input-shim)
            use_input=0; shift ;;
        --dry-run)
            dry_run=1; shift ;;
        --)
            shift; game_args+=("$@"); break ;;
        -h|--help)
            sed -n '2,/^set -euo/p' "$0" | sed '$d; s/^# \{0,1\}//'; exit 0 ;;
        *)
            die "unknown option '$1' (see --help)" ;;
    esac
done

[ -x "$GAME/bin/bg3" ] || die "no native game binary at $GAME/bin/bg3"
[ -x "$SNIPER_DIR/run" ] || die "no sniper runtime at $SNIPER_DIR (install 'Steam Linux Runtime 3.0 (sniper)')"
if [ "$use_bg3le" = 1 ] && [ ! -f "$LIB" ]; then
    [ "$dry_run" = 1 ] || die "no bg3le library at $LIB; run tools/build_bg3le.sh"
    echo "launch_game: warning: no bg3le library at $LIB yet" >&2
fi
# A library built before a crash patch was added (UI-1: this laptop's dist predated all nine) passes every other
# check. Its BUILDINFO's "patch:" lines must name every patches/bg3le/*.patch with the same hash.
if [ "$use_bg3le" = 1 ] && [ -f "$LIB" ]; then
    if ! stale="$(python3 "$REPO/tools/bg3le_buildinfo.py" --dist "$(dirname "$LIB")")"; then
        if [ "${BG3AI_ALLOW_STALE_BG3LE:-0}" = 1 ]; then
            echo "launch_game: WARNING !!! launching a STALE bg3le (BG3AI_ALLOW_STALE_BG3LE=1): crash fixes missing !!!" >&2
            echo "$stale" | sed 's/^/launch_game:   /' >&2
        else
            echo "$stale" | sed "1s/^/launch_game: REFUSING TO LAUNCH: /; 2,\$s/^/launch_game: /" >&2
            echo "launch_game: (BG3AI_ALLOW_STALE_BG3LE=1 launches it anyway)" >&2
            exit 4
        fi
    fi
fi
if [ "$use_input" = 1 ] && [ ! -f "$INPUT_LIB" ]; then
    [ "$dry_run" = 1 ] || die "no input shim at $INPUT_LIB; run native/bg3sdl/build.sh (or pass --no-input-shim)"
    echo "launch_game: warning: no input shim at $INPUT_LIB yet" >&2
fi
if [ "$use_input" = 1 ] && command -v ss >/dev/null 2>&1 && ss -Hxl | grep -q '@bg3ai-input '; then
    echo "launch_game: warning: @bg3ai-input is already taken; the new game's input shim will not listen" >&2
fi
if ! pgrep -x steam >/dev/null 2>&1 && ! pgrep -f 'ubuntu12_32/steam' >/dev/null 2>&1; then
    echo "launch_game: warning: Steam does not appear to be running; libsteam_api needs it" >&2
fi
# Machine 2 shares the Steam account with the operator's machine: an online launch would kick them off. Refuse
# unless Steam's last login was offline ($BG3AI_ALLOW_STEAM_ONLINE=1 on a machine where online is fine).
if [ "$dry_run" != 1 ] && [ "${BG3AI_ALLOW_STEAM_ONLINE:-0}" != 1 ]; then
    steam_state="$(python3 "$REPO/tools/steam_paths.py" offline)" \
        || die "REFUSING TO LAUNCH: Steam is not offline ($steam_state). Steam > Go Offline, or BG3AI_ALLOW_STEAM_ONLINE=1"
fi
if [ "$use_bg3le" = 1 ] && command -v ss >/dev/null 2>&1 && ss -Hltn "sport = :$PORT" | grep -q .; then
    [ "$dry_run" = 1 ] || die "port $PORT is already in use (another game instance?)"
    echo "launch_game: warning: port $PORT is already in use (another game instance?)" >&2
fi

stamp="${BG3AI_LAUNCH_STAMP:-$(date +%Y%m%d-%H%M%S)}"   # the runner passes its own, to find this launch's logs
mkdir -p "$RUNTIME/logs" "$RUNTIME/shots"
GAME_LOG="$RUNTIME/logs/game-$stamp.log"

# Environment for the game process only. `env` runs inside the container, so none of this touches the runtime's
# own launcher.
env_args=(
    SteamAppId=1086940
    SteamGameId=1086940
    # Frame capture (bridge/src/bg3ai/screen.py talks to the control socket).
    VK_LOADER_LAYERS_ENABLE=VK_LAYER_MESA_screenshot
    "VK_LAYER_MESA_SCREENSHOT_CONFIG=comms;control=bg3ai-shot;output_dir=$RUNTIME/shots;log_type=error"
)
# PRIME render offload: on a hybrid laptop the game must render on the NVIDIA GPU. The screenshot layer sits
# above any driver, so capture is unaffected (tools/dev/check_screenshot_layer.sh checks it with vkcube).
case "${BG3AI_PRIME:-auto}" in
    1) prime=1 ;;
    0) prime=0 ;;
    *) prime=0
       if grep -qs 0x10de /sys/bus/pci/devices/*/vendor && [ -e /proc/driver/nvidia/version ] &&
          { compgen -G "/usr/share/vulkan/icd.d/nvidia_icd*.json" || compgen -G "/etc/vulkan/icd.d/nvidia_icd*.json"; } >/dev/null; then
           prime=1
       fi ;;
esac
if [ "$prime" = 1 ]; then
    env_args+=(__NV_PRIME_RENDER_OFFLOAD=1 __VK_LAYER_NV_optimus=NVIDIA_only __GLX_VENDOR_LIBRARY_NAME=nvidia)
    echo "launch_game: PRIME offload to the NVIDIA GPU (BG3AI_PRIME=0 turns it off)" >&2
fi
if [ ! -f /usr/share/vulkan/explicit_layer.d/VkLayer_MESA_screenshot.json ] &&
   [ ! -f "${XDG_DATA_HOME:-$HOME/.local/share}/vulkan/explicit_layer.d/VkLayer_MESA_screenshot.json" ]; then
    echo "launch_game: warning: no VK_LAYER_MESA_screenshot installed (apt: mesa-vulkan-drivers >= 26.1 from trixie-backports); no screen capture" >&2
fi
# bg3le first: it exports SDL_PollEvent and forwards with dlsym(RTLD_NEXT), which reaches the shim, which
# forwards to the real libSDL2 (tested with native/bg3sdl/test/test_shim.py --chain --sniper).
preload=()
[ "$use_bg3le" = 1 ] && preload+=("$LIB")
[ "$use_input" = 1 ] && preload+=("$INPUT_LIB")
if [ ${#preload[@]} -gt 0 ]; then
    env_args+=("LD_PRELOAD=$(IFS=:; echo "${preload[*]}")")
fi
if [ "$use_bg3le" = 1 ]; then
    env_args+=(
        # Debug server on (its default), on a known port; no auto-opened console terminal.
        BG3LE_DEBUG=1
        "BG3LE_DEBUG_PORT=$PORT"
        BG3LE_CONSOLE=0
        # bg3le writes <path>.<pid> for each process in the chain that loads it.
        "BG3LE_LOG=$RUNTIME/logs/bg3le-$stamp.log"
    )
    [ -n "$mod_path" ] && env_args+=("BG3LE_MOD_PATH=$mod_path")
fi
env_args+=("${extra_env[@]}")

# pressure-vessel shares $HOME and the Steam libraries with the container, not arbitrary paths such as a repo on
# /data. Share the game dir (Steam does that itself only for games it launches), the repo, runtime dir, mod path and preload libraries explicitly when they live outside $HOME.
pv_fs=()
for p in "$GAME" "$REPO" "$RUNTIME" $(IFS=:; echo $mod_path) "$(dirname "$LIB")" "$(dirname "$INPUT_LIB")"; do
    case "$p" in ""|"$HOME"|"$HOME"/*) continue ;; esac
    [ -e "$p" ] || continue
    case ":$(IFS=:; echo "${pv_fs[*]:-}"):" in *":$p:"*) continue ;; esac
    pv_fs+=("$p")
done
[ -n "${PRESSURE_VESSEL_FILESYSTEMS_RW:-}" ] && pv_fs+=("$PRESSURE_VESSEL_FILESYSTEMS_RW")
run_env=()
[ ${#pv_fs[@]} -gt 0 ] && run_env+=("PRESSURE_VESSEL_FILESYSTEMS_RW=$(IFS=:; echo "${pv_fs[*]}")")

# Own cgroup for the game (see BG3AI_GAME_SCOPE). `systemd-run --scope` moves itself into the new scope and execs
# the command, so $! below is still the game's launcher and the runner's process-group kill still reaches it.
scope=()
SCOPE_UNIT="bg3ai-game-$stamp.scope"
if [ "${BG3AI_GAME_SCOPE:-1}" = 1 ]; then
    if command -v systemd-run >/dev/null 2>&1 &&
       systemd-run --user --scope --quiet --collect -p OOMPolicy=continue true >/dev/null 2>&1; then
        scope=(systemd-run --user --scope --quiet --collect "--unit=$SCOPE_UNIT"
               -p OOMPolicy=continue
               -p "MemoryMax=${BG3AI_GAME_MEMORY_MAX:-10G}" -p "MemorySwapMax=${BG3AI_GAME_SWAP_MAX:-2G}")
    else
        echo "launch_game: warning: systemd-run --user --scope does not work here: the game shares the caller's" \
             "cgroup (an OOM could take the caller's unit down)" >&2
    fi
fi

launch=("${scope[@]}" env "${run_env[@]}" "$SNIPER_DIR/run" -- env "${env_args[@]}" ./bin/bg3 "${game_args[@]}")

if [ "$dry_run" = 1 ]; then
    echo "cd $(printf '%q' "$GAME")"
    printf '%q ' "${launch[@]}"; echo
    echo "# log: $GAME_LOG"
    exit 0
fi

# The game writes ModCrashSanityCheck/ into the profile while running and deletes it on a clean exit; finding it
# at startup disables every mod. Killed runs leave it behind. Remove exactly that directory and nothing else.
SANITY="$PROFILE/ModCrashSanityCheck"
if [ -d "$SANITY" ] && [ ! -L "$SANITY" ]; then
    rm -rf -- "$SANITY"
    echo "launch_game: removed $SANITY" >&2
fi

{
    echo "# launch_game $stamp"
    echo "# cwd: $GAME"
    printf '# cmd: '; printf '%q ' "${launch[@]}"; echo
} > "$GAME_LOG"
echo "launch_game: game log $GAME_LOG" >&2

cd "$GAME"
"${launch[@]}" >> "$GAME_LOG" 2>&1 &
pid=$!
[ ${#scope[@]} -gt 0 ] && echo "launch_game: game cgroup $SCOPE_UNIT (OOMPolicy=continue, MemoryMax=${BG3AI_GAME_MEMORY_MAX:-10G})" >&2
echo "launch_game: started (pid $pid); bg3le debug server will listen on 127.0.0.1:$PORT" >&2

# If libsteam_api decides the game was not started by Steam it asks Steam to relaunch it and exits at once; the
# relaunched game would then be running WITHOUT bg3le. Catch that early exit and say why.
for _ in 1 2 3 4 5 6 7 8 9 10; do
    sleep 1
    kill -0 "$pid" 2>/dev/null || break
done
if ! kill -0 "$pid" 2>/dev/null; then
    wait "$pid" && rc=0 || rc=$?
    echo "launch_game: game exited within 10 s (status $rc); see $GAME_LOG" >&2
    if grep -qiE 'forwarded|RestartApp' "$GAME_LOG"; then
        echo "launch_game: Steam relaunched the game itself, so bg3le is NOT loaded in it. The Steam API ignored" >&2
        echo "  SteamAppId; the fallback is a steam_appid.txt (content: 1086940) in the game dir, as" >&2
        echo "  bg3le's run-native.sh does. That writes into the Steam install, so ask before doing it." >&2
    fi
    exit "$rc"
fi
echo "$pid" > "$RUNTIME/game.pid"
wait "$pid"

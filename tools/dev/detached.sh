#!/bin/bash
# tools/dev/detached.sh <parent pid> <command> [args...]: first_launch.sh's launcher, started as
#   setsid nohup tools/dev/detached.sh $$ runner/launch_game.sh ... &
# so it runs in a session of its own (the game outlives the script). It closes the inherited fds > 2 first: run
# under `flock runtime/live.lock`, the launcher (which lives as long as the game) kept the lock fd and held the live
# lock for the whole game (2026-10-10 10:52, every worker blocked).
# BG3AI_LAUNCHER_PIDFILE set: its pid (= its session and process group id) goes there before it checks that
# <parent pid> is still its parent; when not (the parent was killed: Ctrl-C of live_window.py during the launch) it
# exits 130 without running the command. A killer that reaps the parent and then reads the file thus either finds
# the launcher's group or the command never runs (live fix Window C: the launcher's own session outlived the window's
# kill of first_launch.sh's group and brought the game up 20 s later).
for f in "/proc/$$/fd/"*; do
    n=${f##*/}
    [ "$n" -gt 2 ] 2>/dev/null && eval "exec $n>&-" 2>/dev/null
done
[ -z "${BG3AI_LAUNCHER_PIDFILE:-}" ] || echo $$ > "$BG3AI_LAUNCHER_PIDFILE"
read -r _ _ _ ppid _ < /proc/$$/stat
[ "$ppid" = "$1" ] || { echo "detached.sh: parent $1 is gone (now $ppid): not running $2" >&2; exit 130; }
shift
exec "$@"

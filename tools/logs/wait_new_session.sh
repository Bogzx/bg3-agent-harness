#!/bin/bash
# Wait until a new Sxxx.triage.md (written by the runner at a session's end) appears in a playthrough's
# session dir, then until that session's memory keeper has finished (its memory_keeper event in
# runner_events.jsonl, at most keeper_max_s), then print its name and exit. Used by the logs worker as a
# background wake-up.
# Usage: tools/logs/wait_new_session.sh [run=claude-run-2] [max_wait_s=21600] [keeper_max_s=420]
run=${1:-claude-run-2}
max=${2:-21600}
keeper_max=${3:-420}
dir="$(dirname "$0")/../../runtime/sessions/$run"
stamp=$(mktemp)
trap 'rm -f "$stamp"' EXIT
end=$((SECONDS + max))
while [ $SECONDS -lt $end ]; do
    new=$(find "$dir" -maxdepth 1 -name 'S*.triage.md' -newer "$stamp" 2>/dev/null | sort | tail -1)
    if [ -n "$new" ]; then
        sid=$(basename "$new" .triage.md)
        kend=$((SECONDS + keeper_max))
        until grep -q "\"kind\": \"memory_keeper\", \"session\": \"$sid\"" "$dir/runner_events.jsonl" 2>/dev/null \
                || [ $SECONDS -ge $kend ]; do
            sleep 15
        done
        echo "NEW SESSION: $sid"
        exit 0
    fi
    sleep 30
done
echo "no new session in ${max}s"

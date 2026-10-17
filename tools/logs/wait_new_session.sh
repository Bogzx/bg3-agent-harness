#!/bin/bash
# Wait until a new Sxxx.triage.md (written by the runner at a session's end) appears in a playthrough's
# session dir, then until that session's Sxxx.review.md is written (the runner runs tools/logs/review_session.py
# once the memory keeper has finished, or right away without a keeper pass), at most keeper_max_s, then print its
# name and exit. If the review fails, it still wakes on the keeper's memory_keeper event in runner_events.jsonl
# (plus REVIEW_GRACE_S for a slow review). Used by the logs worker as a background wake-up.
# Usage: tools/logs/wait_new_session.sh [run=claude-run-2] [max_wait_s=21600] [keeper_max_s=420]
run=${1:-claude-run-2}
max=${2:-21600}
keeper_max=${3:-420}
REVIEW_GRACE_S=${REVIEW_GRACE_S:-150}   # the runner kills a review after 120 s (play.py REVIEW_TIMEOUT)
dir="$(dirname "$0")/../../runtime/sessions/$run"
stamp=$(mktemp)
trap 'rm -f "$stamp"' EXIT
end=$((SECONDS + max))
while [ $SECONDS -lt $end ]; do
    new=$(find "$dir" -maxdepth 1 -name 'S*.triage.md' -newer "$stamp" 2>/dev/null | sort | tail -1)
    if [ -n "$new" ]; then
        sid=$(basename "$new" .triage.md)
        sid=${sid%%-*}   # a resume's triage (S065-r1, S065-c1) belongs to S065
        review="$dir/$sid.review.md"
        kend=$((SECONDS + keeper_max))
        kept=
        until [ "$review" -nt "$new" ] || [ $SECONDS -ge $kend ]; do
            if [ -z "$kept" ] && grep -q "\"kind\": \"memory_keeper\", \"session\": \"$sid\"" \
                    "$dir/runner_events.jsonl" 2>/dev/null; then
                kept=1
                [ $((SECONDS + REVIEW_GRACE_S)) -lt $kend ] && kend=$((SECONDS + REVIEW_GRACE_S))
            fi
            sleep "${POLL_S:-15}"
        done
        if [ "$review" -nt "$new" ]; then
            echo "review: runtime/sessions/$run/$sid.review.md"
        else
            echo "review: none (not written in time; run tools/logs/review_session.py $run $sid)"
        fi
        echo "NEW SESSION: $sid"
        exit 0
    fi
    sleep "${POLL_S:-30}"
done
echo "no new session in ${max}s"

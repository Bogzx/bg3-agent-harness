#!/usr/bin/env python3
"""Classify hand clicks / raw keys / screenshots per session from runtime/mined/<run>/Sxxx.calls.jsonl.

Usage: python3 tools/logs/hand_clicks.py claude-run-2 [S025 S026 ...]
A "hand" call = click(x,y), press_key, screenshot, zoom/camera. Buckets by known screen spots and keys.
"""
import json, os, sys, glob, collections, time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import repo_paths  # noqa: E402
RUNTIME = str(repo_paths.runtime_dir())   # the main tree's from a worktree

SPOTS = [  # (bucket, x, y, radius)
    ("exit_turn_based", 0.87, 0.927, 0.02),
    ("rest_yes_box", 0.44, 0.55, 0.03),
    ("rest_autoselect", 0.5, 0.797, 0.015),
    ("rest_confirm", 0.5, 0.872, 0.015),
    ("rest_close_panel", 0.617, 0.113, 0.02),
    ("trade_balance", 0.5, 0.905, 0.015),
    ("trade_confirm", 0.5, 0.956, 0.015),
    ("dialogue_answer", 0.36, 0.85, 0.03),
    ("levelup_banner", 0.5, 0.85, 0.02),
]
KEYS = {"DELETE": "camera", "END": "camera", "O": "camera", "Q": "camera", "E": "camera", "HOME": "camera",
        "ESCAPE": "escape_close", "I": "inventory_panel", "LSHIFT+SPACE": "turn_based_key", "SPACE": "space",
        "M": "map", "TAB": "tab"}


def bucket(c):
    a = json.loads(c["args"]) if c["args"].startswith("{") and c["args"].endswith("}") else {}
    if c["tool"] == "click":
        x, y = a.get("x"), a.get("y")
        if isinstance(x, (int, float)) and isinstance(y, (int, float)):
            for b, bx, by, r in SPOTS:
                if abs(x - bx) <= r and abs(y - by) <= r:
                    return "click:" + b
        if a.get("button") == "right":
            return "click:right"
        return "click:world/other"
    if c["tool"] == "press_key":
        return "key:" + KEYS.get(str(a.get("key", "")).upper(), str(a.get("key", "")))
    return c["tool"]


def main():
    run = sys.argv[1]
    sids = sys.argv[2:] or sorted(os.path.basename(p)[:-12] for p in glob.glob(os.path.join(RUNTIME, "mined", run, "S*.calls.jsonl")))
    total = collections.Counter()
    for sid in sids:
        p = os.path.join(RUNTIME, "mined", run, sid + ".calls.jsonl")
        if not os.path.exists(p):
            continue
        cnt = collections.Counter()
        cost = collections.Counter()
        for line in open(p):
            c = json.loads(line)
            if c["tool"] in ("click", "press_key", "screenshot", "zoom", "camera"):
                b = bucket(c)
                cnt[b] += 1
                cost[b] += (c["dur"] or 0) + (c["think"] or 0)
        if cnt:
            print(sid, sum(cnt.values()), "calls,", round(sum(cost.values())), "s:",
                  ", ".join(f"{k} {v} ({cost[k]:.0f}s)" for k, v in cnt.most_common()))
        total.update(cnt)
    print("TOTAL", dict(total.most_common()))


if __name__ == "__main__":
    main()

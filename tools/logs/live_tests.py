#!/usr/bin/env python3
"""Mine runtime/actions.jsonl (every bridge tool call, any client) for a time window: per-tool latency
distribution, failures/refusals, slow calls, and a compact timeline. Streams the file (RAM-safe).

Usage: tools/logs/live_tests.py [--since HH:MM] [--until HH:MM] [--date YYYY-MM-DD] [--timeline] [--fails N]
"""
import argparse, json, re, statistics, sys, time
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
import repo_paths  # noqa: E402
FAIL = re.compile(r"(?i)\b(error|failed|fails|refused|refusing|can't|cannot|couldn't|could not|not found|timed? ?out|"
                  r"no such|invalid|unknown|blocked|stuck|didn't|did not|nothing happened|unreachable|aborted|exception)\b")


def ts(date, hm):
    return time.mktime(time.strptime(f"{date} {hm}", "%Y-%m-%d %H:%M"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=time.strftime("%Y-%m-%d"))
    ap.add_argument("--since", default="00:00")
    ap.add_argument("--until", default="23:59")
    ap.add_argument("--file", default=str(repo_paths.runtime_dir() / "actions.jsonl"))
    ap.add_argument("--timeline", action="store_true", help="print every call (one line each)")
    ap.add_argument("--fails", type=int, default=60, help="max failure lines to print")
    ap.add_argument("--slow", type=float, default=10.0, help="list calls slower than this (s)")
    a = ap.parse_args()
    t0, t1 = ts(a.date, a.since), ts(a.date, a.until) + 60
    ms = defaultdict(list)
    fails, slow, gaps = [], [], []
    fails_by_tool = defaultdict(int)
    last_t = None
    n = 0
    with open(a.file, errors="replace") as f:
        for line in f:
            try:
                d = json.loads(line)
            except ValueError:
                continue
            t = d.get("t", 0)
            if not (t0 <= t < t1):
                continue
            n += 1
            tool = d.get("tool", "?")
            m = d.get("ms")
            if m is None and d.get("total_s") is not None:
                m = d["total_s"] * 1000
            ms[tool].append(m or 0)
            res = str(d.get("result", d.get("detail", "")))
            head = res[:400]
            hm = time.strftime("%H:%M:%S", time.localtime(t))
            args = json.dumps(d.get("args", {}), ensure_ascii=False)[:90]
            if d.get("ok") is False or FAIL.search(head):
                fails_by_tool[tool] += 1
                fails.append(f"{hm} {tool} {args} => {head[:220]!r}")
            if (m or 0) / 1000 >= a.slow:
                slow.append(f"{hm} {tool} {(m or 0)/1000:.1f}s {args} => {head[:120]!r}")
            if last_t and t - last_t > 300:
                gaps.append(f"{time.strftime('%H:%M:%S', time.localtime(last_t))} -> {hm} ({(t-last_t)/60:.1f} min idle)")
            last_t = t
            if a.timeline:
                print(f"{hm} {tool} {(m or 0):.0f}ms {args} => {head[:140]!r}")
    print(f"# {n} calls {a.date} {a.since}-{a.until}")
    print(f"{'tool':24} {'n':>5} {'fail':>5} {'p50 s':>7} {'p90 s':>7} {'max s':>7} {'sum s':>8}")
    for tool, v in sorted(ms.items(), key=lambda kv: -sum(kv[1])):
        v = sorted(v)
        p = lambda q: v[min(len(v) - 1, int(q * len(v)))] / 1000
        print(f"{tool:24} {len(v):5} {fails_by_tool[tool]:5} {p(.5):7.2f} {p(.9):7.2f} {v[-1]/1000:7.1f} {sum(v)/1000:8.0f}")
    print(f"\n## slow calls (>= {a.slow}s): {len(slow)}")
    print("\n".join(slow[:40]))
    print(f"\n## failure-looking results: {len(fails)}")
    print("\n".join(fails[: a.fails]))
    print("\n## idle gaps > 5 min")
    print("\n".join(gaps))


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Observation size of recorded sessions: per-tool result size, repetition, context growth, and compaction replay.

    tools/vis/obs_size.py report runtime/sessions/naut/S001.jsonl ...   # tables (markdown) for research/impl-vis.md
    tools/vis/obs_size.py extract -o bridge/tests/golden/naut_results.jsonl.gz runtime/sessions/naut*/S001.jsonl
    tools/vis/obs_size.py check bridge/tests/golden/naut_results.jsonl.gz [--show N]   # fact checker + savings

Input: the runner's stream-json transcripts (runtime/sessions/<series>/Sxxx.jsonl). Text results only (screenshots
are images, ~1.35k tokens each, not touched). Tokens are estimated (bg3ai.compact_check.est_tokens: 1.6 x pieces,
fitted on the per-result context growth of these transcripts); context growth is the API's own usage numbers.
"""
from __future__ import annotations

import argparse
import collections
import gzip
import json
import re
import sys
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "bridge" / "src"))

from bg3ai import compact_check as cc  # noqa: E402


def _ts(s: str | None) -> float:
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def load_transcript(path: Path) -> tuple[list[dict], list[int]]:
    """([{"tool", "args", "text", "t", "image"}] in order, [context tokens of each model call])."""
    names: dict[str, tuple[str, dict]] = {}
    results: list[dict] = []
    ctx: list[int] = []
    prev = None
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            r = json.loads(line)
        except ValueError:
            continue
        msg = r.get("message")
        if not isinstance(msg, dict):
            continue
        if r.get("type") == "assistant":
            u = msg.get("usage") or {}
            if msg.get("id") != prev:
                prev = msg.get("id")
                ctx.append(int(u.get("input_tokens", 0)) + int(u.get("cache_creation_input_tokens", 0))
                           + int(u.get("cache_read_input_tokens", 0)))
            for c in msg.get("content") or []:
                if c.get("type") == "tool_use":
                    names[c["id"]] = (c["name"].replace("mcp__bg3__", ""), c.get("input") or {})
        elif r.get("type") == "user" and isinstance(msg.get("content"), list):
            for c in msg["content"]:
                if not (isinstance(c, dict) and c.get("type") == "tool_result"):
                    continue
                ct = c.get("content")
                image = False
                if isinstance(ct, list):
                    image = any(x.get("type") == "image" for x in ct if isinstance(x, dict))
                    ct = "".join(x.get("text", "") for x in ct if isinstance(x, dict) and x.get("type") == "text")
                ct = ct or ""
                try:
                    text = json.loads(ct)["result"]
                except (ValueError, KeyError, TypeError):
                    text = ct
                tool, args = names.get(c.get("tool_use_id"), ("?", {}))
                results.append({"tool": tool, "args": args, "text": text, "t": _ts(r.get("timestamp")),
                                "image": image})
    return results, ctx


def _repeat_share(results: list[dict]) -> dict[str, int]:
    """Chars per tool of lines that already appeared verbatim in an earlier result of the session."""
    seen: set[str] = set()
    rep: dict[str, int] = collections.Counter()
    for r in results:
        for ln in r["text"].split("\n"):
            if ln.strip() and ln in seen:
                rep[r["tool"]] += len(ln) + 1
            seen.add(ln)
    return rep


def _kind(line: str) -> str:
    s = line.strip()
    if s.startswith("⏱"):
        return "clock"
    for rx, k in ((r"^(ALERTS|PARTY:)", "alerts/party advice"), (r"^(EVENTS|CHANGES)$", "block headers"),
                  (r"^(cast|damage|turn_started|turn_ended|status_\w+|used|died|dialogue_line|round_started)\b",
                   "events"),
                  (r"^(NEXT|PACE|NOTES WINDOW|RUNNING)", "hints/running"), (r"^LAYOUT", "layout legend"),
                  (r"^(NOW|YOUR ACTIONS)", "combat resources"), (r"^\w[\w ]* \[[ABR3m+]+\]", "action list")):
        if re.match(rx, s):
            return k
    return "other"


def report(paths: list[Path]) -> str:
    out = []
    per_tool: dict[str, list[float]] = collections.defaultdict(lambda: [0, 0, 0, 0, 0])
    kinds: dict[str, list[int]] = collections.defaultdict(lambda: [0, 0])
    sess_rows = []
    for p in paths:
        results, ctx = load_transcript(p)
        text = [r for r in results if not r["image"]]
        rep = _repeat_share(text)
        rows = cc.replay([{"tool": r["tool"], "text": r["text"], "t": r["t"]} for r in text])
        for r, row in zip(text, rows):
            t = per_tool[r["tool"]]
            t[0] += 1
            t[1] += len(r["text"])
            t[2] += cc.est_tokens(r["text"])
            t[4] += cc.est_tokens(row["out"])
        for tool, n in rep.items():
            per_tool[tool][3] += n
        seen: set[str] = set()
        for r in text:
            for ln in r["text"].split("\n"):
                k = _kind(ln)
                kinds[k][0] += len(ln) + 1
                if ln.strip() and ln in seen:
                    kinds[k][1] += len(ln) + 1
                seen.add(ln)
        tok = sum(cc.est_tokens(r["text"]) for r in text)
        tok_c = sum(cc.est_tokens(x["out"]) for x in rows)
        game = [(r, x) for r, x in zip(text, rows) if r["tool"] not in cc.compact.PROSE_TOOLS]
        sess_rows.append((f"{p.parent.name}/{p.stem}", len(results), sum(r["image"] for r in results),
                          ctx[0] if ctx else 0, ctx[-1] if ctx else 0, len(ctx), sum(ctx),
                          sum(len(r["text"]) for r in text), tok, tok_c,
                          sum(cc.est_tokens(r["text"]) for r, _ in game), sum(cc.est_tokens(x["out"]) for _, x in game),
                          sum(len(x["problems"]) for x in rows)))
    out.append("| session | results (img) | ctx first → last (calls) | Σ ctx read over calls | result chars | "
               "result tok est | compacted | game-tool tok → compacted | fact problems |")
    out.append("|---|---|---|---|---|---|---|---|---|")
    for s in sess_rows:
        out.append(f"| {s[0]} | {s[1]} ({s[2]}) | {s[3]/1000:.1f}k → {s[4]/1000:.1f}k ({s[5]}) | {s[6]/1e6:.2f}M | "
                   f"{s[7]} | {s[8]} | {s[9]} ({(s[9]-s[8])/max(1,s[8]):+.0%}) | {s[10]} → {s[11]} "
                   f"({(s[11]-s[10])/max(1,s[10]):+.0%}) | {s[12]} |")
    out.append("")
    out.append("| tool | n | chars | tok est | tok/result | repeated lines (chars) | compacted tok | change |")
    out.append("|---|---|---|---|---|---|---|---|")
    for tool, (n, ch, tk, rp, tc) in sorted(per_tool.items(), key=lambda x: -x[1][2]):
        out.append(f"| {tool} | {int(n)} | {int(ch)} | {int(tk)} | {tk/max(1,n):.0f} | {rp/max(1,ch):.0%} | {int(tc)} | "
                   f"{(tc-tk)/max(1,tk):+.0%} |")
    out.append("")
    out.append("| line kind | chars | repeated verbatim |")
    out.append("|---|---|---|")
    for k, (ch, rp) in sorted(kinds.items(), key=lambda x: -x[1][0]):
        out.append(f"| {k} | {ch} | {rp/max(1,ch):.0%} |")
    return "\n".join(out)


def extract(paths: list[Path], dest: Path) -> int:
    n = 0
    with gzip.open(dest, "wt", encoding="utf-8") as f:
        for p in paths:
            results, _ = load_transcript(p)
            for r in results:
                if r["image"]:
                    continue
                f.write(json.dumps({"session": f"{p.parent.name}/{p.stem}", "tool": r["tool"], "t": r["t"],
                                    "text": r["text"]}, ensure_ascii=False) + "\n")
                n += 1
    return n


def load_golden(path: Path) -> dict[str, list[dict]]:
    sessions: dict[str, list[dict]] = collections.defaultdict(list)
    with gzip.open(path, "rt", encoding="utf-8") as f:
        for line in f:
            r = json.loads(line)
            sessions[r["session"]].append(r)
    return dict(sessions)


def check(path: Path, show: int = 0) -> int:
    bad = 0
    for sess, results in load_golden(path).items():
        rows = cc.replay(results)
        a = sum(cc.est_tokens(r["text"]) for r in results)
        b = sum(cc.est_tokens(x["out"]) for x in rows)
        probs = [(i, x) for i, x in enumerate(rows) if x["problems"]]
        bad += len(probs)
        print(f"{sess}: {len(rows)} results, tokens ~{a} -> ~{b} ({(b - a) / max(1, a):+.1%}), "
              f"{len(probs)} with fact problems")
        for i, x in probs[:10]:
            print(f"  #{i} {x['tool']}: {x['problems'][:3]}")
        for i, x in enumerate(rows[:show]):
            print(f"----- #{i} {x['tool']} ({cc.est_tokens(x['orig'])} -> {cc.est_tokens(x['out'])})\n{x['out']}")
    return 1 if bad else 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("report")
    r.add_argument("paths", nargs="+", type=Path)
    e = sub.add_parser("extract")
    e.add_argument("-o", "--out", type=Path, required=True)
    e.add_argument("paths", nargs="+", type=Path)
    c = sub.add_parser("check")
    c.add_argument("golden", type=Path)
    c.add_argument("--show", type=int, default=0)
    a = ap.parse_args(argv)
    if a.cmd == "report":
        print(report(a.paths))
        return 0
    if a.cmd == "extract":
        print(f"{extract(a.paths, a.out)} results -> {a.out}")
        return 0
    return check(a.golden, a.show)


if __name__ == "__main__":
    sys.exit(main())

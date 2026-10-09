"""Post-session triage report: what went wrong in one session, for the next round of fixes.

    python runner/triage.py runtime/sessions/<playthrough>/S002     # writes S002.triage.md

Reads the files the runner writes per session (all optional, missing ones are skipped):
    Sxxx.jsonl          claude stream-json (tool calls with arguments, results, the final summary)
    Sxxx.turns.jsonl    arrival times of the stream events (bg3ai.metrics.StreamTiming)
    Sxxx.metrics.jsonl  the bridge's per-tool latency records and state samples
    Sxxx.progress.jsonl story progress / stuck records (bg3ai.progress)

Sections: failed tool calls grouped by tool + error, the >10 s action gaps and what happened in them,
ToolSearch calls, screenshot/click counts, stuck stretches (progress stuck records and repeated identical
calls), and the agent's last summary. Stdlib + bg3ai.metrics only.
"""
from __future__ import annotations

import json
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bridge" / "src"))
from bg3ai import metrics as latency  # noqa: E402

FAIL_PREFIXES = ("FAILED", "NOT READY", "REFUSED")  # the bridge's own failure markers (server.py)
LONG_GAP_S = latency.LONG_GAP_S
REPEAT_MIN = 3          # the same call (tool + args) this many times in a row = a stuck stretch
SCREEN_TOOLS = ("screenshot",)
CLICK_TOOLS = ("click",)


def bare(name: str) -> str:
    return latency._bare(name or "")


def result_text(block: dict) -> str:
    """Plain text of a tool_result block; unwraps the bridge's {"result": "..."} envelope."""
    c = block.get("content")
    if isinstance(c, list):
        txt = " ".join(x.get("text", "") if x.get("type") == "text" else f"[{x.get('type')}]"
                       for x in c if isinstance(x, dict))
    else:
        txt = str(c or "")
    s = txt.strip()
    if s.startswith("{"):
        try:
            v = json.loads(s)
            if isinstance(v, dict) and isinstance(v.get("result"), str):
                return v["result"]
        except ValueError:
            pass
    return s


_STEP_FAIL = re.compile(r"(?:^|\s)([A-Za-z_]+)(?: [^:—\n]*)?:\s*(FAILED|TIMEOUT)\b\s*[—:]?\s*(.*)")
_ANY_FAIL = re.compile(r"\b(FAILED|TIMEOUT|NOT READY|REFUSED)\b\s*[—:]?\s*(.*)")


def failures(name: str, block: dict, text: str) -> list[tuple[str, str]]:
    """(tool or step, message) for each failure in one result: the call itself (is_error, or the bridge's
    FAILED/NOT READY/REFUSED prefix) and failed steps inside batch/queue results ("2. use_ability: FAILED — …",
    "leg 1: move_to: FAILED — …", "end_turn: TIMEOUT — …"). STOPPED lines are consequences, not counted."""
    t = text.strip()
    if block.get("is_error") or t.startswith("<tool_use_error>"):
        return [(name, t)]
    out = []
    for line in t.splitlines():
        line = line.strip()
        if not line or line.startswith(("STOPPED", "EVENTS", "CHANGES", "NOW")):
            continue
        m = _STEP_FAIL.search(line)
        if m:
            out.append((m.group(1), f"{m.group(2)}: {m.group(3)}"))
        elif line.startswith(FAIL_PREFIXES) or re.match(r"^\d+\.\s*(FAILED|TIMEOUT)\b", line):
            m = _ANY_FAIL.search(line)
            out.append((name, f"{m.group(1)}: {m.group(2)}" if m else line))
    return out


def error_key(text: str) -> str:
    """Group key for an error: first line, ids/numbers/quoted values masked, capped."""
    line = text.strip().splitlines()[0] if text.strip() else ""
    line = re.sub(r"</?tool_use_error>", "", line)
    line = re.sub(r"'[^']*'|\"[^\"]*\"", "'…'", line)
    line = re.sub(r"\b[eo]\d+\b", "#id", line)
    line = re.sub(r"-?\d+(\.\d+)?", "N", line)
    return line[:110]


def args_str(inp, limit: int = 160) -> str:
    s = json.dumps(inp, ensure_ascii=False, separators=(",", ":")) if inp is not None else ""
    return s if len(s) <= limit else s[:limit - 1] + "…"


def load_calls(stream_path: Path, turns_path: Path) -> tuple[list[dict], str | None]:
    """Tool calls in order: {id, name, input, t (use arrival), t_res, failed, text}; plus the final summary."""
    times = {}
    for r in latency.read_jsonl(turns_path):
        if r.get("id") and r.get("type") in ("use", "result"):
            times.setdefault((r["type"], r["id"]), r.get("t"))
    calls: dict[str, dict] = {}
    order: list[str] = []
    summary = last_text = None
    for ev in latency.read_jsonl(stream_path):
        typ = ev.get("type")
        if typ == "result":
            summary = ev.get("result") or summary
            continue
        content = (ev.get("message") or {}).get("content")
        for b in content if isinstance(content, list) else []:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "tool_use" and b.get("id"):
                calls[b["id"]] = {"id": b["id"], "name": bare(b.get("name", "")), "input": b.get("input"),
                                  "t": times.get(("use", b["id"])), "t_res": None, "failed": False, "text": "",
                                  "fails": []}
                order.append(b["id"])
            elif b.get("type") == "tool_result" and b.get("tool_use_id") in calls:
                c = calls[b["tool_use_id"]]
                c["text"] = result_text(b)
                c["fails"] = failures(c["name"], b, c["text"])
                c["failed"] = bool(c["fails"])
                c["t_res"] = times.get(("result", c["id"]))
            elif b.get("type") == "text" and typ == "assistant" and b.get("text", "").strip():
                last_text = b["text"].strip()
    return [calls[i] for i in order], summary or last_text


def hm(t: float | None) -> str:
    return datetime.fromtimestamp(t).strftime("%H:%M:%S") if t else "--:--:--"


def failures_section(calls: list[dict]) -> list[str]:
    groups: dict[tuple[str, str], list[tuple[dict, str]]] = defaultdict(list)
    for c in calls:
        for step, msg in c["fails"]:
            groups[(step, error_key(msg))].append((c, msg))
    n_calls = sum(1 for c in calls if c["failed"])
    out = [f"## Failed tool calls ({n_calls} of {len(calls)} calls; "
           f"{sum(len(v) for v in groups.values())} failures incl. batch/queue steps)", ""]
    if not groups:
        return out + ["none", ""]
    for (step, key), hits in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        first, msg = hits[0]
        via = sorted({c["name"] for c, _ in hits} - {step})
        out.append(f"- **{step}** ×{len(hits)}{' (via ' + ', '.join(via) + ')' if via else ''}: `{key}`")
        out.append(f"  - first {hm(first['t'])} `{first['name']}` args `{args_str(first['input'])}`")
        out.append(f"  - → {msg[:300]}")
    return out + [""]


def gaps_section(metrics_path: Path, calls: list[dict]) -> list[str]:
    recs = latency.read_jsonl(metrics_path)
    tools = [r for r in recs if r.get("type") == "tool"]
    samples = [r for r in recs if r.get("type") == "sample"]
    long_ = [g for g in latency.gaps(latency.action_intervals(tools), samples) if g["s"] > LONG_GAP_S]
    out = [f"## Idle gaps > {LONG_GAP_S:.0f} s between visible actions ({len(long_)})", ""]
    if not long_:
        return out + ["none", ""]
    timed = [c for c in calls if c["t"] is not None]
    for g in sorted(long_, key=lambda g: g["t"]):
        a, b = g["t"], g["t"] + g.get("gap_s", g["s"])
        before = [c for c in timed if c["t"] <= a][-1:]
        inside = [c for c in timed if a < c["t"] < b]
        names = Counter(c["name"] for c in inside)
        busy = f", party busy {g['busy_s']:.1f} s of {g['gap_s']:.1f} s" if g.get("busy_s") else ""
        out.append(f"- {hm(a)} **{g['s']:.1f} s** idle ({g['kind']}{busy}) → next `{g['next']}`")
        if before:
            p = before[0]
            out.append(f"  - preceded by `{p['name']}` `{args_str(p['input'], 100)}`"
                       f"{' FAILED' if p['failed'] else ''}: {p['text'].strip()[:160]!r}")
        if inside:
            out.append("  - during: " + ", ".join(f"{n}×{k}" if k > 1 else n for n, k in names.most_common()))
    return out + [""]


def toolsearch_section(calls: list[dict]) -> list[str]:
    ts = [c for c in calls if c["name"] == "ToolSearch"]
    out = [f"## ToolSearch calls ({len(ts)})", ""]
    out += [f"- {hm(c['t'])} `{args_str(c['input'], 120)}`" for c in ts] or ["none"]
    return out + [""]


def counts_section(calls: list[dict]) -> list[str]:
    shots = sum(1 for c in calls if any(s in c["name"] for s in SCREEN_TOOLS))
    clicks = sum(1 for c in calls if any(s in c["name"] for s in CLICK_TOOLS))
    top = Counter(c["name"] for c in calls).most_common(12)
    return ["## Counts", "",
            f"- tool calls: {len(calls)}; screenshots: {shots}; clicks: {clicks}",
            "- most used: " + ", ".join(f"{n}×{k}" for n, k in top), ""]


def repeat_runs(calls: list[dict], n: int = REPEAT_MIN) -> list[tuple[dict, int]]:
    """Runs of >= n consecutive identical calls (same tool and args) — loops like the S001 ladder."""
    runs, i = [], 0
    sig = [(c["name"], args_str(c["input"], 10_000)) for c in calls]
    while i < len(calls):
        j = i
        while j + 1 < len(calls) and sig[j + 1] == sig[i]:
            j += 1
        if j - i + 1 >= n:
            runs.append((calls[i], j - i + 1))
        i = j + 1
    return runs


def stuck_section(progress_path: Path, calls: list[dict]) -> list[str]:
    out = ["## Stuck stretches", ""]
    recs = latency.read_jsonl(progress_path)
    for r in recs:
        if r.get("type") == "stuck":
            out.append(f"- {hm(r.get('since'))}: no story progress for {r.get('idle_min')} min "
                       f"({r.get('actions')} tool calls meanwhile)")
        elif r.get("type") == "unstuck":
            out.append(f"  - unstuck {hm(r.get('t'))} after {r.get('stuck_min')} min")
    prog = [r["t"] for r in recs if r.get("type") == "progress" and r.get("t")]
    polls = [r["t"] for r in recs if r.get("t")]
    if polls:
        marks = [polls[0]] + prog + [polls[-1]]
        longest = max((b - a, a) for a, b in zip(marks, marks[1:]))
        out.append(f"- longest stretch without story progress: {longest[0] / 60:.1f} min from {hm(longest[1])}")
    for c, k in repeat_runs(calls):
        out.append(f"- {hm(c['t'])} `{c['name']}` repeated {k}× with the same args `{args_str(c['input'], 100)}`")
    if len(out) == 2:
        out.append("none")
    return out + [""]


def build(base: Path) -> str:
    """Markdown triage for the session files at base (e.g. .../S002)."""
    calls, summary = load_calls(Path(f"{base}.jsonl"), Path(f"{base}.turns.jsonl"))
    lines = [f"# Triage {base.name}", ""]
    lines += failures_section(calls)
    lines += gaps_section(Path(f"{base}.metrics.jsonl"), calls)
    lines += toolsearch_section(calls)
    lines += counts_section(calls)
    lines += stuck_section(Path(f"{base}.progress.jsonl"), calls)
    lines += ["## Last agent summary", "", (summary or "(none)").strip(), ""]
    return "\n".join(lines)


def write(base: Path) -> Path:
    out = Path(f"{base}.triage.md")
    out.write_text(build(base))
    return out


if __name__ == "__main__":
    for arg in sys.argv[1:]:
        print(write(Path(re.sub(r"\.(jsonl|log|triage\.md)$", "", arg))))

"""Parity of the analyzers ported to events.load_events (triage.load_calls, judge.stream_decisions,
model_time.stream_info, runs.fairness_flags / claude_totals) with their raw-stream versions as they were before the
port (frozen below as legacy_*), on small real sessions in runner/fixtures/sessions (claude-run-2 S086 with its
resume and keeper pass, naut2 S001: stream heads, strings cut to 500 chars, paths scrubbed) and, when this machine
has them, on the real runtime/sessions streams. Each session is checked twice: read from its raw stream (the
fallback) and from an events.jsonl written from it (the new path).

    python3 -m unittest runner/test_analyzer_parity.py
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ.setdefault("BG3AI_RUNTIME", tempfile.mkdtemp(prefix="bg3ai-test-runtime-"))
import events as E
import judge as J
import model_time as MT
import runs as R
import triage as T

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "sessions"
# BG3AI_PARITY_SESSIONS: another checkout's runtime/sessions (a worktree has none)
REAL = Path(os.environ.get("BG3AI_PARITY_SESSIONS") or Path(__file__).resolve().parents[1] / "runtime" / "sessions")
REAL_SAMPLE = ("claude-run-2/S062", "claude-run-2/S068", "claude-run-2/S078-r1", "naut/S001", "naut3/S001")


# ======================================================================== frozen pre-port versions (raw claude stream)


def legacy_load_calls(stream_path: Path, turns_path: Path) -> tuple[list[dict], str | None]:
    times = {}
    for r in T.latency.read_jsonl(turns_path):
        if r.get("id") and r.get("type") in ("use", "result"):
            times.setdefault((r["type"], r["id"]), r.get("t"))
    calls: dict[str, dict] = {}
    order: list[str] = []
    summary = last_text = None
    for ev in T.latency.read_jsonl(stream_path):
        typ = ev.get("type")
        if typ == "result":
            summary = ev.get("result") or summary
            continue
        content = (ev.get("message") or {}).get("content")
        for b in content if isinstance(content, list) else []:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "tool_use" and b.get("id"):
                calls[b["id"]] = {
                    "id": b["id"],
                    "name": T.bare(b.get("name", "")),
                    "input": b.get("input"),
                    "t": times.get(("use", b["id"])),
                    "t_res": None,
                    "failed": False,
                    "text": "",
                    "fails": [],
                }
                order.append(b["id"])
            elif b.get("type") == "tool_result" and b.get("tool_use_id") in calls:
                c = calls[b["tool_use_id"]]
                c["text"] = T.result_text(b)
                c["fails"] = T.failures(c["name"], b, c["text"])
                c["failed"] = bool(c["fails"])
                c["t_res"] = times.get(("result", c["id"]))
            elif b.get("type") == "text" and typ == "assistant" and b.get("text", "").strip():
                last_text = b["text"].strip()
    return [calls[i] for i in order], summary or last_text


def _legacy_result_text(block: dict) -> str:
    c = block.get("content")
    if isinstance(c, list):
        txt = " ".join(x.get("text", "") for x in c if isinstance(x, dict) and x.get("type") == "text")
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


def legacy_stream_decisions(stream: Path, since: float | None = None, until: float | None = None) -> list[dict]:
    sid = stream.name[: -len(".jsonl")]
    times = {
        r["id"]: r.get("t")
        for r in J._read_jsonl(stream.with_name(sid + ".turns.jsonl"))
        if r.get("type") == "use" and r.get("id")
    }
    if not times and since is not None:
        try:
            if stream.stat().st_mtime < since:
                return []
        except OSError:
            return []

    def in_window(t) -> bool:
        if t is None:
            return not times
        return (since is None or t >= since) and (until is None or t <= until)

    out: list[dict] = []
    offered: dict | None = None
    said: list[str] = []
    pending: dict[str, dict] = {}
    for ev in J._read_jsonl(stream):
        typ = ev.get("type")
        content = (ev.get("message") or {}).get("content")
        for b in content if isinstance(content, list) else []:
            if not isinstance(b, dict):
                continue
            bt = b.get("type")
            if typ == "assistant" and bt == "text" and b.get("text", "").strip():
                said.append(b["text"].strip())
            elif typ == "assistant" and bt == "tool_use" and b.get("id"):
                pending[b["id"]] = {
                    "name": (b.get("name") or "").rsplit("__", 1)[-1],
                    "input": b.get("input"),
                    "reason": " ".join(said) or None,
                    "offered": offered,
                }
            elif bt == "tool_result" and b.get("tool_use_id") in pending:
                call = pending.pop(b["tool_use_id"])
                t = times.get(b["tool_use_id"])
                text = _legacy_result_text(b)
                blocks = [x for x in J.dialogue_blocks(text) if x["options"]]
                if in_window(t):
                    out += J._call_decisions(sid, t, call, blocks)
                said = []
                if call["name"] in (J.CHOOSE, *J.BATCH_TOOLS) or blocks:
                    offered = (
                        blocks[-1] if blocks else (None if J._choice_steps(call["name"], call["input"]) else offered)
                    )
                elif "conversation ended" in text or "no dialogue on screen" in text:
                    offered = None
    return out


def legacy_stream_info(stream: Path) -> dict:
    think_ms: dict[str, float] = {}
    result, records, t = None, [], None
    for ev in MT.M.read_jsonl(stream):
        typ = ev.get("type")
        if ev.get("timestamp"):
            t = MT._iso(ev["timestamp"]) or t
        if typ == "assistant":
            mid = (ev.get("message") or {}).get("id")
            if mid and ev.get("thinking_duration_ms"):
                think_ms[mid] = think_ms.get(mid, 0.0) + float(ev["thinking_duration_ms"])
        elif typ == "result":
            u = ev.get("usage") or {}
            result = {
                "api_ms": ev.get("duration_api_ms"),
                "out": u.get("output_tokens"),
                "think": (u.get("output_tokens_details") or {}).get("thinking_tokens"),
                "turns": ev.get("num_turns"),
                "wall_ms": ev.get("duration_ms"),
            }
        if t is not None and typ in ("assistant", "user", "system"):
            records.extend(MT.M.stream_records(ev, t))
    return {"think_ms": think_ms, "result": result, "records": records}


def legacy_claude_totals(state_dir: Path, since: float) -> dict:
    cost, turns, tokens = 0.0, 0, {k: 0 for k, _ in R.TOKEN_FIELDS}
    found = sorted(
        (f.stat().st_mtime, f.name, f)
        for f in state_dir.iterdir()
        if R.CLAUDE_STREAM_RE.match(f.name) and f.stat().st_mtime >= since
    )
    parsers: dict = {}
    for _, _, f in found:
        lines = f.read_text(errors="replace").splitlines()
        name = R.stream_client(lines)
        for ev in R.stream_events([ln for ln in lines if any(m in ln for m in R.RESULT_MARKS[name])], parsers, name):
            if ev.kind != "result":
                continue
            cost += float(ev.cost_usd or 0)
            turns += int(ev.turns or 0)
            for k in R.clients.TOKEN_KEYS:
                tokens[k] += int((ev.tokens or {}).get(k) or 0)
    return {"cost_usd": cost, "turns": turns, "tokens": tokens}


def legacy_fairness_flags(streams: list[Path]) -> dict:
    kinds: dict = {}
    leak = check = blocks = 0
    for s in streams:
        calls, _ = legacy_load_calls(s, Path(str(s)[: -len(".jsonl")] + ".turns.jsonl"))
        blocks += sum(1 for c in calls if c["name"] in R.fairness.NOT_GAME and R.GUARD_BLOCK in (c.get("text") or ""))
        for rule, hits in R.fairness.audit_calls(calls).items():
            kinds[rule] = kinds.get(rule, 0) + len(hits)
            if R.fairness.RULES[rule][0] == R.fairness.LEAK:
                leak += len(hits)
            else:
                check += len(hits)
    return {"count": leak + check, "leak": leak, "check": check, "blocks": blocks, "kinds": kinds}


# ======================================================================== the two ways in


def with_events_log(src_dir: Path, dst_dir: Path) -> None:
    """dst_dir: the sessions of src_dir as the runner now writes them: raw stream, turns and an events.jsonl (the
    raw stream's Events, each call and result at its turns.jsonl time, as the pump stamps them on arrival)."""
    for f in sorted(src_dir.iterdir()):
        shutil.copy2(f, dst_dir / f.name)
        if R.CLAUDE_STREAM_RE.match(f.name):
            base = dst_dir / f.name[: -len(".jsonl")]
            evs = E.load_events(src_dir / f.name[: -len(".jsonl")])
            base.with_name(base.name + ".events.jsonl").write_text("".join(ev.to_json() + "\n" for ev in evs))


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="bg3ai-parity-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def dirs(self, src: Path) -> list[tuple[str, Path]]:
        """(how, dir): the raw fixture (fallback) and a copy with events logs."""
        logged = self.tmp / src.name
        logged.mkdir()
        with_events_log(src, logged)
        return [("raw", src), ("events", logged)]

    def streams(self, d: Path, play_only: bool = False) -> list[Path]:
        rx = R.PLAY_STREAM_RE if play_only else R.CLAUDE_STREAM_RE
        return sorted(f for f in d.iterdir() if rx.match(f.name))

    def check_session(self, how: str, stream: Path, legacy_stream: Path) -> None:
        turns = Path(str(stream)[: -len(".jsonl")] + ".turns.jsonl")
        legacy_turns = Path(str(legacy_stream)[: -len(".jsonl")] + ".turns.jsonl")
        msg = f"{how} {stream.parent.name}/{stream.name}"
        calls, summary = T.load_calls(stream, turns)
        old_calls, old_summary = legacy_load_calls(legacy_stream, legacy_turns)
        # one intended change: a non-text block reads <image> (the runner's log form) instead of [image]
        for c in old_calls:
            c["text"] = re.sub(r"\[(image|tool_reference)\]", r"<\1>", c["text"])
        self.assertEqual(calls, old_calls, msg)
        self.assertEqual(summary, old_summary, msg)
        self.assertEqual(J.stream_decisions(stream), legacy_stream_decisions(legacy_stream), msg)
        ts = [c["t"] for c in old_calls if c["t"]]
        if ts:  # a window over the middle of the session
            lo, hi = ts[len(ts) // 4], ts[3 * len(ts) // 4]
            self.assertEqual(J.stream_decisions(stream, lo, hi), legacy_stream_decisions(legacy_stream, lo, hi), msg)
        self.assertEqual(MT.stream_info(stream), legacy_stream_info(legacy_stream), msg)


class Fixtures(Base):
    def test_fixture_sessions(self):
        n = 0
        for src in sorted(FIXTURES.iterdir()):
            for how, d in self.dirs(src):
                for s in self.streams(d):
                    self.check_session(how, s, src / s.name)
                    n += 1
                plays = self.streams(d, play_only=True)
                self.assertEqual(R.fairness_flags(plays), legacy_fairness_flags(self.streams(src, play_only=True)))
                self.assertEqual(R.claude_totals(d, 0), legacy_claude_totals(src, 0), f"{how} {src.name}")
        self.assertEqual(n, 8)

    def test_fixtures_have_what_the_analyzers_read(self):
        s086 = FIXTURES / "claude-run-2" / "S086.jsonl"
        calls, summary = T.load_calls(s086, s086.with_name("S086.turns.jsonl"))
        self.assertTrue(summary)
        self.assertGreater(len(calls), 20)
        self.assertTrue(any(c["failed"] for c in calls) and all(c["t"] for c in calls))
        self.assertTrue(J.stream_decisions(s086))
        info = MT.stream_info(s086)
        self.assertTrue(info["think_ms"] and info["result"] and info["result"]["api_ms"])
        tot = R.claude_totals(FIXTURES / "claude-run-2", 0)
        self.assertGreater(tot["cost_usd"], 0)
        self.assertNotIn("/data/", "".join(f.read_text() for f in FIXTURES.rglob("*.jsonl")))

    def test_codex_calls_now_read(self):
        # before the port the analyzers read only claude's blocks; a codex session's calls now count too
        import test_clients as TC

        (self.tmp / "S001.jsonl").write_text("\n".join(TC.CODEX_RUN) + "\n")
        calls, summary = T.load_calls(self.tmp / "S001.jsonl", self.tmp / "S001.turns.jsonl")
        self.assertEqual([(c["name"], c["text"]) for c in calls], [("get_status", "status: ok, hp 12/12")])
        self.assertEqual(summary, "Status read. Done.")
        self.assertEqual(legacy_load_calls(self.tmp / "S001.jsonl", self.tmp / "S001.turns.jsonl"), ([], None))


@unittest.skipUnless(all((REAL / f"{s}.jsonl").exists() for s in REAL_SAMPLE), "no runtime/sessions streams")
class RealSessions(Base):
    def test_real_sample(self):
        for s in REAL_SAMPLE:
            stream = REAL / f"{s}.jsonl"
            with self.subTest(stream=s):
                self.check_session("raw", stream, stream)


if __name__ == "__main__":
    unittest.main()

"""Operator messages (the operator chat): the person running a playthrough writes to its agent from the Watch screen.

The playthrough's option (playthrough.toml `[operator] messages = true`, off by default) lets the server take
messages and the runner give the session's bg3 server $BG3AI_OPERATOR_DIR (<runtime>/sessions/<pt>); the bg3 server
puts each pending message into its next tool result, once (bg3ai.operator, which owns the file format). Files there:

    operator.jsonl            this server appends {id, t, text} (post)
    operator.delivered.jsonl  the bg3 server appends {id, t, session, tool}; the runner appends {id, t, redeliver:
                              true} for one a dead client's stream never showed (redeliver): it is queued again

A row as the API and the SSE stream show it: {id, t, text, status: "queued" | "delivered", delivered?: {t, session,
tool}}. A bench run's dir (manifest.json) never takes one, whatever the config says.
"""

from __future__ import annotations

import json
import re
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from bg3ai import operator as op

from . import config
from .jsontypes import is_list, is_obj

MAX_CHARS = 1000
MIN_GAP_S = 5.0  # one message per this many seconds
MAX_QUEUED = 10  # messages still queued before a new one is refused
CONTROL_RE = re.compile(r"[\x00-\x09\x0b-\x1f\x7f]")  # control characters but the newline
_post_lock = threading.Lock()  # the server's request threads: one append at a time (ids stay unique)


def runner_env(env: Mapping[str, str]) -> dict[str, str]:
    """`env` (the app's, a shell's export included) without any BG3AI_OPERATOR_*: only the runner's session env
    gives a playthrough with the option on its own dir (session_env, legacy_cli.condition_env)."""
    return {k: v for k, v in env.items() if not k.startswith("BG3AI_OPERATOR_")}


def bench_run(sdir: Path) -> bool:
    """A bench run's dir: the bench's run manifest is there."""
    return (sdir / "manifest.json").exists()


def enabled(pt_dir: Path, sdir: Path) -> bool:
    """The playthrough's option is on (an unreadable config: off) and the dir is no bench run."""
    try:
        on = config.load(pt_dir).operator.messages
    except (config.ConfigError, OSError):
        return False
    return on and not bench_run(sdir)


def clean(text: str) -> str:
    """The text as sent: Windows line ends as newlines, other control characters removed, outer space stripped."""
    return CONTROL_RE.sub("", text.replace("\r\n", "\n").replace("\r", "\n")).strip()


def rows(sdir: Path) -> list[dict[str, Any]]:
    """Every message with its status, in the order sent."""
    done = op.delivery(op.read_rows(sdir / op.DELIVERED))
    return [row(r, done.get(r["id"])) for r in op.valid(op.read_rows(sdir / op.INBOX))]


def row(msg: dict[str, Any], delivered: dict[str, Any] | None) -> dict[str, Any]:
    out: dict[str, Any] = {"id": msg["id"], "t": msg["t"], "text": msg["text"], "status": "queued"}
    if delivered is not None:
        out["status"] = "delivered"
        out["delivered"] = {k: delivered.get(k) for k in ("t", "session", "tool")}
    return out


def post(sdir: Path, on: bool, body: dict[str, Any], now: float) -> tuple[int, dict[str, Any]]:
    """POST .../messages -> (HTTP code, payload): 201 {id, t, status}; 409 option off or a bench run; 400 a text
    that is empty or too long; 429 too soon after the last one, or too many still queued."""
    if not on or bench_run(sdir):
        return 409, {"error": "operator messages are off for this playthrough"}
    raw = body.get("text")
    if not isinstance(raw, str):
        return 400, {"error": "text: expected a string"}
    text = clean(raw)
    if not text:
        return 400, {"error": "text: empty"}
    if len(text) > MAX_CHARS:
        return 400, {"error": f"text: {len(text)} characters, at most {MAX_CHARS}"}
    with _post_lock:
        sent = op.valid(op.read_rows(sdir / op.INBOX))
        if sent and now - max(r["t"] for r in sent) < MIN_GAP_S:
            return 429, {"error": f"one message per {MIN_GAP_S:.0f} s"}
        if sum(r["status"] == "queued" for r in rows(sdir)) >= MAX_QUEUED:
            return 429, {"error": f"{MAX_QUEUED} messages are still queued"}
        n = max((int(m[1]) for r in sent if (m := re.fullmatch(r"m(\d+)", r["id"]))), default=0) + 1
        msg = {"id": f"m{n}", "t": round(now, 3), "text": text}
        sdir.mkdir(parents=True, exist_ok=True)
        with (sdir / op.INBOX).open("a", encoding="utf-8") as f:
            f.write(json.dumps(msg, ensure_ascii=False) + "\n")
    return 201, {"id": msg["id"], "t": msg["t"], "status": "queued"}


def session_counts(sdir: Path, session: str, started: float | None, ended: float | None) -> dict[str, Any]:
    """A session's history row field: {enabled, sent (posted while it ran), delivered (into its results)}."""
    sent = [r for r in op.valid(op.read_rows(sdir / op.INBOX))
            if started is not None and r["t"] >= started and (ended is None or r["t"] <= ended)]  # fmt: skip
    done = op.read_rows(sdir / op.DELIVERED)
    delivered = {r["id"] for r in done if r.get("session") == session and not r.get("redeliver")}
    return {"enabled": True, "sent": len(sent), "delivered": len(delivered)}


def _strings(v: object) -> list[str]:
    if isinstance(v, str):
        return [v]
    if is_obj(v):
        return [s for x in v.values() for s in _strings(x)]
    if is_list(v):
        return [s for x in v for s in _strings(x)]
    return []


def shown(stream: Path) -> list[str]:
    """Every string in a client's raw stream log (<sid>.jsonl): the tool results its model was given are there."""
    try:
        text = stream.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    out: list[str] = []
    for ln in text.splitlines():
        try:
            out += _strings(json.loads(ln))
        except ValueError:
            continue
    return out


def redeliver(sdir: Path, session: str, now: float) -> list[str]:
    """At an agent restart: each message delivered in `session` whose block is in no string of the session's stream
    log (the client died before its model saw the result) gets a redeliver row. Returns those ids."""
    inbox = {r["id"]: r for r in op.valid(op.read_rows(sdir / op.INBOX))}
    done = op.delivery(op.read_rows(sdir / op.DELIVERED))
    mine = [i for i, d in done.items() if d is not None and d.get("session") == session and i in inbox]
    if not mine:
        return []
    seen = shown(sdir / f"{session}.jsonl")
    lost = [i for i in mine if not any(op.head(inbox[i]["t"]) in s for s in seen)]
    if lost:
        with (sdir / op.DELIVERED).open("a", encoding="utf-8") as f:
            for i in lost:
                f.write(json.dumps({"id": i, "t": round(now, 3), "redeliver": True}) + "\n")
    return lost

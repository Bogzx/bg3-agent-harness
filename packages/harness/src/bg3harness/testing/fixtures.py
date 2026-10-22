"""Recorded fixtures as a fake mod responder: replay the bridge's recorder files (bg3ai/rpc_record.py) by request.

A fixture line holds one eval: its calls ([{op, args}]) and replies (one per call), or a Lua `error`, or for a non-RPC
eval its `code` and raw `reply` (or the Lua `error` it raised). Replies are indexed per op by (context, op, args),
raw replies by (context, code); the same request answers with its recorded replies in order and then keeps repeating
the last one (a session polls the same op many times; a load's GetGameState probes go Running, UnloadLevel, ...,
Running).
`match="op"` also answers a request whose args were never recorded with that op's replies (any args).
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from .fakemod import LuaError, Reply

VERSION = 1


def _key(ctx: str, op: str, args: dict[str, Any]) -> tuple[str, str, str]:
    return ctx, op, json.dumps(args or {}, sort_keys=True, separators=(",", ":"))


class _Queue[T]:
    def __init__(self) -> None:
        self.replies: list[T] = []
        self.next = 0

    def take(self) -> T:
        rep = self.replies[min(self.next, len(self.replies) - 1)]
        self.next += 1
        return rep


class FixtureStore:
    def __init__(self, lines: Iterable[dict[str, Any]] = (), match: str = "exact"):
        if match not in ("exact", "op"):
            raise ValueError("match is 'exact' or 'op'")
        self.match = match
        self._exact: dict[tuple[str, str, str], _Queue[Reply]] = {}
        self._by_op: dict[tuple[str, str], _Queue[Reply]] = {}
        self._raw: dict[tuple[str, str], _Queue[str | LuaError]] = {}
        for line in lines:
            self.add(line)

    @classmethod
    def load(cls, *paths: str | Path, match: str = "exact") -> FixtureStore:
        lines: list[dict[str, Any]] = []
        for p in paths:
            for n, text in enumerate(Path(p).read_text(encoding="utf-8").splitlines(), 1):
                if text.strip():
                    try:
                        lines.append(json.loads(text))
                    except json.JSONDecodeError as e:
                        raise ValueError(f"{p}:{n}: not JSON ({e})") from e
        return cls(lines, match)

    def add(self, line: dict[str, Any]) -> None:
        if line.get("v") != VERSION:
            raise ValueError(f"fixture line version {line.get('v')!r}, expected {VERSION}")
        ctx = line.get("ctx", "server")
        calls = line.get("calls")
        if calls is None:
            if "code" in line and ("reply" in line or "error" in line):
                raw = line["reply"] if "reply" in line else LuaError(line["error"])
                self._raw.setdefault((ctx, line["code"]), _Queue()).replies.append(raw)
            return
        if "error" in line:
            replies: list[Reply] = [{"lua_error": line["error"]}] * len(calls)
        elif "replies" in line:
            replies = list(line["replies"])
        else:
            return  # the mod wasn't loaded ("nil"): nothing an op could answer
        if len(replies) != len(calls):
            raise ValueError(f"fixture line has {len(calls)} calls but {len(replies)} replies")
        for call, rep in zip(calls, replies, strict=True):
            op: str = call["op"]
            args: dict[str, Any] = call.get("args") or {}
            self._exact.setdefault(_key(ctx, op, args), _Queue()).replies.append(rep)
            self._by_op.setdefault((ctx, op), _Queue()).replies.append(rep)

    def ops(self) -> set[tuple[str, str]]:
        """(context, op) pairs this store can answer."""
        return set(self._by_op)

    def reply(self, ctx: str, op: str, args: dict[str, Any]) -> Reply | None:
        q = self._exact.get(_key(ctx, op, args))
        if q is None and self.match == "op":
            q = self._by_op.get((ctx, op))
        return q.take() if q is not None else None

    def eval_raw(self, ctx: str, code: str) -> str | None:
        """The recorded raw reply; raises the recorded LuaError for an eval that failed."""
        q = self._raw.get((ctx, code))
        if q is None:
            return None
        out = q.take()
        if isinstance(out, LuaError):
            raise LuaError(str(out))  # a fresh one each time: the queue repeats its last entry
        return out

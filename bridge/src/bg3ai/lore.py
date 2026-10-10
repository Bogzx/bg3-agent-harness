"""What the party has read, kept for recall (puzzle hints a player would remember: a book's riddle, a note naming
an order, a journal step). Fair by construction: only texts the game showed on screen (book/note panels, read_all)
and the party's own journal; never wiki text.

Storage: one JSON line per text in <memory>/read_texts.jsonl ({title, text, where, t}), deduplicated by title+text.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

FILE = "read_texts.jsonl"
SNIPPET = 500        # chars shown around the first match of a long text
MAX_TEXT = 20000     # chars kept per text


def _load(path: Path) -> list[dict]:
    out = []
    try:
        for line in path.read_text().splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict) and row.get("text"):
                out.append(row)
    except OSError:
        pass
    return out


def remember(memory: Path, title: Any, text: Any, where: str = "") -> bool:
    """Keep one read text (False: empty or already kept)."""
    title, text = str(title or "").strip(), str(text or "").strip()
    if not text:
        return False
    path = memory / FILE
    if any(r.get("title") == title and r.get("text") == text[:MAX_TEXT] for r in _load(path)):
        return False
    try:
        memory.mkdir(parents=True, exist_ok=True)
        with path.open("a") as f:
            f.write(json.dumps({"title": title, "text": text[:MAX_TEXT], "where": where,
                                "t": time.strftime("%Y-%m-%d %H:%M")}) + "\n")
    except OSError:
        return False
    return True


def _words(q: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9']+", q.lower()) if len(w) >= 3]


def _score(text: str, words: list[str]) -> int:
    low = text.lower()
    # a word counts by its first 5 letters (painting/paintings, order/ordered)
    return sum(1 for w in words if w[:5] in low)


def _snippet(text: str, words: list[str], n: int = SNIPPET) -> str:
    if len(text) <= n:
        return text
    low = text.lower()
    at = min((low.find(w[:5]) for w in words if w[:5] in low), default=0)
    start = max(0, at - n // 3)
    return ("…" if start else "") + text[start:start + n].strip() + ("…" if start + n < len(text) else "")


def search(memory: Path, query: str, journal: Any = None, max_chars: int = 3000) -> str:
    """Texts read so far (and journal steps) matching the query's words, best first. Empty query: the titles."""
    rows = _load(memory / FILE)
    words = _words(query)
    if not words:
        if not rows:
            return "Nothing read yet. Books, notes and letters you read (interact, read_all) are kept here."
        return "READ (recall(query) searches them): " + "; ".join(
            f"{r.get('title') or '(untitled)'}" + (f" ({r['where']})" if r.get("where") else "") for r in rows[-40:])
    hits = []
    for r in rows:
        s = _score(f"{r.get('title', '')} {r['text']}", words)
        if s:
            hits.append((s, "READ", r.get("title") or "(untitled)", r.get("where") or "", r["text"]))
    for q in journal if isinstance(journal, list) else []:
        if not isinstance(q, dict):
            continue
        for st in q.get("steps") or []:
            s = _score(f"{q.get('title', '')} {st}", words)
            if s:
                hits.append((s, "JOURNAL", str(q.get("title") or ""), "", str(st)))
    if not hits:
        return (f"Nothing you have read or noted in the journal mentions {query!r}. Read the books/notes near the "
                "puzzle (interact), look at the scene (get_nearby kinds=props for names of paintings/statues).")
    hits.sort(key=lambda h: -h[0])
    out, used = [], 0
    for _, kind, title, where, text in hits:
        line = f"{kind} {title}" + (f" ({where})" if where else "") + ": " + _snippet(text, words)
        if used + len(line) > max_chars and out:
            out.append(f"(+{len(hits) - len(out)} more matches; narrow the query)")
            break
        out.append(line)
        used += len(line)
    return "\n".join(out)

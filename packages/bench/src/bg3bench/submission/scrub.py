"""The submission scrub: personal data out of every string of every bundle file.

Replaced, each with a marker: absolute paths (home, repo, save and temp paths, Windows paths: the last component
kept, as `<path>/<name>`, including the `[Image: source: ...]` lines of tool results), e-mail addresses, Steam IDs
and userdata paths, Tailscale names and addresses, API key shapes, KEY/TOKEN/SECRET/PASSWORD assignments, and any
literal value the caller passes (the values of the submitter's .env). The path rule is the bridge's fixture recorder
rule and the others are the repo's publish scrub patterns (tools/ci/scrub.py), copied: the bench imports neither.

JSON files are scrubbed string by string after parsing (a Windows path inside JSON text is escaped), and a file or
line is rewritten only when something in it changed. `hits` finds what the scrub would replace; a packed bundle has
none (check S01).
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

VERSION = 1

_SEG = r"[^\s\"'<>|:;,)\]}\\/]+"
_INNER = r"[^\"<>|:;,)\]}\\/\n]+?(?=[\\/])"
_ABS_PATH = re.compile(
    r"(?:~|/(?:home|data|tmp|root|mnt|media|run|var|opt|usr|srv|Users|nix|proc)(?=[\\/])|\b[A-Za-z]:(?=[\\/]))"
    rf"(?:[\\/](?:{_INNER}|{_SEG}))+"
)
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
_NOREPLY = re.compile(
    r"(?:no-?reply@.*|.*@(?:users\.noreply\.github\.com|example\.(?:com|org|net|test)))", re.IGNORECASE
)
_STEAM = re.compile(r"(?<!\d)7656119\d{10}(?!\d)|userdata[/\\]\d{4,}")
_TAILSCALE = re.compile(
    r"\b[a-z0-9-]+(?:\.[a-z0-9-]+)*\.ts\.net\b|\b100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7])(?:\.\d{1,3}){2}\b"
)
_KEY = re.compile(
    r"sk-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{22,}|hf_[A-Za-z0-9]{30,}"
    r"|AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{35}|xox[abprs]-[A-Za-z0-9-]{10,}|-----BEGIN [A-Z ]*PRIVATE KEY-----"
)
_SECRET = re.compile(
    r"\b[A-Z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD)[A-Z0-9_]*\s*[:=]\s*[\"']?(?P<value>[A-Za-z0-9_\-./+=]{16,})[\"']?"
    r"(?=\s|$|,)"
)
_NOT_SECRET = re.compile(r"[A-Z0-9_]+|[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}")
PATH_MARK = "<path>"


def _path(m: re.Match[str]) -> str | None:
    return f"{PATH_MARK}/{re.split(r'[\\/]', m.group(0))[-1]}"


def _email(m: re.Match[str]) -> str | None:
    return None if _NOREPLY.fullmatch(m.group(0)) else "<email>"


def _secret(m: re.Match[str]) -> str | None:
    if _NOT_SECRET.fullmatch(m.group("value")):
        return None
    return m.group(0)[: m.start("value") - m.start(0)] + "<secret>"


def _mark(text: str) -> Callable[[re.Match[str]], str | None]:
    return lambda m: text


# (kind, pattern, replacement: None keeps the match). Keys before secrets, so `X_KEY=sk-...` is one replacement.
RULES: tuple[tuple[str, re.Pattern[str], Callable[[re.Match[str]], str | None]], ...] = (
    ("api_key", _KEY, _mark("<key>")),
    ("secret", _SECRET, _secret),
    ("email", _EMAIL, _email),
    ("steam_id", _STEAM, _mark("<steam_id>")),
    ("tailscale", _TAILSCALE, _mark("<tailscale>")),
    ("path", _ABS_PATH, _path),
)
MIN_LITERAL = 8  # a shorter .env value would match ordinary text


@dataclass
class Scrubber:
    """Scrubs strings and counts replacements. `literals`: exact values to remove (the .env's values)."""

    literals: Iterable[str] = ()
    replacements: int = 0
    _lits: list[str] = field(default_factory=list[str], init=False)

    def __post_init__(self) -> None:
        self._lits = sorted({v for v in self.literals if len(v) >= MIN_LITERAL}, key=len, reverse=True)

    def text(self, s: str) -> str:
        for lit in self._lits:
            if lit in s:
                self.replacements += s.count(lit)
                s = s.replace(lit, "<env>")
        for _kind, pattern, repl in RULES:
            s = pattern.sub(lambda m, r=repl: self._sub(m, r), s)
        return s

    def _sub(self, m: re.Match[str], repl: Callable[[re.Match[str]], str | None]) -> str:
        out = repl(m)
        if out is None or out == m.group(0):
            return m.group(0)
        self.replacements += 1
        return out

    def value(self, v: Any) -> Any:
        if isinstance(v, str):
            return self.text(v)
        if isinstance(v, list):
            return [self.value(x) for x in v]  # pyright: ignore[reportUnknownVariableType]
        if isinstance(v, dict):
            return {self.text(str(k)): self.value(x) for k, x in v.items()}  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]
        return v

    def file(self, name: str, data: bytes) -> bytes:
        """A bundle file's bytes with every string scrubbed; unchanged bytes when nothing matched."""
        if name.endswith(".jsonl"):
            lines = data.decode().split("\n")
            return "\n".join(self._json_line(ln) for ln in lines).encode()
        if name.endswith(".json"):
            before = self.replacements
            doc = self.value(json.loads(data))
            if self.replacements == before:
                return data
            return (json.dumps(doc, indent=1, sort_keys=True, ensure_ascii=False) + "\n").encode()
        if name.endswith((".txt", ".md")):
            return self.text(data.decode()).encode()
        return data  # binary (frames.tar): never holds text the run wrote

    def _json_line(self, line: str) -> str:
        if not line.strip():
            return line
        before = self.replacements
        doc = self.value(json.loads(line))
        return line if self.replacements == before else json.dumps(doc, ensure_ascii=False, separators=(",", ":"))


def _strings(v: Any) -> Iterable[str]:
    if isinstance(v, str):
        yield v
    elif isinstance(v, list):
        for x in v:  # pyright: ignore[reportUnknownVariableType]
            yield from _strings(x)
    elif isinstance(v, dict):
        for k, x in v.items():  # pyright: ignore[reportUnknownVariableType]
            yield str(k)  # pyright: ignore[reportUnknownArgumentType]
            yield from _strings(x)


def text_hits(s: str, literals: Iterable[str] = ()) -> list[str]:
    """The kinds the scrub would replace in s (one entry per match)."""
    out = ["env_value" for lit in literals if len(lit) >= MIN_LITERAL and lit in s]
    for kind, pattern, repl in RULES:
        out += [kind for m in pattern.finditer(s) if repl(m) not in (None, m.group(0))]
    return out


def file_hits(name: str, data: bytes, literals: Iterable[str] = ()) -> list[str]:
    """What the scrub would replace in a bundle file; a JSON file that doesn't parse is scanned as text."""
    texts: Iterable[str]
    try:
        if name.endswith(".jsonl"):
            texts = [s for ln in data.decode().splitlines() if ln.strip() for s in _strings(json.loads(ln))]
        elif name.endswith(".json"):
            texts = list(_strings(json.loads(data)))
        elif name.endswith((".txt", ".md")):
            texts = [data.decode()]
        else:
            return []
    except (ValueError, UnicodeDecodeError):
        texts = [data.decode(errors="replace")]
    lits = list(literals)
    return [k for s in texts for k in text_hits(s, lits)]

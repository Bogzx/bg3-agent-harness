"""Scrub check for the launch gate (OVERHAUL-PLAN.md Phase F2.4): personal paths and keys in anything published.

Scans every tracked file of the repo (git ls-files, its path and its contents) and, with --artifacts, every file
under a directory of release artifacts (results, save metadata, transcripts). One line per hit: path:line: kind.
The matched text is not printed for keys and .env values, so the report itself can be pasted anywhere.

Kinds:
    home_path    /home/<name>, /Users/<name>, C:\\Users\\<name>
    email        an address outside the noreply allowlist (users.noreply.github.com, noreply@, example.* domains)
    api_key      sk-... (OpenAI, Anthropic, DeepSeek, OpenRouter sk-or-v1-...), ghp_/gho_/ghs_/github_pat_, hf_,
                 AKIA..., AIza..., xox?-..., a PEM private key header
    env_file     a file named .env or .env.<x> (not .env.example / .env.sample / .env.template)
    env_value    the value of a KEY=value line of a real .env file (--env-file), found verbatim anywhere
    secret       a KEY/TOKEN/SECRET/PASSWORD assignment to a literal of 16+ token characters (not an env var's
                 NAME or a GUID)
    tailscale    a *.ts.net name or a Tailscale (CGNAT 100.64/10) address
    steam_id     a SteamID64 (7656119 + 10 digits) or a Steam userdata/<account id> path
    term         a personal term: machine names, the owner's name and handles, account ids (see below)

Personal terms are not in this file, so the tool doesn't publish what it looks for. They come from the local git
user.name (each word, and the words joined), $USER, the hostname, --term, and the terms file (default
~/.config/bg3ai/scrub-terms.txt: one term per line, '#' comments). A term matches case-insensitively where it is not
part of a longer word.

The allowlist (tools/ci/scrub_allowlist.txt, never scanned itself) has one entry per line:

    <kind|*> <path-glob> [<text>]  # <reason>

A hit is allowed when its kind and path match (fnmatch) and, if <text> is given, its matched text equals it
(case-insensitive). The reason is required.

    uv run python tools/ci/scrub.py                                   # the tracked tree
    uv run python tools/ci/scrub.py --artifacts <out_dir> --env-file ../bg3-ai/.env

Exit codes: 0 no hits, 1 hits (listed on stdout), 2 usage or config error (bad allowlist, not a git repo, missing
--artifacts directory or terms/env file named on the command line).
"""

from __future__ import annotations

import argparse
import fnmatch
import getpass
import re
import socket
import subprocess
import sys
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ALLOWLIST = Path(__file__).with_name("scrub_allowlist.txt")
TERMS_FILE = Path.home() / ".config" / "bg3ai" / "scrub-terms.txt"
MIN_TERM = 3
SECRET_KINDS = frozenset({"api_key", "env_value", "secret"})  # matched text never printed

_WORD = r"[A-Za-z0-9._-]"
PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("home_path", re.compile(rf"/home/[A-Za-z0-9_]{_WORD}*|/Users/[A-Za-z0-9_]{_WORD}*|[A-Za-z]:\\\\?Users\\\\?\w+")),
    ("email", re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")),
    (
        "api_key",
        re.compile(
            r"sk-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{22,}|hf_[A-Za-z0-9]{30,}"
            r"|AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{35}|xox[abprs]-[A-Za-z0-9-]{10,}|-----BEGIN [A-Z ]*PRIVATE KEY-----"
        ),
    ),
    (
        "secret",
        re.compile(
            r"\b[A-Z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD)[A-Z0-9_]*\s*[:=]\s*[\"']?(?P<value>[A-Za-z0-9_\-./+=]{16,})[\"']?(?=\s|$|,)"
        ),
    ),
    (
        "tailscale",
        re.compile(
            r"\b[a-z0-9-]+(?:\.[a-z0-9-]+)*\.ts\.net\b|\b100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7])(?:\.\d{1,3}){2}\b"
        ),
    ),
    ("steam_id", re.compile(r"(?<!\d)7656119\d{10}(?!\d)|userdata[/\\]\d{4,}")),
)
NOREPLY_DOMAINS = ("users.noreply.github.com", "example.com", "example.org", "example.net", "example.test")
NOREPLY_LOCALS = ("noreply", "no-reply")
# A "secret" whose value is an env var's name or a GUID is a reference or an id, not a key.
NOT_SECRET = re.compile(r"[A-Z0-9_]+|[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}")
ENV_TEMPLATES = (".env.example", ".env.sample", ".env.template")
KINDS = frozenset({"home_path", "email", "api_key", "env_file", "env_value", "secret", "tailscale", "steam_id", "term"})


@dataclass(frozen=True)
class Hit:
    path: str
    line: int  # 0: the file's path itself
    kind: str
    text: str

    def render(self) -> str:
        shown = "" if self.kind in SECRET_KINDS else f": {self.text}"
        return f"{self.path}:{self.line}: {self.kind}{shown}"


@dataclass(frozen=True)
class Allow:
    kind: str  # or "*"
    glob: str
    text: str | None
    reason: str

    def covers(self, hit: Hit) -> bool:
        return (
            self.kind in ("*", hit.kind)
            and fnmatch.fnmatchcase(hit.path, self.glob)
            and (self.text is None or self.text.casefold() == hit.text.casefold())
        )


def parse_allowlist(text: str) -> list[Allow]:
    """Parse the allowlist; raises ValueError on a malformed line or an entry without a reason."""
    entries: list[Allow] = []
    for n, raw in enumerate(text.splitlines(), 1):
        body, _, comment = raw.partition("#")
        fields = body.split()
        if not fields:
            continue
        if len(fields) < 2:
            raise ValueError(f"line {n}: expected '<kind> <path-glob> [<text>]  # <reason>': {raw!r}")
        if not comment.strip():
            raise ValueError(f"line {n}: entry needs a reason after '#': {raw!r}")
        kind, glob, *rest = fields
        if kind != "*" and kind not in KINDS:
            raise ValueError(f"line {n}: unknown kind {kind!r}")
        entries.append(Allow(kind, glob, " ".join(rest) or None, comment.strip()))
    return entries


def parse_env(text: str) -> dict[str, str]:
    """KEY=value pairs of a .env file; values shorter than 8 characters are too common to search for."""
    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.removeprefix("export ").partition("=")
        value = value.strip().strip("\"'")
        if len(value) >= 8:
            values[key.strip()] = value
    return values


def term_pattern(terms: Iterable[str]) -> re.Pattern[str] | None:
    words = sorted({t.strip() for t in terms if len(t.strip()) >= MIN_TERM}, key=len, reverse=True)
    if not words:
        return None
    alts = "|".join(re.escape(w) for w in words)
    return re.compile(rf"(?<![A-Za-z0-9])(?:{alts})(?![A-Za-z0-9])", re.IGNORECASE)


def _allowed_email(addr: str) -> bool:
    local, _, domain = addr.lower().rpartition("@")
    return local in NOREPLY_LOCALS or any(domain == d or domain.endswith("." + d) for d in NOREPLY_DOMAINS)


def scan_text(
    path: str, text: str, terms: re.Pattern[str] | None = None, env: dict[str, str] | None = None
) -> list[Hit]:
    """Every hit in one file: its path (line 0) and its contents."""
    hits: list[Hit] = []
    name = path.rsplit("/", 1)[-1]
    if (name == ".env" or name.startswith(".env.")) and name not in ENV_TEMPLATES:
        hits.append(Hit(path, 0, "env_file", name))
    patterns = PATTERNS + ((("term", terms),) if terms else ())
    for n, line in enumerate([path, *text.splitlines()]):
        for kind, pat in patterns:
            for m in pat.finditer(line):
                if kind == "email" and _allowed_email(m.group()):
                    continue
                if kind == "secret" and NOT_SECRET.fullmatch(m.group("value")):
                    continue
                hits.append(Hit(path, n, kind, m.group()))
        for key, value in (env or {}).items():
            if value in line:
                hits.append(Hit(path, n, "env_value", key))
    return hits


def filter_allowed(hits: Iterable[Hit], allow: Sequence[Allow]) -> list[Hit]:
    return [h for h in hits if not any(a.covers(h) for a in allow)]


def tracked_files(root: Path) -> list[str]:
    out = subprocess.run(["git", "-C", str(root), "ls-files", "-z"], capture_output=True, check=True).stdout
    return [p for p in out.decode().split("\0") if p]


def _read(path: Path) -> str:
    try:
        return path.read_bytes().decode("utf-8", errors="replace")
    except (FileNotFoundError, IsADirectoryError):  # deleted in the work tree, or a submodule
        return ""


def iter_sources(root: Path, artifacts: Path | None) -> Iterator[tuple[str, str]]:
    """(reported path, contents) of every tracked file but the allowlist, then every file under artifacts."""
    skip = ALLOWLIST.resolve()
    for rel in tracked_files(root):
        if (root / rel).resolve() != skip:
            yield rel, _read(root / rel)
    if artifacts is not None:
        for p in sorted(artifacts.rglob("*")):
            if p.is_file():
                yield f"{artifacts.name}/{p.relative_to(artifacts).as_posix()}", _read(p)


def local_terms() -> list[str]:
    """The owner's identifiers on this machine: git user.name (words and joined), $USER, hostname."""
    name = subprocess.run(["git", "config", "user.name"], capture_output=True, text=True).stdout.split()
    terms = [*name, "".join(name)] if name else []
    return [*terms, getpass.getuser(), socket.gethostname()]


def read_terms(path: Path) -> list[str]:
    return [t for raw in path.read_text().splitlines() if (t := raw.partition("#")[0].strip())]


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", type=Path, default=ROOT, help="git repo to scan (default: this repo)")
    ap.add_argument("--artifacts", type=Path, help="also scan every file under this release directory")
    ap.add_argument("--allowlist", type=Path, default=ALLOWLIST)
    ap.add_argument("--terms-file", type=Path, help=f"personal terms (default {TERMS_FILE} if it exists)")
    ap.add_argument("--term", action="append", default=[], help="one more personal term (repeatable)")
    ap.add_argument("--no-local-terms", action="store_true", help="don't add git user.name, $USER, hostname")
    ap.add_argument("--env-file", type=Path, action="append", default=[], help="a real .env whose values to find")
    args = ap.parse_args(argv)
    try:
        allow = parse_allowlist(args.allowlist.read_text()) if args.allowlist.exists() else []
        terms = list(args.term) + ([] if args.no_local_terms else local_terms())
        terms_file = args.terms_file or (TERMS_FILE if TERMS_FILE.exists() else None)
        if terms_file is not None:
            terms += read_terms(terms_file)
        env: dict[str, str] = {}
        for f in args.env_file:
            env.update(parse_env(f.read_text()))
        if args.artifacts is not None and not args.artifacts.is_dir():
            raise FileNotFoundError(f"--artifacts {args.artifacts} is not a directory")
        pat = term_pattern(terms)
        hits = [h for rel, text in iter_sources(args.root, args.artifacts) for h in scan_text(rel, text, pat, env)]
    except (ValueError, OSError, subprocess.CalledProcessError) as e:
        print(f"scrub: {e}", file=sys.stderr)
        return 2
    left = filter_allowed(hits, allow)
    for h in left:
        print(h.render())
    print(f"scrub: {len(left)} hit(s), {len(hits) - len(left)} allowed", file=sys.stderr)
    return 1 if left else 0


if __name__ == "__main__":
    sys.exit(main())

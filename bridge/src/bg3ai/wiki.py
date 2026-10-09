"""bg3.wiki lookups for the agent: search and plain-text pages.

Uses the MediaWiki Action API at https://bg3.wiki/w/api.php. Content is
CC BY-SA 4.0 / CC BY-NC-SA 4.0, so every result carries its page URL for
attribution. The wiki's robots.txt disallows crawlers on api.php; this module
is for occasional on-demand lookups only: one request per second at most
(shared across processes through a lock file), a descriptive User-Agent and an
on-disk cache.

Pages are rendered from `action=parse` HTML rather than TextExtracts: on this
wiki TextExtracts returns empty heading names ("==  ==") and silently drops
some sections (e.g. Fire Bolt's "Technical details"), so sections could not be
addressed reliably.

    python -m bg3ai.wiki search "gauntlet of shar"
    python -m bg3ai.wiki page "Iron Throne" --section Tactics
"""

from __future__ import annotations

import argparse
import fcntl
import gzip
import hashlib
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path

API = "https://bg3.wiki/w/api.php"
SITE = "https://bg3.wiki/wiki/"
LICENSE = "bg3.wiki, CC BY-SA 4.0 / CC BY-NC-SA 4.0"
USER_AGENT = "bg3-ai-bridge/0.1 (AI agent playing Baldur's Gate 3; on-demand lookups, cached, <=1 req/s)"
MIN_INTERVAL = 1.0
DEFAULT_TTL = 7 * 24 * 3600
TIMEOUT = 20
RENDER_VERSION = 1  # bump when render_html output changes, to invalidate cached pages


def _cache_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache"
    return Path(os.environ.get("BG3AI_WIKI_CACHE", Path(base) / "bg3-ai" / "wiki"))


def page_url(title: str, anchor: str | None = None) -> str:
    url = SITE + urllib.parse.quote(title.replace(" ", "_"), safe="/:_()',!-")
    return f"{url}#{urllib.parse.quote(anchor, safe='_.-')}" if anchor else url


class WikiError(Exception):
    pass


# ---------------------------------------------------------------- HTTP

class _Client:
    def __init__(self, cache_dir: Path, user_agent: str):
        self.cache_dir = cache_dir
        self.user_agent = user_agent

    def _throttle(self) -> None:
        """Wait until MIN_INTERVAL has passed since the last request by any process."""
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        with open(self.cache_dir / ".last_request", "a+") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            f.seek(0)
            try:
                last = float(f.read().strip() or 0)
            except ValueError:
                last = 0.0
            wait = last + MIN_INTERVAL - time.time()
            if wait > 0:
                time.sleep(wait)
            f.seek(0)
            f.truncate()
            f.write(repr(time.time()))

    def get(self, **params) -> dict:
        params = {"format": "json", "formatversion": "2", "maxlag": "5", **params}
        url = API + "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={"User-Agent": self.user_agent, "Accept-Encoding": "gzip"})
        for attempt in range(3):
            self._throttle()
            try:
                with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                    body = resp.read()
                    if resp.headers.get("Content-Encoding") == "gzip":
                        body = gzip.decompress(body)
                data = json.loads(body)
            except urllib.error.HTTPError as e:
                if e.code in (429, 500, 502, 503, 504) and attempt < 2:
                    time.sleep(min(float(e.headers.get("Retry-After") or 2 ** (attempt + 1)), 30))
                    continue
                raise WikiError(f"HTTP {e.code} from bg3.wiki") from e
            except (urllib.error.URLError, TimeoutError) as e:
                if attempt < 2:
                    time.sleep(2 ** (attempt + 1))
                    continue
                raise WikiError(f"cannot reach bg3.wiki: {e}") from e
            err = data.get("error")
            if err and err.get("code") == "maxlag" and attempt < 2:
                time.sleep(5)
                continue
            if err:
                raise WikiError(f"{err.get('code')}: {err.get('info')}")
            return data
        raise WikiError("bg3.wiki request failed")


# ---------------------------------------------------------------- HTML -> text

_SKIP_TAGS = {"script", "style", "figure", "video", "audio", "noscript", "sup", "math", "img", "picture"}
_SKIP_CLASSES = {"navbox", "mw-editsection", "reference", "references", "toc", "noprint", "mw-file-source",
                 "catlinks", "mw-empty-elt", "embedvideo-wrapper", "mw-collapsible-toggle-placeholder"}
_VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}
_BLOCK = {"p", "div", "ul", "ol", "dl", "table", "tr", "blockquote", "center", "section", "pre", "caption"}
_HEADINGS = {"h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6}


@dataclass
class Section:
    level: int
    title: str
    anchor: str | None
    start: int  # offset of the heading line in the rendered text


@dataclass
class _Rendered:
    text: str
    sections: list[Section] = field(default_factory=list)


class _TextRenderer(HTMLParser):
    """Compact plain text: '## Heading' lines, '- ' bullets, ' | ' between table cells."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.skip_depth = 0
        self.stack: list[tuple[str, bool]] = []  # (tag, opened a skip)
        self.heading: tuple[int, str | None] | None = None
        self.heading_buf: list[str] = []
        self.headings: list[tuple[int, str, str | None, int]] = []  # level, title, anchor, out index
        self.after_dt = False

    def _nl(self, n: int = 1) -> None:
        self.out.append("\n" * n)

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        classes = set((a.get("class") or "").split())
        style = (a.get("style") or "").replace(" ", "").lower()
        skip = (tag in _SKIP_TAGS or bool(classes & _SKIP_CLASSES) or "display:none" in style)
        if tag in _VOID:
            if tag == "br" and not self.skip_depth:
                self._nl()
            return
        self.stack.append((tag, skip))
        if skip:
            self.skip_depth += 1
        if self.skip_depth:
            return
        if tag in _HEADINGS:
            self.heading = (_HEADINGS[tag], a.get("id"))
            self.heading_buf = []
        elif tag == "span" and self.heading and a.get("id") and not self.heading[1]:
            self.heading = (self.heading[0], a["id"])
        elif tag == "li":
            self._nl()
            self.out.append("- ")
        elif tag == "dt" or (tag == "dd" and not self.after_dt):
            self._nl()
        elif tag in ("td", "th"):
            self.out.append(" | ")
        elif tag in _BLOCK:
            self._nl()

    def handle_endtag(self, tag):
        if tag in _VOID:
            return
        # Tolerate unbalanced HTML: pop up to the matching tag.
        while self.stack:
            t, skip = self.stack.pop()
            if skip:
                self.skip_depth -= 1
            if t == tag:
                break
        if self.skip_depth:
            return
        if tag in _HEADINGS and self.heading:
            title = re.sub(r"\s+", " ", "".join(self.heading_buf)).strip()
            if title:
                self._nl(2)
                self.headings.append((self.heading[0], title, self.heading[1], len(self.out)))
                self.out.append(f"{'#' * self.heading[0]} {title}")
                self._nl()
            self.heading = None
        elif tag == "dt":
            self.out.append(": ")
            self.after_dt = True
            return
        elif tag in _BLOCK or tag in ("li", "dd"):
            self._nl()
        self.after_dt = False

    def handle_data(self, data):
        if self.skip_depth:
            return
        if self.heading:
            self.heading_buf.append(data)
        elif self.after_dt and data.isspace():
            return
        else:
            self.out.append(data)


_INVISIBLE = dict.fromkeys(map(ord, "⁠​‎‏﻿"), None) | {0x202F: " ", 0xA0: " "}


def _clean(text: str) -> str:
    text = text.translate(_INVISIBLE)
    lines = []
    for line in text.split("\n"):
        line = re.sub(r"[ \t]+", " ", line).strip()
        line = re.sub(r"^\|\s*", "", line)  # leading cell separator
        line = re.sub(r":\s*:", ":", line)  # "Damage: 1~10" + ": "
        if line and line not in ("|", "-", ":", "\u201c", "\u201e", "\u201d"):
            lines.append(line)
    return "\n".join(lines)


def render_html(fragment: str) -> _Rendered:
    r = _TextRenderer()
    r.feed(fragment)
    r.close()
    # Render pieces between headings separately so section offsets survive cleaning.
    marks = [i for *_, i in r.headings] + [len(r.out)]
    text = _clean("".join(r.out[:marks[0]]))
    sections = []
    for (level, title, anchor, start), end in zip(r.headings, marks[1:]):
        chunk = _clean("".join(r.out[start:end]))
        text = f"{text}\n\n" if text else ""
        sections.append(Section(level, title, anchor, len(text)))
        text += chunk
    return _Rendered(text, sections)


# ---------------------------------------------------------------- cache

class _Cache:
    def __init__(self, root: Path):
        self.root = root

    def _path(self, kind: str, *key) -> Path:
        h = hashlib.sha1(json.dumps([k.casefold() if isinstance(k, str) else k for k in key]).encode()).hexdigest()
        return self.root / kind / f"{h}.json"

    def load(self, kind: str, *key) -> dict | None:
        try:
            return json.loads(self._path(kind, *key).read_text())
        except (OSError, ValueError):
            return None

    def save(self, kind: str, value: dict, *key) -> None:
        p = self._path(kind, *key)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(value, ensure_ascii=False))
        tmp.replace(p)


# ---------------------------------------------------------------- public API

class Wiki:
    def __init__(self, cache_dir: Path | None = None, ttl: float = DEFAULT_TTL, user_agent: str | None = None):
        root = Path(cache_dir) if cache_dir else _cache_dir()
        contact = os.environ.get("BG3AI_WIKI_CONTACT")
        ua = user_agent or (f"{USER_AGENT[:-1]}; contact: {contact})" if contact else USER_AGENT)
        self.http = _Client(root, ua)
        self.cache = _Cache(root)
        self.ttl = ttl

    # -- search

    def search(self, query: str, limit: int = 5) -> list[dict]:
        query = query.strip()
        if not query:
            raise WikiError("empty query")
        limit = max(1, min(int(limit), 20))
        hit = self.cache.load("search", query, limit)
        if hit and time.time() - hit["fetched_at"] < self.ttl:
            return hit["results"]
        data = self.http.get(action="query", list="search", srsearch=query, srlimit=limit,
                             srnamespace=0, srprop="snippet")
        results = []
        for r in data["query"]["search"]:
            snippet = html.unescape(re.sub(r"<[^>]+>", "", r.get("snippet", "")))
            results.append({"title": r["title"], "url": page_url(r["title"]),
                            "snippet": _clean(snippet).replace("\n", " ")})
        self.cache.save("search", {"fetched_at": time.time(), "results": results}, query, limit)
        return results

    # -- pages

    def _resolve(self, title: str) -> tuple[str, int] | None:
        """Canonical title and latest revid, following redirects and case-insensitive near matches."""
        data = self.http.get(action="query", prop="info", titles=title, redirects=1)
        page = data["query"]["pages"][0]
        if not page.get("missing") and not page.get("invalid"):
            return page["title"], page["lastrevid"]
        near = self.http.get(action="query", list="search", srsearch=title, srwhat="nearmatch",
                             srnamespace=0, srlimit=1)["query"]["search"]
        if near:
            return self._resolve(near[0]["title"]) if near[0]["title"] != title else None
        return None

    def _fetch(self, title: str) -> dict:
        """Rendered page for the current revision, from cache when fresh."""
        alias = self.cache.load("alias", title)
        canonical = alias["title"] if alias else title
        cached = self.cache.load("page", canonical)
        if cached and cached.get("render") != RENDER_VERSION:
            cached = None
        elif cached and time.time() - cached["checked_at"] < self.ttl:
            return cached
        resolved = self._resolve(canonical)
        if resolved is None:
            raise WikiError(f"no page titled {title!r}")
        canonical, revid = resolved
        self.cache.save("alias", {"title": canonical}, title)
        cached = self.cache.load("page", canonical)
        if cached and cached["revid"] == revid and cached.get("render") == RENDER_VERSION:
            cached["checked_at"] = time.time()
        else:
            data = self.http.get(action="parse", page=canonical, prop="text|revid", redirects=1,
                                 disableeditsection=1, disabletoc=1, disablelimitreport=1)["parse"]
            r = render_html(data["text"])
            cached = {"title": data["title"], "revid": data["revid"], "render": RENDER_VERSION, "checked_at": time.time(),
                      "text": r.text, "sections": [vars(s) for s in r.sections]}
        self.cache.save("page", cached, canonical)
        return cached

    def page(self, title: str, section: str | int | None = None, max_chars: int = 6000) -> dict:
        title = title.strip().replace("_", " ")
        if title.startswith(SITE):
            title = urllib.parse.unquote(title[len(SITE):]).replace("_", " ")
        title, _, frag = title.partition("#")
        if frag and section is None:
            section = frag.replace("_", " ")
        p = self._fetch(title)
        sections = [Section(**s) for s in p["sections"]]
        text, anchor, sec_title = p["text"], None, None
        if section is not None and str(section).strip():
            i = _pick_section(sections, section)
            if i is None:
                raise WikiError(f"no section {section!r} in {p['title']}; sections: "
                                + "; ".join(s.title for s in sections))
            s = sections[i]
            end = next((t.start for t in sections[i + 1:] if t.level <= s.level), len(text))
            text, anchor, sec_title = text[s.start:end].strip(), s.anchor, s.title
        truncated = len(text) > max_chars
        if truncated:
            cut = text.rfind("\n", 0, max_chars)
            text = text[:cut if cut > max_chars // 2 else max_chars].rstrip() + "\n[...truncated]"
        return {
            "title": p["title"], "url": page_url(p["title"], anchor), "revid": p["revid"],
            "section": sec_title, "text": text, "truncated": truncated,
            "sections": [f"{i + 1}. {'  ' * (s.level - 2)}{s.title}" for i, s in enumerate(sections)],
            "license": LICENSE,
        }


def _pick_section(sections: list[Section], want: str | int) -> int | None:
    if isinstance(want, int) or str(want).strip().isdigit():
        i = int(want) - 1
        return i if 0 <= i < len(sections) else None
    w = str(want).strip().casefold()
    for test in (lambda t: t == w, lambda t: t.startswith(w), lambda t: w in t):
        for i, s in enumerate(sections):
            if test(s.title.casefold()):
                return i
    return None


_default: Wiki | None = None


def _wiki() -> Wiki:
    global _default
    if _default is None:
        _default = Wiki()
    return _default


def wiki_search(query: str, limit: int = 5) -> list[dict]:
    """Search bg3.wiki. Returns [{title, url, snippet}]."""
    return _wiki().search(query, limit)


def wiki_page(title: str, section: str | int | None = None, max_chars: int = 6000) -> dict:
    """Plain text of a bg3.wiki page (or one section, by number, name or name prefix).

    Returns {title, url, revid, section, text, truncated, sections, license}.
    """
    return _wiki().page(title, section, max_chars)


def format_search(results: list[dict]) -> str:
    if not results:
        return "No results."
    return "\n".join(f"- {r['title']} <{r['url']}>: {r['snippet']}" for r in results)


def format_page(p: dict) -> str:
    head = f"# {p['title']}" + (f" > {p['section']}" if p["section"] else "")
    parts = [head, f"Source: {p['url']} ({p['license']})", "", p["text"]]
    if p["truncated"] or not p["section"]:
        parts += ["", "Sections: " + " | ".join(s.strip() for s in p["sections"])]
    return "\n".join(parts)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m bg3ai.wiki", description="Query bg3.wiki")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("search")
    s.add_argument("query")
    s.add_argument("-n", "--limit", type=int, default=5)
    p = sub.add_parser("page")
    p.add_argument("title")
    p.add_argument("-s", "--section")
    p.add_argument("-m", "--max-chars", type=int, default=6000)
    for x in (s, p):
        x.add_argument("--json", action="store_true")
        x.add_argument("--ttl", type=float, default=DEFAULT_TTL, help="cache lifetime in seconds (0 = refresh)")
    a = ap.parse_args(argv)
    w = Wiki(ttl=a.ttl)
    try:
        if a.cmd == "search":
            r = w.search(a.query, a.limit)
            print(json.dumps(r, indent=1, ensure_ascii=False) if a.json else format_search(r))
        else:
            r = w.page(a.title, a.section, a.max_chars)
            print(json.dumps(r, indent=1, ensure_ascii=False) if a.json else format_page(r))
    except WikiError as e:
        print(f"wiki: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

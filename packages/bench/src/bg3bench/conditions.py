"""Conditions: what a run's agent is given besides the scenario. The id is the condition's whole content, so a
run's manifest names it exactly: `rules`, `rules+coaching@<hash8>`, `rules+wiki`, `rules+persona:evil`,
`rules+reloads:2`, combined in that order (`rules+coaching@1a2b3c4d+wiki+persona:evil`).

- prompt: rules only, or rules + the frozen coaching files (their hash; packages/harness/prompts assembles them).
- wiki: the wiki tools are OFF unless the id says `+wiki` (contamination: the main track is closed-book about
  walkthroughs; a wiki condition is reported apart).
- persona: neutral unless named.
- reloads: `load_game` is removed from the tool list unless the condition allows reloads (BENCHMARK-PLAN.md:
  "success within R reloads" is a separate, declared condition).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

WIKI_TOOLS = ("wiki_search", "wiki_page")
RELOAD_TOOLS = ("load_game",)
_COACHING_RE = re.compile(r"^coaching@[0-9a-f]{8}$")
_PERSONA_RE = re.compile(r"^persona:[a-z][a-z0-9_-]{0,31}$")
_RELOADS_RE = re.compile(r"^reloads:[1-9]\d?$")


@dataclass(frozen=True)
class Condition:
    coaching: str | None = None  # the coaching bundle's hash8, None = rules only
    wiki: bool = False
    persona: str = "neutral"
    reloads: int = 0

    @property
    def id(self) -> str:
        parts = ["rules"]
        if self.coaching:
            parts.append(f"coaching@{self.coaching}")
        if self.wiki:
            parts.append("wiki")
        if self.persona != "neutral":
            parts.append(f"persona:{self.persona}")
        if self.reloads:
            parts.append(f"reloads:{self.reloads}")
        return "+".join(parts)

    def removed_tools(self) -> list[str]:
        """Tools the server must not list for this condition."""
        return [*(() if self.wiki else WIKI_TOOLS), *(() if self.reloads else RELOAD_TOOLS)]


def parse(cid: str) -> Condition:
    """A condition id -> Condition; ValueError unless it is in the canonical form `Condition(...).id` gives."""
    parts = cid.split("+")
    if parts[0] != "rules":
        raise ValueError(f"condition {cid!r} must start with 'rules'")
    kw: dict = {}
    for p in parts[1:]:
        if _COACHING_RE.match(p):
            kw["coaching"] = p.removeprefix("coaching@")
        elif p == "wiki":
            kw["wiki"] = True
        elif _PERSONA_RE.match(p):
            kw["persona"] = p.removeprefix("persona:")
        elif _RELOADS_RE.match(p):
            kw["reloads"] = int(p.removeprefix("reloads:"))
        else:
            raise ValueError(f"condition {cid!r}: unknown part {p!r}")
    cond = Condition(**kw)
    if cond.id != cid:
        raise ValueError(f"condition {cid!r} is not canonical: write {cond.id!r}")
    return cond

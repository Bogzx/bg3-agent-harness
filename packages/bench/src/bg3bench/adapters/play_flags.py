"""TEMPORARY (with legacy_cli.py, Phase D): what runner/play.py can do for each condition id (conditions.py), read
from its argparse on 2026-10-21 (play.py run, with --client pi and --profile from the harness worker).

A condition becomes play.py flags plus environment for the bg3 MCP server, and a list of what play.py cannot do for
it yet. `entry(cid)` builds that from the parsed condition; `table(cids)` is LegacyCli's input and refuses a
condition with a gap unless `allow_gaps` (a pilot run whose results are labelled as such, never published).

- Tools: the bridge removes tools named in BG3AI_TOOLS_OFF at startup (bridge/src/bg3ai/capabilities.py). play.py
  has no flag for it, so the adapter passes it in the environment of play.py, which the client and the MCP server
  inherit (to be confirmed live once per client).
- Prompt: play.py always plays play/CLAUDE.md plus knowledge/playbooks, rules and coaching mixed, unhashed: there is
  no rules-only prompt and no frozen coaching bundle to select.
- Persona: the scenario fixes the character brief; play.py refuses --character-brief / --roleplay that contradict it,
  and has no persona switch on top.
- Reloads: load_game can be left on, but play.py does not cap reloads at R (it only counts them: the record's
  `reloads`), so "success within R reloads" is graded after the run.
- Dice seed (scenario game_settings.seed, D9): no flag; the mod's setup RPC must apply it after every load.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from bg3bench import conditions

TOOLS_OFF_ENV = "BG3AI_TOOLS_OFF"
GAP_RULES_PROMPT = "a rules-only prompt (play.py always plays play/CLAUDE.md + knowledge/playbooks, coaching mixed in)"
GAP_COACHING = "a frozen coaching bundle selected by hash (no prompt-bundle flag)"
GAP_PERSONA = "a persona switch on top of the scenario's character brief"
GAP_RELOADS = "a reload cap at R (play.py counts reloads, never stops at R; grade within-R after the run)"
GAP_SEED = "the dice seed after every load (game_settings.seed: the mod setup RPC, then a play.py hook)"


@dataclass(frozen=True)
class Entry:
    condition: str
    flags: tuple[str, ...]
    env: dict[str, str]
    gaps: tuple[str, ...]


def entry(cid: str) -> Entry:
    c = conditions.parse(cid)
    gaps = [GAP_COACHING if c.coaching else GAP_RULES_PROMPT]
    if c.persona != "neutral":
        gaps.append(GAP_PERSONA)
    if c.reloads:
        gaps.append(GAP_RELOADS)
    gaps.append(GAP_SEED)
    off = c.removed_tools()
    return Entry(c.id, (), {TOOLS_OFF_ENV: ",".join(off)} if off else {}, tuple(gaps))


def table(cids: Iterable[str], *, allow_gaps: bool = False) -> tuple[dict[str, list[str]], dict[str, dict[str, str]]]:
    """(condition_flags, condition_env) for LegacyCli. A condition with gaps is refused unless allow_gaps."""
    flags, env = {}, {}
    for cid in cids:
        e = entry(cid)
        if e.gaps and not allow_gaps:
            raise ValueError(f"play.py can't play {cid!r} yet: it lacks " + "; ".join(e.gaps))
        flags[cid], env[cid] = list(e.flags), dict(e.env)
    return flags, env

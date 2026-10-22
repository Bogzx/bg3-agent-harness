"""E2E plan 3 (research/ovh/pkg-harness-analysis.md H): one session per tool group through the real bridge (bg3ai.server
over stdio) against the scripted fake mod. Each tool's text must match its golden (fixtures/golden/bridge/<group>.txt;
regenerate with BG3AI_UPDATE_GOLDEN=1 after a deliberate change and say why in the commit), with the mod ops it
called (sorted, events.since left out), and each call must stay within its budget of evals (round trips; one eval may
carry several ops). The live latency lever is the RPC count per tool (combat_turn 19, click_on 22 live), so a change
that adds an op or a round trip to a read fails here first. events.since is left out of the golden and the budget
has one eval of slack because a drain the previous call left in the background may land in this call's window.

The groups the scripted model answers: reads (status, party, character, inventory, nearby, journal, events), combat
reads, dialogue. Trade, load and area are replayed from the live recordings in test_fixtures_live4/6.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any

import pytest
from bg3harness.testing.fakemod import FakeMod
from bg3harness.testing.mcp_bridge import bridge_session, call
from bg3harness.testing.scripted import FakeClock, GameModel

pytestmark = pytest.mark.e2e

TIMEOUT_S = 180
GOLDEN = Path(__file__).resolve().parent / "fixtures" / "golden" / "bridge"

Step = tuple[str, str, dict[str, Any], int, Callable[[GameModel], None] | None]  # label, tool, args, budget, before
Result = tuple[str, str, int, list[str]]  # label, text, evals, ops


def run[T](coro: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(asyncio.wait_for(coro, TIMEOUT_S))


def model() -> GameModel:
    m = GameModel(clock=FakeClock())
    m.add_nearby("Arron", can_trade=True)
    m.add_nearby("Iron Chest", item=True, dist=3.0)
    m.add_quest("Find the Nightsong", "Look for her in the Shadow-Cursed Lands.")
    return m


# (label, tool, args, max evals, change to the game before the call). Budgets: the evals measured on 2026-10-22 + 1.
GROUPS: dict[str, list[Step]] = {
    "reads": [
        ("status", "get_status", {}, 4, None),
        ("party", "get_party", {}, 4, None),
        ("character", "get_character", {}, 3, None),
        ("inventory", "get_inventory", {}, 3, None),
        ("nearby", "get_nearby", {}, 3, None),
        ("journal", "get_journal", {}, 3, None),
        ("events_loot", "get_events", {}, 4, lambda m: (m.add_gold(25), m.add_item("Ring of Protection", value=300))),
        ("status_again", "get_status", {}, 4, None),
    ],
    "combat": [
        ("combat", "get_combat_state", {}, 7, lambda m: m.start_fight(["Goblin", "Goblin Archer"])),
        ("status_fight", "get_status", {}, 4, None),
        ("events_kill", "get_events", {}, 4, lambda m: (m.kill("Goblin"), m.end_fight())),
    ],
    "dialogue": [
        ("dialogue", "get_dialogue", {}, 4, lambda m: m.open_dialogue("Arron", "What do you want?", ["Trade.", "No."])),
        ("choose", "choose_dialogue", {"option": 2}, 8, None),
        ("status_after", "get_status", {}, 4, None),
    ],
}


async def session(home: Path, steps: list[Step]) -> list[Result]:
    m = model()
    out: list[Result] = []
    with FakeMod(m) as fake:
        async with bridge_session(fake.port, home) as s:
            await call(s, "get_status")  # the first call syncs the event cursor and serves the rules once
            for label, tool, args, _, before in steps:
                if before:
                    before(m)
                n0, k0 = fake.evals, len(fake.calls)
                text = await call(s, tool, args)
                ops = sorted(f"{ctx}:{op}" for ctx, op, _ in fake.calls[k0:] if op != "events.since")
                out.append((label, text, fake.evals - n0, ops))
    return out


@pytest.mark.parametrize("group", sorted(GROUPS))
def test_group_session_matches_golden_within_budget(tmp_path: Path, group: str) -> None:
    steps = GROUPS[group]
    got = run(session(tmp_path / "home", steps))
    text = "".join(f"=== {label} ({' '.join(ops)})\n{body}\n" for label, body, _, ops in got)
    golden = GOLDEN / f"{group}.txt"
    if os.environ.get("BG3AI_UPDATE_GOLDEN"):
        golden.parent.mkdir(parents=True, exist_ok=True)
        golden.write_text(text, encoding="utf-8")
    assert text == golden.read_text(encoding="utf-8"), f"{golden.name}: regenerate with BG3AI_UPDATE_GOLDEN=1"
    over = [(label, n, budget) for (label, _, n, _), (_, _, _, budget, _) in zip(got, steps, strict=True) if n > budget]
    assert not over, f"over the RPC budget (label, evals, budget): {over}"


def test_a_second_session_reads_the_same() -> None:
    """The goldens mean something only if a session is deterministic: two runs of a group give the same text."""
    import tempfile

    with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
        first = run(session(Path(a), GROUPS["reads"]))
        second = run(session(Path(b), GROUPS["reads"]))
    assert [(t, ops) for _, t, _, ops in first] == [(t, ops) for _, t, _, ops in second]

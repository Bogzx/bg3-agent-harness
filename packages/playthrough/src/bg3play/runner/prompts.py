"""The playthrough's prompt texts (moved from bg3harness.runner.policy, Phase D.2): a session's kickoff and the
notes only a playthrough gives (a scenario run's first session, the fix-notes session, the stuck directive). Pure."""

from __future__ import annotations

from typing import Any

from bg3harness.runner import env as renv
from bg3harness.runner.policy import PACE_PREFLIGHT, PROGRESS_MIN_POINTS, ROLEPLAY_TEXT


def build_kickoff(
    sid: str,
    playthrough: str,
    loaded_save: str | None,
    notes: list[str],
    briefing: str = "",
    save_why: str = "",
    pace: str = PACE_PREFLIGHT,
) -> str:
    """The first message of a session. `briefing` is session.py start's memory injection (the runner opened the
    session): it leads the message, where the SessionStart hook used to put it. Without it (the runner couldn't open
    the session) the agent calls session_start. It names no deadline: every time shown before WRAP UP NOW was read
    as a clock to beat (S066-S082 wrapped up 7-13 min early)."""
    lines = [
        f"This is session {sid} of the BG3 playthrough '{playthrough}', started by the autonomous runner.",
        (
            "Your memory briefing (PLAYTHROUGH.md and HANDOFF.md) is above. "
            if briefing
            else "First call session_start: it shows your memory briefing (PLAYTHROUGH.md and HANDOFF.md). "
        )
        + "Run the session-start playbook (memory_read knowledge/playbooks/session-start.md), then "
        "play following CLAUDE.md. Your notes are files under memory/: use the memory_* tools and note.",
        "Keep playing until the ⏱ line in tool results says WRAP UP NOW: you'll be told when to wrap up. Then run "
        "the session-wrapup playbook; its last step is end_session, and you stop (no more tool calls) when that "
        "reports the commit. Nobody will answer questions: decide yourself and write down why.",
        *([pace] if pace else []),
    ]
    if briefing:
        lines.insert(0, briefing)
    if loaded_save:
        why = f" ({save_why})" if save_why else ""
        lines.append(
            f'The runner has already loaded save "{loaded_save}"{why}. Don\'t reload it; verify state with get_status.'
        )
    lines.extend(notes)
    return "\n\n".join(lines)


def _roleplay(character: dict[str, Any]) -> str:
    mode = character.get("roleplay")
    return ROLEPLAY_TEXT[mode if mode in ROLEPLAY_TEXT else "strict"]


def scenario_start_note(scenario: str | None, profile: dict[str, Any], loaded: str | None) -> str:
    """The first session of a scenario run that starts from a save: no new game, the character is the save's."""
    c: dict[str, Any] = profile.get("character") or {}
    lines = [
        f"RUNNER NOTE: SCENARIO START ({scenario}). This run starts mid-game from a fixed save, not a new game: the "
        f'runner loaded "{loaded or "the start save"}". The character'
        + (f" ({c['name']})" if c.get("name") else "")
        + " is the save's: name, race, class, level, party, gear and "
        "journal are what the game shows. Your memory is new; HANDOFF.md holds the harness's short start note. "
        "Before playing on: get_status and get_party, then write memory/CHARACTER.md (the format in "
        "knowledge/playbooks/character-creation.md; header lines from the game) with the persona from the brief "
        "below, the party in party-build.md and the Character line in PLAYTHROUGH.md. Never start a new game.",
    ]
    if c.get("brief"):
        lines.append("Character brief (the operator's words):\n> " + str(c["brief"]).strip().replace("\n", "\n> "))
    if c.get("constraints"):
        lines.append(f"Constraints: {c['constraints']}")
    lines.append(_roleplay(c))
    return "\n".join(lines)


def fix_notes_prompt(lint_report: str) -> str:
    return (
        "The session is over, but `session.py end` refused to commit your memory because lint "
        "failed. Do NOT play or touch the game. Only fix the memory files so lint passes (memory_read, "
        "memory_edit, memory_write: each result shows the file's remaining lint errors), then call end_session "
        f"and stop (no more tool calls). You have {renv.FIX_SESSION_MINUTES} minutes.\n\n"
        f"Lint report:\n{lint_report[-6000:]}"
    )


STUCK_DIRECTIVE = (
    "RUNNER DIRECTIVE, CHANGE APPROACH: the last {n} sessions ended with no measurable progress "
    f"(same place, party levels/XP and journal, under {PROGRESS_MIN_POINTS} story points, no far move: "
    "{what}). What you've been doing isn't working. "
    "Before anything else: run the stuck playbook, write three approaches in hypotheses.md that are "
    "genuinely different from the ones in your recent session logs, and pick one you haven't tried. "
    "Consider parking the blocked objective and pursuing a different quest, region or main-story lead."
)


def stuck_directive(history: list[dict[str, Any]], n: int = renv.STUCK_SESSIONS) -> str:
    sig: dict[str, Any] = next((h["end_sig"] for h in reversed(history) if h.get("end_sig")), None) or {}
    # region/subregion as the map names them; the level id stays internal (FAIRNESS.md, audit C24)
    what = ", ".join(f"{k}={v}" for k, v in sig.items() if k in ("region", "subregion")) or "n/a"
    return STUCK_DIRECTIVE.format(n=n, what=what)

"""`run --scenario-file <resolved.json>`: the scenario a caller (the bench's `python -m bg3bench.scenario resolve`)
hands the runner. The harness never imports the bench, so this is the contract between them: today's v1 scenario
(scenarios/<name>.json, validated by the runner as before) plus

- game_settings {seed, difficulty, karmic_dice}: seed goes to scn.seed as a v1 scenario's does; difficulty and
  karmic_dice are what the game must be set to: the run's settings check refuses a game that reads otherwise;
- game_settings.autoskip: false lets movies, cinematics and voiced lines play (default true: skipped); it stays in
  the v1 part, where run --scenario reads it (play.py --no-autoskip);
- timing: "realtime" or "turn_based" (BENCHMARK-PLAN.md D7), recorded in the run record.

null (or a missing key) = not fixed by the scenario.
"""

from __future__ import annotations

from typing import Any

DIFFICULTIES = ("Explorer", "Balanced", "Tactician", "Honour")
TIMINGS = ("realtime", "turn_based")
CHECKED = ("difficulty", "karmic_dice")  # the settings check compares these with the game's readout


class SettingsMismatch(Exception):  # noqa: N818 - named like the runner's other stops (Bg3leStale)
    """The game's settings contradict the scenario's (karmic dice, difficulty): the run stops. Not a RuntimeError, so
    no retry loop swallows it; an operator changes the game's options and restarts the run."""


def split(sc: Any) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    """A resolved scenario -> (the v1 scenario: game_settings holds only seed, no timing; the run's expected settings
    {difficulty, karmic_dice, timing}; errors in the extension keys). The v1 part is validated by the caller."""
    if not isinstance(sc, dict):
        return {}, {}, ["a scenario file holds a JSON object"]
    v1: dict[str, Any] = dict(sc)  # pyright: ignore[reportUnknownArgumentType]
    errs: list[str] = []
    gs = v1.pop("game_settings", None)
    timing = v1.pop("timing", None)
    want: dict[str, Any] = {"difficulty": None, "karmic_dice": None, "timing": timing}
    if gs is not None:
        if not isinstance(gs, dict):
            errs.append("game_settings must be an object")
        else:
            gsd: dict[str, Any] = dict(gs)  # pyright: ignore[reportUnknownArgumentType]
            if unknown := sorted(set(gsd) - {"seed", "autoskip", *CHECKED}):
                errs.append(
                    f"game_settings: unknown keys {', '.join(unknown)} (seed, difficulty, karmic_dice, autoskip)"
                )
            if gsd.get("autoskip", True) not in (True, False):
                errs.append("game_settings.autoskip must be true or false")
            if gsd.get("difficulty") not in (None, *DIFFICULTIES):
                errs.append(f"game_settings.difficulty must be one of {', '.join(DIFFICULTIES)} or null")
            if gsd.get("karmic_dice") not in (None, True, False):
                errs.append("game_settings.karmic_dice must be true, false or null")
            want.update(difficulty=gsd.get("difficulty"), karmic_dice=gsd.get("karmic_dice"))
            kept = {k: gsd[k] for k in ("seed", "autoskip") if k in gsd and (k == "seed" or gsd[k] is False)}
            if kept:
                v1["game_settings"] = kept
    if timing not in (None, *TIMINGS):
        errs.append(f"timing must be one of {', '.join(TIMINGS)} or null")
    return v1, want, errs


def mismatches(want: dict[str, Any], got: dict[str, Any]) -> tuple[list[str], list[str]]:
    """The settings check: (contradictions, settings the game's readout can't tell) for the keys the scenario fixes.
    A readout of null is unknown, never a contradiction. avatar_name (a bench save start: run_host.options_for) is
    the name the runner gave the avatar (game_setup): one it couldn't give is a contradiction (the agent would play
    under the save's name)."""
    bad: list[str] = []
    unknown: list[str] = []
    for k in CHECKED:
        if want.get(k) is None:
            continue
        if got.get(k) is None:
            unknown.append(k)
        elif got[k] != want[k]:
            bad.append(f"{k} is {got[k]!r}, the scenario needs {want[k]!r}")
    name = want.get("avatar_name")
    if name is not None and got.get("avatar_name") != name:
        why = f" ({got['avatar_error']})" if got.get("avatar_error") else ""
        bad.append(f"the avatar is named {got.get('avatar_name')!r}, the scenario needs {name!r}{why}")
    return bad, unknown

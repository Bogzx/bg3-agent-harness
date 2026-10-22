"""Scenario files v2 (schema/scenario.v2.schema.json): a fixed start point, a goal, caps and the game settings.

v2 adds to today's scenarios/*.json (v1, runner/runs.py `validate_scenario`, scenarios/README.md):

- `schema: 2`, `family` (fight, sandbox, choice, exploration, puzzle), `holdout` (private: never published).
- `task` and `scored_by`: the agent's task and how the run is scored, in plain words, first in the run's kickoff
  (bg3harness.runner.run_host.scenario_kickoff). They must say what the grader does (graders.grade over the goal) and
  nothing it doesn't; every scenario in packages/bench/scenarios has both.
- `goal`: a predicate of the goal language (predicates.py, D8 checked), or `until: <milestone id>` as the shorthand
  for `{"milestone": id}`. Exactly one of the two.
- `game_settings`: difficulty, karmic_dice (always false, D9), seed (D9: the dice seed the mod writes after every
  load, so each run starts from the same rolls; null = the game's own wall-clock seed), resolution, ui_scale,
  language; null = not fixed by the scenario (the setup check records what the game has). The difficulty moves here
  from `profile.difficulty`.
- `timing` (D7): `realtime` (the world runs while the model thinks; results report thinking time next to wall
  time) or `turn_based`. Exploration and sandbox are always realtime; the default is realtime for every family but
  fight. There is no think-pause: the ESC menu does not pause BG3 (research/ovh/live1.md §1), so a `think_pause`
  key is refused.
- `start.needs_save`: while a save start has no save yet (`save: null`), the exact game point the start save must
  be made at (docs/SCENARIOS-AUTHORING.md); refused once the save is set.
- `start.not_ready`: why a save start can't be run yet although it is written (a save that wasn't made, or one made
  but not registered); a save start without its save is not ready either (`not_ready`). The matrix skips both.
- `methods` (default: true for sandbox): report the method attribution (methods.attribute) with the result.
- `requires` {vision, min_context}: a model that can't meet them is reported as not applicable, never as a failure
  (`applicable`). `conditions`: the condition ids it may run under.

`load(path)` reads either version: a file without `schema` is v1 and goes through `from_v1` unchanged on disk.

`python -m bg3bench.scenario resolve <file> > resolved.json` writes the runner-facing file (`resolve`) that
`play.py run --scenario-file` and the matrix's legacy CLI adapter read.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from bg3bench import conditions, predicates

SCHEMA = 2
FAMILIES = ("fight", "sandbox", "choice", "exploration", "puzzle")
DIFFICULTIES = ("Explorer", "Balanced", "Tactician", "Honour")
ROLEPLAYS = ("strict", "light")
START_KINDS = ("new_game", "save")
CAP_KEYS = ("run_hours", "max_cost", "max_tokens")
NOTE_FIELDS = ("where", "objective", "party", "journal")
NOTE_MAX_CHARS = 1500
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,47}$")
SAVE_FOLDER_RE = re.compile(r"^(?P<owner>.+)-(?P<num>\d+)__(?P<label>.+)$")
AGENT_LABEL_RE = re.compile(r"^S\d{3}-")
# The start note is neutral game facts, never a playthrough's own history (runs.NOTE_HISTORY_RE).
NOTE_HISTORY_RE = re.compile(r"\bS\d{3}\b|claude-run|playthroughs/|runtime/|sessions/", re.IGNORECASE)
CONDITION_RE = re.compile(r"^[a-z0-9][a-z0-9+@:._-]{0,63}$")
RESOLUTION_RE = re.compile(r"^\d{3,5}x\d{3,5}$")
SEED_MAX = 2**31 - 1  # the roll streams' BaseSeed is a unix time today; any 31-bit seed fits
TIMINGS = ("realtime", "turn_based")
REALTIME_FAMILIES = ("exploration", "sandbox")  # timing must be realtime (D7)
RESETS = {  # runs.RESETS: what every run starts without; not tunable
    "new_game": {
        "saves": "after_run_start",
        "nav_state": "archive",
        "knowledge": "restore_agent_edits",
        "memory": "template",
    },
    "save": {
        "saves": "after_run_start+start_save",
        "nav_state": "archive",
        "knowledge": "restore_agent_edits",
        "memory": "template+start_note",
    },
}
KEYS = {
    "schema", "name", "title", "task", "scored_by", "about", "family", "holdout", "start", "goal", "until", "runs",
    "caps", "profile", "game_settings", "timing", "methods", "requires", "conditions", "reset",
}  # fmt: skip
GAME_SETTINGS = {
    "difficulty": None, "karmic_dice": False, "seed": None, "resolution": None, "ui_scale": None, "language": None,
    "autoskip": True,
}  # fmt: skip
REQUIRES = {"vision": False, "min_context": None}
DEFAULT_CONDITIONS = ["rules"]


class ScenarioError(ValueError):
    pass


def save_owner(folder: str | None) -> str | None:
    m = SAVE_FOLDER_RE.match(folder or "")
    return m.group("owner") if m else None


def _pos_num(v: Any) -> bool:
    return isinstance(v, int | float) and not isinstance(v, bool) and v > 0


def _str(v: Any) -> bool:
    return isinstance(v, str) and bool(v.strip())


def _check_start(st: Any, name: str, char_name: str | None) -> list[str]:
    kind = st.get("kind") if isinstance(st, dict) else None
    if kind not in START_KINDS:
        return [f"start.kind must be one of {', '.join(START_KINDS)}"]
    if kind == "new_game":
        return ["a new_game start takes no save or note"] if set(st) - {"kind"} else []
    errs = []
    if unknown := sorted(set(st) - {"kind", "save", "save_sha1", "not_ready", "note", "needs_save"}):
        errs.append(f"unknown start keys: {', '.join(unknown)}")
    save = st.get("save")
    if save is None and not _str(st.get("needs_save")):
        errs.append("a save start without a save needs start.needs_save: the game point to make it at")
    elif save is not None and "needs_save" in st:
        errs.append("start.needs_save is only for a start whose save is not made yet (save: null)")
    if save is not None and save_owner(save) is None:
        errs.append(f"start.save {save!r} is not a save folder name (<owner>-<id>__<label>)")
    elif save is not None and AGENT_LABEL_RE.match(SAVE_FOLDER_RE.match(save).group("label")):
        errs.append(f"start.save {save!r} has an Sxxx- label: re-save it as SCN-{name}")
    if st.get("save_sha1") is not None and not isinstance(st["save_sha1"], str):
        errs.append("start.save_sha1 must be a string or null")
    if "not_ready" in st and not _str(st["not_ready"]):
        errs.append("start.not_ready must say why the start can't be run yet")
    if not char_name:
        errs.append("a save start needs profile.character.name: the save fixes the character")
    elif save_owner(save) and save_owner(save) != char_name:
        errs.append(f"profile.character.name {char_name!r} must match the start save's character {save_owner(save)!r}")
    note = st.get("note")
    if not isinstance(note, dict) or not all(_str(note.get(k)) for k in ("where", "objective")):
        return errs + ["a save start needs start.note with where and objective"]
    if unknown := sorted(set(note) - set(NOTE_FIELDS)):
        errs.append(f"unknown start.note keys: {', '.join(unknown)}")
    for k in ("party", "journal"):
        if note.get(k) is not None and not (isinstance(note[k], list) and all(isinstance(x, str) for x in note[k])):
            errs.append(f"start.note.{k} must be a list of strings")
    text = "\n".join(str(x) for v in note.values() for x in (v if isinstance(v, list) else [v]))
    if len(text) > NOTE_MAX_CHARS:
        errs.append(f"start.note is {len(text)} chars > {NOTE_MAX_CHARS}")
    if m := NOTE_HISTORY_RE.search(text):
        errs.append(f"start.note mentions {m.group(0)!r}: neutral game facts only, never a playthrough's history")
    return errs


def _check_settings(gs: Any) -> list[str]:
    if not isinstance(gs, dict) or set(gs) - set(GAME_SETTINGS):
        return [f"game_settings must be an object with only {', '.join(GAME_SETTINGS)}"]
    errs = []
    if gs.get("difficulty") is not None and gs["difficulty"] not in DIFFICULTIES:
        errs.append(f"game_settings.difficulty must be one of {', '.join(DIFFICULTIES)} or null")
    if gs.get("karmic_dice", False) is not False:
        errs.append("game_settings.karmic_dice must be false: Karmic Dice is off in every scenario (D9)")
    seed = gs.get("seed")
    if seed is not None and not (isinstance(seed, int) and not isinstance(seed, bool) and 0 <= seed <= SEED_MAX):
        errs.append(f"game_settings.seed must be an integer 0..{SEED_MAX}, or null")
    if gs.get("resolution") is not None and not RESOLUTION_RE.match(str(gs["resolution"])):
        errs.append("game_settings.resolution must look like 1920x1080, or null")
    if gs.get("ui_scale") is not None and not _pos_num(gs["ui_scale"]):
        errs.append("game_settings.ui_scale must be a positive number (percent), or null")
    if gs.get("language") is not None and not _str(gs["language"]):
        errs.append("game_settings.language must be a non-empty string, or null")
    if not isinstance(gs.get("autoskip", True), bool):
        errs.append("game_settings.autoskip must be true or false (skip movies, cinematics and voiced lines)")
    return errs


def default_timing(family: Any) -> str:
    return "turn_based" if family == "fight" else "realtime"


def validate(sc: Any, name: str) -> list[str]:
    """The problems of a v2 scenario (empty: valid). name is the file's stem."""
    if not isinstance(sc, dict):
        return ["not a JSON object"]
    errs = []
    if sc.get("schema") != SCHEMA:
        errs.append(f"schema must be {SCHEMA}")
    if not NAME_RE.match(name):
        errs.append(f"file name {name!r}: use a-z, 0-9, '_' or '-'")
    if sc.get("name") != name:
        errs.append(f"name {sc.get('name')!r} must be the file name {name!r}")
    if unknown := sorted(set(sc) - KEYS - {"think_pause"}):
        errs.append(f"unknown keys: {', '.join(unknown)}")
    if not _str(sc.get("title")):
        errs.append("title is required")
    errs += [f"{k} must be a non-empty text" for k in ("task", "scored_by") if k in sc and not _str(sc[k])]
    if sc.get("family") not in FAMILIES:
        errs.append(f"family must be one of {', '.join(FAMILIES)}")
    if not isinstance(sc.get("holdout", False), bool):
        errs.append("holdout must be true or false")
    if ("goal" in sc) == ("until" in sc):
        errs.append("give exactly one of goal or until")
    elif "until" in sc and sc["until"] not in predicates.MILESTONES:
        errs.append(f"until {sc['until']!r} is not a milestone id (predicates.MILESTONES)")
    elif "goal" in sc:
        try:
            predicates.parse(sc["goal"])
        except predicates.GoalError as e:
            errs.append(str(e))
    runs_n = sc.get("runs", 3)
    if not isinstance(runs_n, int) or isinstance(runs_n, bool) or runs_n < 1:
        errs.append("runs must be an integer >= 1")
    caps = sc.get("caps")
    if (
        not isinstance(caps, dict)
        or set(caps) - set(CAP_KEYS)
        or not all(v is None or _pos_num(v) for v in caps.values())
    ):
        errs.append(f"caps must be an object with {', '.join(CAP_KEYS)} (positive numbers or null)")
    elif not caps.get("run_hours"):
        errs.append("caps.run_hours is required: every run must end")
    prof = sc.get("profile")
    char = prof.get("character") if isinstance(prof, dict) else None
    if not isinstance(char, dict) or not _str(char.get("brief")):
        errs.append("profile.character.brief is required")
        char = {}
    elif set(prof) - {"character"}:
        errs.append("profile takes only character (the difficulty is game_settings.difficulty)")
    if char.get("roleplay", "strict") not in ROLEPLAYS:
        errs.append(f"profile.character.roleplay must be one of {', '.join(ROLEPLAYS)}")
    errs += _check_start(sc.get("start"), name, char.get("name"))
    errs += _check_settings(sc.get("game_settings", GAME_SETTINGS))
    if not isinstance(sc.get("methods", False), bool):
        errs.append("methods must be true or false")
    if "think_pause" in sc:
        errs.append("think_pause was dropped: the ESC menu does not pause BG3 (D7); use timing instead")
    timing = sc.get("timing", default_timing(sc.get("family")))
    if timing not in TIMINGS:
        errs.append(f"timing must be one of {', '.join(TIMINGS)}")
    elif sc.get("family") in REALTIME_FAMILIES and timing != "realtime":
        errs.append(f"a {sc['family']} scenario is realtime: the world runs while the model thinks (D7)")
    req = sc.get("requires", REQUIRES)
    if (
        not isinstance(req, dict)
        or set(req) - set(REQUIRES)
        or not isinstance(req.get("vision", False), bool)
        or not (
            req.get("min_context") is None or (_pos_num(req["min_context"]) and isinstance(req["min_context"], int))
        )
    ):
        errs.append("requires must be {vision: bool, min_context: positive integer or null}")
    conds = sc.get("conditions", DEFAULT_CONDITIONS)
    if not (isinstance(conds, list) and conds and all(isinstance(c, str) and CONDITION_RE.match(c) for c in conds)):
        errs.append("conditions must be a non-empty list of condition ids")
    elif len(set(conds)) != len(conds):
        errs.append("conditions has duplicates")
    else:
        for c in conds:
            try:
                conditions.parse(c)
            except ValueError as e:
                errs.append(str(e))
    kind = sc["start"]["kind"] if isinstance(sc.get("start"), dict) else None
    if kind in RESETS and sc.get("reset") is not None and sc["reset"] != RESETS[kind]:
        errs.append(f"reset must be the standard one for a {kind} start (or left out)")
    return errs


def with_defaults(sc: dict) -> dict:
    """A valid v2 scenario with every optional field filled."""
    return dict(
        sc,
        runs=sc.get("runs", 3),
        holdout=sc.get("holdout", False),
        caps={k: sc["caps"].get(k) for k in CAP_KEYS},
        profile={
            "character": dict(sc["profile"]["character"], roleplay=sc["profile"]["character"].get("roleplay", "strict"))
        },
        game_settings={**GAME_SETTINGS, **sc.get("game_settings", {})},
        timing=sc.get("timing", default_timing(sc["family"])),
        methods=sc.get("methods", sc["family"] == "sandbox"),
        requires={**REQUIRES, **sc.get("requires", {})},
        conditions=list(sc.get("conditions", DEFAULT_CONDITIONS)),
        reset=RESETS[sc["start"]["kind"]],
    )


def from_v1(v1: dict, family: str = "exploration") -> dict:
    """Today's scenarios/*.json (v1) -> v2. All of today's files are exploration legs (BENCHMARK-PLAN.md family 4);
    `profile.difficulty` moves to `game_settings.difficulty`, `until` stays the shorthand, and a save start
    without its save gets `needs_save` from its note's `where`."""
    out = {k: v for k, v in v1.items() if k != "profile"}
    profile = dict(v1.get("profile") or {})
    difficulty = profile.pop("difficulty", None)
    out.update(schema=SCHEMA, family=family, profile=profile, game_settings={**GAME_SETTINGS, "difficulty": difficulty})
    st = out.get("start")
    if isinstance(st, dict) and st.get("kind") == "save" and st.get("save") is None and "needs_save" not in st:
        # v1 had no needs_save: a v1 save start without its save is made where its note says
        out["start"] = {**st, "needs_save": str((st.get("note") or {}).get("where") or "see the v1 file")}
    return out


def goal_of(sc: dict) -> dict:
    """The scenario's goal predicate (`until` expanded)."""
    return {"milestone": sc["until"]} if "until" in sc else sc["goal"]


def not_ready(sc: dict) -> str | None:
    """Why a loaded scenario can't be run yet (start.not_ready, or a save start without its save); None: it can."""
    st = sc["start"]
    if st["kind"] != "save":
        return None
    if st.get("not_ready"):
        return str(st["not_ready"])
    return None if st.get("save") else "no start save yet (start.needs_save: where to make it)"


def digest(sc: Mapping[str, Any]) -> str:
    """A short hash of a scenario's content (as runs.scenario_digest): a series refuses an edited scenario."""
    return hashlib.sha1(json.dumps(sc, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]


def applicable(sc: dict, *, vision: bool, context: int | None) -> tuple[bool, str | None]:
    """Can a model with these capabilities run it? Not applicable is reported, never scored as a failure."""
    req = {**REQUIRES, **sc.get("requires", {})}
    if req["vision"] and not vision:
        return False, "needs vision"
    if req["min_context"] is not None and (context is None or context < req["min_context"]):
        return False, f"needs a context of at least {req['min_context']} tokens"
    return True, None


def load(path: str | Path, family: str = "exploration") -> dict:
    """A scenario file (v1 or v2), validated, as v2 with defaults. family applies to v1 files only."""
    path = Path(path)
    try:
        sc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise ScenarioError(f"{path}: {e}") from None
    if isinstance(sc, dict) and "schema" not in sc:
        sc = from_v1(sc, family)
    if errs := validate(sc, path.stem):
        raise ScenarioError(f"{path}: " + "; ".join(errs))
    return with_defaults(sc)


RESOLVED_SETTINGS = ("difficulty", "karmic_dice", "seed")


def resolve(sc: dict) -> dict:
    """A loaded v2 scenario (`load`) as the runner reads it: today's v1 shape (scenario_v1, scenarios/README.md)
    plus `game_settings` {difficulty, karmic_dice, seed} and `timing`. The difficulty is also `profile.difficulty`,
    where v1 has it. The legacy runner stops at a milestone only (play.py --until), so a goal other than one milestone
    is refused. The bench's own fields (family, holdout, methods, requires, conditions) and the settings a run only
    records (resolution, ui_scale, language) are left out; so are `start.needs_save` and `start.not_ready` (v1: a save
    start with save null is a placeholder the runner refuses)."""
    goal = goal_of(sc)
    if set(goal) != {"milestone"}:
        raise ScenarioError(
            f"{sc['name']}: goal {json.dumps(goal)} is not one milestone: the legacy runner stops at a milestone only"
        )
    gs = sc["game_settings"]
    out = {"name": sc["name"], "title": sc["title"]}
    if "about" in sc:
        out["about"] = sc["about"]
    out.update(
        start={k: v for k, v in sc["start"].items() if k not in ("needs_save", "not_ready")},
        until=goal["milestone"],
        runs=sc["runs"],
        caps=dict(sc["caps"]),
        profile={"character": dict(sc["profile"]["character"]), "difficulty": gs["difficulty"]},
        reset=sc["reset"],
        # autoskip only when off: a default scenario resolves as before (the series' frozen digest stays valid)
        game_settings={k: gs[k] for k in RESOLVED_SETTINGS} | ({"autoskip": False} if gs["autoskip"] is False else {}),
        timing=sc["timing"],
    )
    return out


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m bg3bench.scenario", description="Scenario files v2.")
    sub = ap.add_subparsers(dest="command", required=True)
    sub.add_parser("resolve", help="print the runner's file (v1 shape + game_settings, timing)").add_argument("file")
    args = ap.parse_args(argv)
    try:
        resolved = resolve(load(args.file))
    except ScenarioError as e:
        print(f"refusing: {e}", file=sys.stderr)
        return 2
    sys.stdout.write(json.dumps(resolved, indent=1, ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":  # pragma: no cover - python -m bg3bench.scenario
    raise SystemExit(main())

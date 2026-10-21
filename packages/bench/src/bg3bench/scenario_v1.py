"""Scenario files v1 (scenarios/<name>.json, the legacy runner's format; moved from runner/runs.py, which
re-exports them). packages/bench/scenarios/ (v2, bg3bench.scenario) is the canonical set; scenarios/*.json stays
for runner/play.py run --scenario until Phase D ends (scenarios/README.md).

A scenario is a fixed start point for a series: a new game or a start save, the milestone it ends at, the caps,
the character profile and the per-run resets. `play.py run --scenario NAME` freezes it into series.json.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from bg3bench.scenario import (
    AGENT_LABEL_RE,
    NAME_RE,
    NOTE_FIELDS,
    NOTE_HISTORY_RE,
    NOTE_MAX_CHARS,
    RESETS,
    SAVE_FOLDER_RE,
    START_KINDS,
)
from bg3bench.series import CAP_KEYS

# The format rules v2 kept (bg3bench.scenario): one definition for both.
SCENARIO_NAME_RE = NAME_RE
SCENARIO_KEYS = {"name", "title", "about", "start", "until", "runs", "caps", "profile", "reset", "game_settings"}
READY, NEEDS_SAVE, INVALID = "ready", "needs a start save", "invalid"


class ScenarioError(ValueError):
    pass


def save_owner_of(folder: str | None) -> str | None:
    m = SAVE_FOLDER_RE.match(folder or "")
    return m.group("owner") if m else None


def save_label_of(folder: str | None) -> str | None:
    m = SAVE_FOLDER_RE.match(folder or "")
    return m.group("label") if m else None


def _num_ok(v) -> bool:
    return v is None or (isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0)


def note_text(note: dict | None) -> str:
    """Every string of a start note, for the neutrality and length checks."""
    parts = []
    for v in (note or {}).values():
        parts += [str(x) for x in v] if isinstance(v, list) else [str(v)]
    return "\n".join(parts)


def validate_scenario(sc, name: str, milestone_ids, roleplays, difficulties) -> list[str]:
    """The problems of a scenario file's content (empty: valid). name is the file's stem; milestone_ids the ones
    run --until knows (bg3ai.milestones); roleplays/difficulties play.py's profile values."""
    if not isinstance(sc, dict):
        return ["not a JSON object"]
    errs = []
    if not SCENARIO_NAME_RE.match(name):
        errs.append(f"file name {name!r}: use a-z, 0-9, '_' or '-'")
    if sc.get("name") != name:
        errs.append(f"name {sc.get('name')!r} must be the file name {name!r}")
    if unknown := sorted(set(sc) - SCENARIO_KEYS):
        errs.append(f"unknown keys: {', '.join(unknown)}")
    gs = sc.get("game_settings", {})
    seed = gs.get("seed") if isinstance(gs, dict) else None
    if (
        not isinstance(gs, dict)
        or set(gs) - {"seed"}
        or not (seed is None or (isinstance(seed, int) and not isinstance(seed, bool) and 0 <= seed <= 2**31 - 1))
    ):
        errs.append("game_settings must be an object with only seed (an integer 0..2147483647 or null)")
    if sc.get("until") not in milestone_ids:
        errs.append(f"until {sc.get('until')!r} is not a milestone (bridge/src/bg3ai/milestones.py)")
    runs_n = sc.get("runs", 3)
    if not isinstance(runs_n, int) or isinstance(runs_n, bool) or runs_n < 1:
        errs.append("runs must be an integer >= 1")
    caps = sc.get("caps")
    if not isinstance(caps, dict) or set(caps) - set(CAP_KEYS) or not all(_num_ok(v) for v in caps.values()):
        errs.append(f"caps must be an object with {', '.join(CAP_KEYS)} (positive numbers or null)")
    elif not caps.get("run_hours"):
        errs.append("caps.run_hours is required: every run must end")
    prof = sc.get("profile")
    c = prof.get("character") if isinstance(prof, dict) else None
    if not isinstance(c, dict) or not isinstance(c.get("brief"), str) or not c["brief"].strip():
        errs.append("profile.character.brief is required (phase 0's brief, or the persona of the save's character)")
        c = {}
    if c.get("roleplay", "strict") not in roleplays:
        errs.append(f"profile.character.roleplay must be one of {', '.join(roleplays)}")
    if isinstance(prof, dict) and prof.get("difficulty") is not None and prof["difficulty"] not in difficulties:
        errs.append(f"profile.difficulty must be one of {', '.join(difficulties)}")
    st = sc.get("start")
    kind = st.get("kind") if isinstance(st, dict) else None
    if kind not in START_KINDS:
        errs.append(f"start.kind must be one of {', '.join(START_KINDS)}")
    elif kind == "new_game":
        if set(st) - {"kind"}:
            errs.append("a new_game start takes no save or note")
    else:
        save, note = st.get("save"), st.get("note")
        if save is not None and save_owner_of(save) is None:
            errs.append(f"start.save {save!r} is not a save folder name (<owner>-<id>__<label>)")
        elif save is not None and AGENT_LABEL_RE.match(save_label_of(save)):
            # An Sxxx- label is a playthrough's own save: it names that playthrough's session in the start note, and
            # its owner's other playthrough (pick_save takes any Sxxx- save of the owner) could load it.
            errs.append(f"start.save {save!r} has an Sxxx- label: re-save it as SCN-{name} (scenarios/README.md)")
        if st.get("save_sha1") is not None and not isinstance(st["save_sha1"], str):
            errs.append("start.save_sha1 must be a string or null")
        if not c.get("name"):
            errs.append("a save start needs profile.character.name: the save fixes the character")
        elif save_owner_of(save) and save_owner_of(save) != c["name"]:
            errs.append(
                f"profile.character.name {c['name']!r} must match the start save's character {save_owner_of(save)!r}"
            )
        if set(st) - {"kind", "save", "save_sha1", "note"}:
            errs.append(f"unknown start keys: {', '.join(sorted(set(st) - {'kind', 'save', 'save_sha1', 'note'}))}")
        if not isinstance(note, dict) or not all(
            isinstance(note.get(k), str) and note[k].strip() for k in ("where", "objective")
        ):
            errs.append("a save start needs start.note with where and objective (the harness's start note)")
        else:
            if set(note) - set(NOTE_FIELDS):
                errs.append(f"unknown start.note keys: {', '.join(sorted(set(note) - set(NOTE_FIELDS)))}")
            for k in ("party", "journal"):
                if note.get(k) is not None and not (
                    isinstance(note[k], list) and all(isinstance(x, str) for x in note[k])
                ):
                    errs.append(f"start.note.{k} must be a list of strings")
            text = note_text(note)
            if len(text) > NOTE_MAX_CHARS:
                errs.append(f"start.note is {len(text)} chars > {NOTE_MAX_CHARS}: keep it short")
            if m := NOTE_HISTORY_RE.search(text):
                errs.append(
                    f"start.note mentions {m.group(0)!r}: it must be neutral game facts, never a "
                    "playthrough's own history (sessions, memory)"
                )
    if kind in RESETS and sc.get("reset") is not None and sc["reset"] != RESETS[kind]:
        errs.append(f"reset must be the standard one for a {kind} start (or left out): {json.dumps(RESETS[kind])}")
    return errs


def load_scenario(path: Path, milestone_ids, roleplays, difficulties) -> dict:
    """scenarios/<name>.json, validated and with its defaults filled (runs 3, every cap key, the reset);
    ScenarioError when it can't be read or is invalid."""
    try:
        sc = json.loads(path.read_text())
    except (OSError, ValueError) as e:
        raise ScenarioError(f"{path}: {e}") from None
    errs = validate_scenario(sc, path.stem, milestone_ids, roleplays, difficulties)
    if errs:
        raise ScenarioError(f"{path}: " + "; ".join(errs))
    out = dict(
        sc, runs=sc.get("runs", 3), caps={k: sc["caps"].get(k) for k in CAP_KEYS}, reset=RESETS[sc["start"]["kind"]]
    )
    out["profile"] = dict(
        sc["profile"],
        character=dict(sc["profile"]["character"], roleplay=sc["profile"]["character"].get("roleplay", "strict")),
    )
    return out


def scenario_digest(sc: dict) -> str:
    """A short hash of a scenario's content: a series refuses a scenario file edited after it started."""
    return hashlib.sha1(json.dumps(sc, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]


def save_lsv(save_dir: Path, folder: str) -> Path | None:
    """A save folder's .lsv: the game names it after the label, <owner>-<id>__<label>/<label>.lsv (+ <label>.WebP);
    else the folder's only .lsv (a label the game rewrote). None when there's none, or several."""
    d = save_dir / folder
    label = save_label_of(folder)
    if label and (d / f"{label}.lsv").is_file():
        return d / f"{label}.lsv"
    try:
        found = [p for p in d.iterdir() if p.suffix == ".lsv" and p.is_file()]
    except OSError:
        return None
    return found[0] if len(found) == 1 else None


def save_digest(save_dir: Path, folder: str) -> str | None:
    """sha1 of a save's .lsv (save_lsv; read only); None when it isn't there."""
    lsv = save_lsv(save_dir, folder)
    if lsv is None:
        return None
    h = hashlib.sha1()
    try:
        with open(lsv, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
    except OSError:
        return None
    return h.hexdigest()


def scenario_status(sc: dict, folders: set[str], sha1_of=None) -> tuple[str, str]:
    """(READY | NEEDS_SAVE, detail) of a loaded scenario: a save start is ready when its save is set and present
    (and, with start.save_sha1, unchanged: sha1_of(folder) is save_digest)."""
    st = sc["start"]
    if st["kind"] == "new_game":
        return READY, "new game"
    save = st.get("save")
    if not save:
        return NEEDS_SAVE, "start.save is a placeholder (scenarios/README.md: how to make it)"
    if save not in folders:
        return NEEDS_SAVE, f"start save {save} is not in the save folder"
    if st.get("save_sha1") and sha1_of is not None:
        got = sha1_of(save)
        if got != st["save_sha1"]:
            return NEEDS_SAVE, f"start save {save} changed (sha1 {got} != the scenario's {st['save_sha1']})"
    return READY, f"save {save}"


def start_note(sc: dict) -> str:
    """The memory's HANDOFF.md for a run that starts from a save: the scenario's fixed start note (written by the
    operator from the game's screens when making the save) rendered the same way for every run and every model.
    Pure: nothing from the game or another playthrough goes in."""
    note = sc["start"]["note"]
    label = save_label_of(sc["start"].get("save")) or "(the scenario's start save)"
    lines = [
        "# Handoff S000 → S001",
        "",
        f'Save to load: "{label}" — the scenario\'s start save (the runner loads it)',
        "",
        (
            f"<!-- Written by the runner from scenarios/{sc['name']}.json: a neutral start note, the same for every "
            "run. This run starts mid-game from a fixed save; nothing else from before it is in your memory. -->"
        ),
        "",
        "## Doing right now",
        f"Scenario start: {sc.get('title') or sc['name']}.",
        f"Where: {note['where'].strip()}",
    ]
    if note.get("party"):
        lines.append("Party: " + ", ".join(note["party"]))
    lines += [
        "",
        "## Next steps",
        f"1. {note['objective'].strip()}",
        (
            "2. Read the character and party back (get_status, get_party) and write CHARACTER.md and party-build.md "
            "from what the game shows."
        ),
        "",
        "## Journal at the start save",
    ]
    lines += [f"- {q}" for q in note.get("journal") or []] or ["- (empty)"]
    lines += ["", "## Watch out", "", "## Unresolved", "", "## Discrepancies found this session", ""]
    return "\n".join(lines)


def scenario_cli_conflicts(sc: dict, cli: dict) -> list[str]:
    """CLI flags that contradict the scenario (run --scenario refuses them; caps go through series_caps): a flag
    left out takes the scenario's value, the same value is fine. cli: until, character_brief, roleplay, difficulty,
    save_owner, seed (None = not given)."""
    c = sc["profile"]["character"]
    want = {
        "until": ("--until", sc["until"]),
        "character_brief": ("--character-brief", c["brief"]),
        "roleplay": ("--roleplay", c.get("roleplay", "strict")),
        "difficulty": ("--difficulty", sc["profile"].get("difficulty")),
        "save_owner": ("--save-owner", c.get("name")),
        "seed": ("--seed", (sc.get("game_settings") or {}).get("seed")),
    }
    out = []
    for k, (flag, have) in want.items():
        v = cli.get(k)
        if v is not None and (v.strip() if isinstance(v, str) else v) != have:
            out.append(f"{flag} {v!r} contradicts scenario {sc['name']} ({have!r})")
    return out


def scenario_series_check(meta: dict, sc: dict, base: str, save_sha1: str | None) -> str | None:
    """Before a scenario series run: a refusal (str) when runtime/sessions/<base>/series.json belongs to another
    scenario or none, the scenario file changed since the series froze it, or its start save changed; else None."""
    if not meta:
        return None
    frozen = meta.get("scenario")
    if not frozen:
        return f"series {base} was started without a scenario: pick another --playthrough for scenario {sc['name']}"
    if frozen.get("name") != sc["name"]:
        return f"series {base} plays scenario {frozen.get('name')}, not {sc['name']}"
    if frozen.get("digest") != scenario_digest(sc):
        return (
            f"scenarios/{sc['name']}.json changed since series {base} started (digest {frozen.get('digest')} → "
            f"{scenario_digest(sc)}): restore it, or start a new series with another --playthrough"
        )
    if frozen.get("start_save_sha1") and frozen["start_save_sha1"] != save_sha1:
        return (
            f"the start save {sc['start'].get('save')} changed since series {base} started (sha1 "
            f"{frozen['start_save_sha1']} → {save_sha1})"
        )
    return None


def scenario_freeze(sc: dict, save_sha1: str | None) -> dict:
    """series.json's "scenario": what every run of the series is held to."""
    return {"name": sc["name"], "digest": scenario_digest(sc), "start_save_sha1": save_sha1, "spec": sc}

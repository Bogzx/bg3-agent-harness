"""The luck covariate (D9): one number per run from its hidden roll log (schema/rolls.v1.schema.json).

The mod logs every roll out of the agent's sight (mod Server/RollLog.lua, read by the harness through roll.since,
bridge/src/bg3ai/referee.py) and the harness writes them one JSON object per line. A record needs `natural` (the
d20 face, null when the source did not see it); `party`, `dc`, `success`, `result`, `stream`/`streams`,
`advantage` and `disadvantage` are read when present, anything else is kept and ignored.

The run's luck is stats.luck_z over the party's view of the d20: a party roll counts as its face, a roll by anyone
else as 21 - face (an enemy's high roll is the party's bad luck). A record without `party` counts as the party's.
Rolls with advantage or disadvantage are left out (the kept face is not uniform), and so are rolls with no natural.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path

from bg3bench import stats

SIDES = 20


class RollLogError(ValueError):
    pass


def check(rec: object, where: str = "roll") -> list[str]:
    """The problems of one roll record (empty: valid)."""
    if not isinstance(rec, dict):
        return [f"{where}: not a JSON object"]
    errs = []
    nat = rec.get("natural", "missing")
    if nat == "missing":
        errs.append(f"{where}: natural is required (null when unknown)")
    elif nat is not None and not (isinstance(nat, int) and not isinstance(nat, bool) and 1 <= nat <= SIDES):
        errs.append(f"{where}: natural must be 1..{SIDES} or null")
    for k in ("party", "success", "advantage", "disadvantage"):
        if rec.get(k) is not None and not isinstance(rec[k], bool):
            errs.append(f"{where}: {k} must be true, false or null")
    for k in ("dc", "result"):
        if rec.get(k) is not None and (isinstance(rec[k], bool) or not isinstance(rec[k], int | float)):
            errs.append(f"{where}: {k} must be a number or null")
    if rec.get("stream") is not None and not isinstance(rec["stream"], str):
        errs.append(f"{where}: stream must be a string or null")
    return errs


def load(path: str | Path) -> list[dict]:
    """A rolls.jsonl file, every record checked."""
    out, errs = [], []
    for i, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except ValueError as e:
            errs.append(f"line {i}: {e}")
            continue
        errs += check(rec, f"line {i}")
        out.append(rec)
    if errs:
        raise RollLogError(f"{path}: " + "; ".join(errs))
    return out


def _counted(rec: dict) -> bool:
    return rec.get("natural") is not None and not rec.get("advantage") and not rec.get("disadvantage")


def summary(rolls: Iterable[dict]) -> dict:
    """{n, z, party, other, skipped, nat20, nat1} for a run's rolls; z is 0.0 when no roll counts. nat20/nat1 are
    the party's view (a party 20 or an enemy 1 is a nat20 for the party)."""
    rolls = list(rolls)
    faces, party, skipped = [], 0, 0
    for rec in rolls:
        if not _counted(rec):
            skipped += 1
            continue
        mine = rec.get("party") is not False
        party += mine
        faces.append(rec["natural"] if mine else SIDES + 1 - rec["natural"])
    return {
        "n": len(faces),
        "z": round(stats.luck_z(faces, SIDES), 4),
        "party": party,
        "other": len(faces) - party,
        "skipped": skipped,
        "nat20": faces.count(SIDES),
        "nat1": faces.count(1),
    }

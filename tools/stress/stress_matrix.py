"""The stress-test matrix (tools/stress/matrix.toml) as data: load, validate. Pure: text in, rows out."""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from typing import Any

ACTS = ("P", "1", "2", "3", "any")
KINDS = ("acc", "probe", "agent", "manual")
ID_RE = re.compile(r"^[A-Z]\d{1,3}$")
LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,80}$")  # a save label (the part after `<owner>-<id>__`)
# keys each check kind takes (kind included); the first tuple is required, the second optional
CHECK_KEYS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "acc": (("kind", "section", "item"), ()),
    "probe": (("kind", "script"), ("args", "ready", "why", "timeout_s")),
    "agent": (("kind", "where", "objective"), ("goal", "until", "minutes", "profile", "max_cost")),
    "manual": (("kind", "what"), ()),
}
ROW_KEYS = {"id", "situation", "act", "save", "alt_saves", "save_note", "check", "baseline"}
DEFAULT_KEYS = {
    "profile": str,
    "max_cost": float,
    "minutes": float,
    "character_brief": str,
    "min_free_gb": float,
    "max_cpu_c": float,
    "guard_wait_s": float,
    "guard_poll_s": float,
}


class MatrixError(ValueError):
    pass


@dataclass(frozen=True)
class Check:
    row: str
    index: int
    kind: str
    spec: dict[str, Any]

    @property
    def key(self) -> str:
        """The check's id in results.json and --record: `<row>#<index>` (index from 0)."""
        return f"{self.row}#{self.index}"


@dataclass(frozen=True)
class Row:
    id: str
    group: str
    situation: str
    act: str
    save: str  # "" = a new game
    checks: tuple[Check, ...]
    alt_saves: tuple[str, ...] = ()
    save_note: str = ""
    baseline: str = ""


@dataclass(frozen=True)
class Matrix:
    defaults: dict[str, Any]
    groups: tuple[tuple[str, str], ...]  # (key, title) in file order
    rows: tuple[Row, ...]
    by_id: dict[str, Row] = field(default_factory=dict)

    def check(self, key: str) -> Check:
        row_id, _, idx = key.partition("#")
        row = self.by_id.get(row_id)
        if row is None or not idx.isdigit() or int(idx) >= len(row.checks):
            raise MatrixError(f"no check {key!r} (ids look like G6#1)")
        return row.checks[int(idx)]


def _check_errors(where: str, c: Any) -> list[str]:
    if not isinstance(c, dict) or c.get("kind") not in KINDS:
        return [f"{where}: kind must be one of {', '.join(KINDS)}"]
    need, opt = CHECK_KEYS[c["kind"]]
    errs = [f"{where}: {k} is required" for k in need if c.get(k) in (None, "")]
    errs += [f"{where}: unknown key {k!r}" for k in sorted(set(c) - set(need) - set(opt))]
    if c["kind"] == "probe":
        if "args" in c and not (isinstance(c["args"], list) and all(isinstance(a, str) for a in c["args"])):
            errs.append(f"{where}: args must be a list of strings")
        if c.get("ready") is False and not c.get("why"):
            errs.append(f"{where}: ready = false needs a why")
    if c["kind"] == "agent":
        if "goal" in c and not isinstance(c["goal"], dict):
            errs.append(f"{where}: goal must be a predicate table")
        for k in ("minutes", "max_cost"):
            if k in c and not (isinstance(c[k], int | float) and not isinstance(c[k], bool) and c[k] > 0):
                errs.append(f"{where}: {k} must be a positive number")
    return errs


def _row_errors(r: Any) -> list[str]:
    if not isinstance(r, dict):
        return ["a row is not a table"]
    rid = r.get("id")
    where = f"row {rid!r}"
    errs = [] if isinstance(rid, str) and ID_RE.match(rid) else [f"{where}: id must look like G1 or M29"]
    errs += [f"{where}: unknown key {k!r}" for k in sorted(set(r) - ROW_KEYS)]
    if not isinstance(r.get("situation"), str) or not r["situation"].strip():
        errs.append(f"{where}: situation is required")
    if r.get("act") not in ACTS:
        errs.append(f"{where}: act must be one of {', '.join(ACTS)}")
    for s in [r.get("save"), *list(r.get("alt_saves") or [])]:
        if not isinstance(s, str) or (s and not LABEL_RE.match(s)):
            errs.append(f'{where}: save {s!r} must be a save label or "" (a new game)')
    checks = r.get("check")
    if not isinstance(checks, list) or not checks:
        errs.append(f"{where}: check must be a non-empty list")
    else:
        for i, c in enumerate(checks):
            errs += _check_errors(f"{where} check {i}", c)
            if isinstance(c, dict) and c.get("kind") == "agent" and r.get("save") == "" and r.get("act") != "P":
                errs.append(f"{where} check {i}: an agent check on a new game belongs to act P")
    return errs


def parse(text: str) -> Matrix:
    """matrix.toml's text -> Matrix; MatrixError listing every problem."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise MatrixError(f"matrix.toml: {e}") from None
    errs: list[str] = []
    defaults = data.get("defaults") or {}
    for k, typ in DEFAULT_KEYS.items():
        v = defaults.get(k)
        if v is None or isinstance(v, bool) or not isinstance(v, typ if typ is str else int | float):
            errs.append(f"defaults.{k} is required ({typ.__name__})")
    errs += [f"defaults: unknown key {k!r}" for k in sorted(set(defaults) - set(DEFAULT_KEYS))]
    groups: list[tuple[str, str]] = []
    rows: list[Row] = []
    for g in data.get("group") or []:
        key, title = g.get("key"), g.get("title")
        if not isinstance(key, str) or not isinstance(title, str) or set(g) - {"key", "title", "rows"}:
            errs.append(f"group {key!r}: needs key, title and rows only")
            continue
        groups.append((key, title))
        for r in g.get("rows") or []:
            row_errs = _row_errors(r)
            errs += row_errs
            if row_errs:
                continue
            checks = tuple(Check(r["id"], i, c["kind"], dict(c)) for i, c in enumerate(r["check"]))
            rows.append(
                Row(
                    id=r["id"],
                    group=key,
                    situation=r["situation"],
                    act=r["act"],
                    save=r["save"],
                    checks=checks,
                    alt_saves=tuple(r.get("alt_saves") or ()),
                    save_note=r.get("save_note", ""),
                    baseline=r.get("baseline", ""),
                )
            )
    if not groups:
        errs.append("no [[group]]")
    seen: set[str] = set()
    for r in rows:
        if r.id in seen:
            errs.append(f"row {r.id!r}: duplicate id")
        seen.add(r.id)
    if len({k for k, _ in groups}) != len(groups):
        errs.append("duplicate group keys")
    if errs:
        raise MatrixError("matrix.toml: " + "; ".join(errs))
    return Matrix(defaults=dict(defaults), groups=tuple(groups), rows=tuple(rows), by_id={r.id: r for r in rows})


def problems(m: Matrix, sections: list[str], milestone_ids: list[str]) -> list[str]:
    """What parse can't see on its own: acc sections that acceptance.py doesn't have, unknown milestones, rows that
    put one save in two acts (live6: C30 moved to an Act 1 save and kept act 2, so --act picked it wrongly)."""
    out = []
    acts: dict[str, dict[str, list[str]]] = {}
    for r in m.rows:
        if r.save and r.act != "any":  # any: the row tests nothing act-specific on its save
            acts.setdefault(r.save, {}).setdefault(r.act, []).append(r.id)
        for c in r.checks:
            if c.kind == "acc" and c.spec["section"] not in sections:
                out.append(f"{c.key}: no acceptance section {c.spec['section']!r} (have {', '.join(sections)})")
            if c.kind == "agent" and c.spec.get("until", "game_end") not in milestone_ids:
                out.append(f"{c.key}: until {c.spec['until']!r} is not a milestone")
    for save, by in acts.items():
        if len(by) > 1:
            out.append(
                f"save {save}: rows in acts " + "; ".join(f"{a} ({', '.join(ids)})" for a, ids in sorted(by.items()))
            )
    return out

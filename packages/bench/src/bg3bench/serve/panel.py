# pyright: basic
"""What the panel's screens show, read from the files and modules that exist (no second way to run a scenario):

- scenarios: packages/bench/scenarios/*.json (bg3bench.scenario.load), each with its start save's status from a
  `SavesSource`;
- profiles: profiles/*.toml (bg3harness.profiles.load); v1 offers pi only (a Claude Code run is a pilot today and
  can't be submitted);
- plan: the jobs bg3bench.matrix.plan makes of a New run form, its reserve against the machine's budget file, the
  scenarios that aren't ready, the condition gaps, and the scenarios whose cap is longer than a YouTube account
  without phone verification can upload (15 minutes);
- queue: runtime/matrix/*/progress.json (bg3bench.matrix);
- results: every run dir under runtime/sessions with manifest.json and result.json;
- doctor: bg3harness.doctor's report, refused when it isn't the shape the Doctor screen reads.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, cast

from bg3harness import doctor as hdoctor
from bg3harness import profiles as hprofiles

from bg3bench import conditions, matrix, scenario
from bg3bench.adapters import play_flags

YOUTUBE_UNVERIFIED_S = 15 * 60  # an account without phone verification uploads at most 15 minutes
OFFERED_CLIENTS = ("pi",)
NOT_OFFERED = "v1 offers pi only"
CLAUDE_PILOT = ": a Claude Code run is a pilot (it reads play/CLAUDE.md) and can't be submitted"


def not_offered(client: str) -> str:
    return NOT_OFFERED + (CLAUDE_PILOT if client == "claude" else "")


class PanelError(ValueError):
    """A request the panel refuses (400), with a message for the user."""


class SavesSource(Protocol):
    def status(self, save: str | None, sha1: str | None) -> dict[str, Any]:
        """{"status": installed|missing|wrong_hash|stray|unknown|none, "detail": str} of one scenario's save."""
        ...


@dataclass(frozen=True)
class StorySaves:
    """Presence in the Story folder only, until bg3bench.saves (sha256, stray copies) is wired in."""

    story: Path | None

    def status(self, save: str | None, sha1: str | None) -> dict[str, Any]:
        if not save:
            return {"status": "none", "detail": "this scenario starts without a save"}
        if self.story is None or not self.story.is_dir():
            return {"status": "unknown", "detail": f"no Story folder ({self.story})"}
        if (self.story / save).is_dir():
            return {"status": "installed", "detail": "folder present; its hash is not checked yet"}
        return {"status": "missing", "detail": f"not in {self.story}"}


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def load_scenarios(scen_dir: Path) -> dict[str, dict[str, Any]]:
    """Every scenario file of the suite by name; a broken one raises (the suite tests keep them valid)."""
    return {p.stem: scenario.load(p) for p in sorted(scen_dir.glob("*.json"))}


def scenario_row(name: str, sc: dict[str, Any], saves: SavesSource) -> dict[str, Any]:
    start, caps, gs = sc.get("start") or {}, sc.get("caps") or {}, sc.get("game_settings") or {}
    cap_s = round(caps["run_hours"] * 3600) if caps.get("run_hours") else None
    save, sha1 = start.get("save"), start.get("save_sha1")
    return {
        "name": name,
        "title": sc.get("title"),
        "task": sc.get("task"),
        "family": sc.get("family"),
        "holdout": bool(sc.get("holdout")),
        "save": save,
        "save_sha1": sha1,
        "seed": gs.get("seed"),
        "difficulty": gs.get("difficulty"),
        "goal": sc.get("goal"),
        "cap_s": cap_s,
        "max_cost": caps.get("max_cost"),
        "conditions": sc.get("conditions") or [],
        "not_ready": scenario.not_ready(sc),
        "save_status": saves.status(save, sha1),
    }


def profile_rows(prof_dir: Path, has_key: Callable[[str], bool]) -> list[dict[str, Any]]:
    rows = []
    for p in sorted(prof_dir.glob("*.toml")):
        try:
            prof = hprofiles.load(p)
        except (OSError, hprofiles.ProfileError) as e:
            rows.append({"name": p.stem, "error": str(e), "offered": False, "why": "the profile doesn't load"})
            continue
        key = prof.endpoint.api_key_env if prof.endpoint else None
        offered = prof.client in OFFERED_CLIENTS and prof.status != "unsupported"
        why = (
            None
            if offered
            else (not_offered(prof.client) if prof.client not in OFFERED_CLIENTS else "status unsupported")
        )
        rows.append(
            {
                "name": p.stem,
                "client": prof.client,
                "model": prof.model,
                "effort": prof.effort,
                "status": prof.status,
                "paid": prof.client in matrix.PAID_CLIENTS,
                "api_key_env": key,
                "key_set": has_key(key) if key else None,
                "offered": offered,
                "why": why,
            }
        )
    return rows


def _str_list(body: Mapping[str, Any], key: str) -> list[str]:
    v = body.get(key)
    if not isinstance(v, list) or not v or not all(isinstance(x, str) and x for x in v):
        raise PanelError(f"{key} must be a non-empty list of names")
    return v


def plan_run(
    body: Mapping[str, Any],
    *,
    scenarios: Mapping[str, dict[str, Any]],
    prof_dir: Path,
    budget: Path,
    youtube_verified: bool | None,
) -> dict[str, Any]:
    """A New run form -> what Start would queue: the jobs (repeat-major), the reserve, the budget, what's skipped
    and why, the condition gaps and the YouTube note. Writes nothing."""
    names = _str_list(body, "profiles")
    wanted = _str_list(body, "scenarios")
    condition = body.get("condition", "rules")
    repeats = body.get("repeats", 1)
    max_cost = body.get("max_cost_usd")
    allow_unverified = body.get("allow_unverified") is True
    if not isinstance(condition, str):
        raise PanelError("condition must be a condition id")
    if not isinstance(repeats, int) or isinstance(repeats, bool) or not 1 <= repeats <= 20:
        raise PanelError("repeats must be an integer from 1 to 20")
    if max_cost is not None and (isinstance(max_cost, bool) or not isinstance(max_cost, int | float) or max_cost <= 0):
        raise PanelError("max_cost_usd must be a number > 0")
    try:
        play_flags.entry(condition)
    except ValueError as e:
        raise PanelError(f"condition {condition!r}: {e}") from None
    if unknown := [s for s in wanted if s not in scenarios]:
        raise PanelError(f"unknown scenarios: {', '.join(unknown)}")
    models = []
    known = {p.stem for p in prof_dir.glob("*.toml")}
    for n in names:
        if n not in known:
            raise PanelError(f"unknown profile {n!r}")
        try:
            prof = hprofiles.load(prof_dir / f"{n}.toml")
        except (OSError, hprofiles.ProfileError) as e:
            raise PanelError(str(e)) from None
        if prof.client not in OFFERED_CLIENTS:
            raise PanelError(f"profile {n}: {not_offered(prof.client)}")
        if prof.status == "unsupported" or (prof.status != "verified" and not allow_unverified):
            raise PanelError(f"profile {n}: status {prof.status!r} (never played live; allow unverified to plan it)")
        models.append({"slug": prof.model, "client": prof.client, "effort": prof.effort, "profile": str(n)})
    skipped = {s: why for s in wanted if (why := scenario.not_ready(scenarios[s]))}
    ready = [s for s in wanted if s not in skipped]
    caps = {s: (scenarios[s].get("caps") or {}).get("max_cost") for s in ready}
    reserve_of = {s: float(max_cost) if max_cost is not None else caps[s] for s in ready}
    try:
        jobs = matrix.plan(models, ready, [condition], repeats, max_cost=reserve_of) if ready else []
    except ValueError as e:
        raise PanelError(str(e)) from None
    unpriced = sorted({j["scenario"] for j in jobs if j["client"] in matrix.PAID_CLIENTS and j["max_cost"] is None})
    try:
        b = matrix.read_budget(budget)
        budget_view: dict[str, Any] = {"file": str(budget), "cap": b["cap"], "spent": b["spent"], "error": None}
    except (OSError, ValueError) as e:
        budget_view = {"file": str(budget), "cap": None, "spent": None, "error": str(e)}
    reserve = sum(matrix.reserve(j) for j in jobs)
    fits = budget_view["error"] is None and not unpriced and budget_view["spent"] + reserve <= budget_view["cap"]
    long_caps = sorted(
        s for s in ready if ((scenarios[s].get("caps") or {}).get("run_hours") or 0) * 3600 > YOUTUBE_UNVERIFIED_S
    )
    return {
        "jobs": jobs,
        "reserve_usd": reserve,
        "budget": budget_view,
        "fits_budget": fits,
        "needs_max_cost": unpriced,
        "skipped": skipped,
        "gaps": {m["profile"]: conditions.session_gaps(condition, m["client"]) for m in models},
        "youtube": {
            "verified": youtube_verified,
            "long_scenarios": long_caps,
            "warn": bool(long_caps) and youtube_verified is not True,
        },
    }


def queue(matrix_root: Path) -> list[dict[str, Any]]:
    """Each matrix's progress.json (status only), newest update first."""
    out = []
    for p in sorted(matrix_root.glob("*/progress.json")):
        prog = _read_json(p)
        if isinstance(prog, dict):
            out.append({"matrix": p.parent.name, **prog})
    return sorted(out, key=lambda r: -(r.get("updated") or 0))


def _dir_size(d: Path) -> int:
    return sum(f.stat().st_size for f in d.rglob("*") if f.is_file()) if d.is_dir() else 0


def epoch(v: Any) -> float | None:
    """A manifest's `created` (manifest.v1: epoch seconds, a number) as the API sends it; None when it isn't one."""
    return float(v) if isinstance(v, int | float) and not isinstance(v, bool) else None


def result_row(run_dir: Path, man: dict, res: dict) -> dict[str, Any]:
    sc, model = man.get("scenario") or {}, man.get("model") or {}
    score = res.get("score") or {}
    return {
        "run_id": res.get("run_id") or man.get("run_id"),
        "dir": run_dir.name,
        "series": man.get("series"),
        "scenario": sc.get("name"),
        "model": model.get("slug"),
        "client": (man.get("client") or {}).get("name"),
        "condition": man.get("condition"),
        "created": epoch(man.get("created")),
        "passed": res.get("passed"),
        "score": score.get("gain"),
        "outcome": res.get("outcome"),
        "t_pass_s": res.get("t_pass_s"),
        "wall_s": res.get("wall_s"),
        "cost_usd": res.get("cost_usd"),
        "valid": res.get("valid"),
        "invalid_reason": res.get("invalid_reason"),
        "pilot": res.get("pilot") or [],
        "luck_z": (res.get("luck") or {}).get("z"),
        "saves_bytes": _dir_size(run_dir / "saves"),
    }


def results(sessions: Path) -> list[dict[str, Any]]:
    """Every finished run (a dir with both manifest.json and result.json), newest first."""
    rows = []
    if not sessions.is_dir():
        return rows
    for d in sorted(sessions.iterdir()):
        man, res = _read_json(d / "manifest.json"), _read_json(d / "result.json")
        if isinstance(man, dict) and isinstance(res, dict):
            rows.append(result_row(d, man, res))
    return sorted(rows, key=lambda r: r["created"] or 0.0, reverse=True)


Check = tuple[str, str, str]


def panel_checks(
    *, which: Callable[[str], str | None], env: Mapping[str, str], disk_free: Callable[[], int]
) -> list[Check]:
    """What the panel needs beyond bg3bench.preflight: ffmpeg (record, render), a display (the game and the
    recorder), disk space for recordings (1-2 GB per hour of run)."""
    out: list[Check] = []
    ff = which("ffmpeg")
    out.append(("PASS", "ffmpeg", ff) if ff else ("FAIL", "ffmpeg", "no ffmpeg on PATH: Record and Render need it"))
    disp = env.get("DISPLAY") or env.get("WAYLAND_DISPLAY")
    out.append(("PASS", "display", disp) if disp else ("FAIL", "display", "no DISPLAY: the game needs an X display"))
    free = disk_free()
    gib = free / 2**30
    if gib < 20:
        out.append(("WARN", "disk", f"{gib:.1f} GiB free: a recorded hour takes 1-2 GB, a run's saves up to 0.6 GB"))
    else:
        out.append(("PASS", "disk", f"{gib:.1f} GiB free"))
    return out


def preflight_view(lines: Sequence[Check]) -> dict[str, Any]:
    rows = [{"status": s, "name": n, "detail": d} for s, n, d in lines]
    fails = sum(r["status"] == "FAIL" for r in rows)
    warns = sum(r["status"] == "WARN" for r in rows)
    return {"checks": rows, "fails": fails, "warns": warns, "ready": fails == 0}


def harness_doctor() -> dict[str, Any]:
    """bg3harness.doctor's report(doctor()): {"ready": bool, "fails": int, "warns": int,
    "checks": [{"name", "status": PASS|WARN|FAIL, "detail", "fix"}]} ("fix" is "" for a PASS)."""
    rep = hdoctor.report(hdoctor.doctor())
    if why := doctor_problem(rep):
        raise PanelError(f"bg3harness.doctor's report isn't what the Doctor screen reads: {why}")
    return rep


DOCTOR_CHECK_KEYS = {"name": str, "status": str, "detail": str, "fix": str}


def doctor_problem(rep: Any) -> str | None:
    """Where a doctor report breaks the shape harness_doctor documents (None: it doesn't)."""
    if not isinstance(rep, dict):
        return "not an object"
    r = cast(dict[str, Any], rep)
    if not isinstance(r.get("ready"), bool) or not all(type(r.get(k)) is int for k in ("fails", "warns")):
        return "ready must be a boolean, fails and warns integers"
    checks = r.get("checks")
    if not isinstance(checks, list):
        return "checks must be a list"
    for i, c in enumerate(cast(list[Any], checks)):
        if not isinstance(c, dict) or any(
            not isinstance(cast(dict[str, Any], c).get(k), t) for k, t in DOCTOR_CHECK_KEYS.items()
        ):
            return f"checks[{i}] needs string name, status, detail and fix"
        if c["status"] not in ("PASS", "WARN", "FAIL"):
            return f"checks[{i}]: status {c['status']!r} is not PASS, WARN or FAIL"
    return None

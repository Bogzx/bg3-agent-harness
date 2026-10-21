"""The choices grader (BENCHMARK-PLAN.md family 3): what a run picked at each dialogue scene, never right or wrong.

`picks` reads the facts v1 `choice` events (the harness writes one per choose_dialogue: scene = the DIALOGUE line
shown, options, picked, picked_text, reason). `grade` reports them and, given a judge client, how well the picks fit
the scenario's character brief: bg3harness.judge's persona judge over the picks (decision D4), a 0-10 score per pick
and overall. The judge is never the model under test. `table` is the leaderboard's cross-model view: per scenario
and scene, which option each model picked in how many of its valid runs.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any, cast

from bg3harness import judge as judge_mod

Pick = dict[str, Any]
REASON_MAX = 400  # a pick's stated reason as the result keeps it (the facts keep up to the harness's limit)


def _obj(v: object) -> dict[str, Any]:
    return cast(dict[str, Any], v) if isinstance(v, dict) else {}


def _text(o: Mapping[str, Any]) -> str:
    return str(o.get("text") or "")


def picks(records: Iterable[Mapping[str, Any]]) -> list[Pick]:
    """The run's choice events in order: {t, scene, options: [text], picked, pick, reason}. `pick` is the picked
    option's text (picked_text, else the option list's, else "option <n>")."""
    out: list[Pick] = []
    for r in records:
        if r.get("kind") != "event" or r.get("ev") != "choice":
            continue
        opts = [_obj(o) for o in cast(list[object], r.get("options") or [])]
        picked = r.get("picked")
        text = r.get("picked_text") or next((_text(o) for o in opts if o.get("i") == picked), None)
        reason = r.get("reason")
        out.append(
            {
                "t": r.get("at", r.get("t")),
                "scene": str(r.get("scene") or ""),
                "options": [f"{o.get('i')}. {_text(o)}" for o in opts],
                "picked": picked,
                "pick": str(text) if text else f"option {picked}",
                "reason": str(reason)[:REASON_MAX] if reason else None,
            }
        )
    return out


def decisions(ps: Sequence[Pick]) -> list[judge_mod.Decision]:
    """The picks as the persona judge's decisions (kind dialogue, numbered from 1)."""
    out: list[judge_mod.Decision] = []
    for i, p in enumerate(ps, 1):
        speaker, _, line = p["scene"].partition(": ")
        out.append(
            {
                "id": i,
                "kind": "dialogue",
                "speaker": speaker if line else "",
                "line": line or speaker,
                "offered": p["options"],
                "chosen": p["pick"],
                "reason": p["reason"],
            }
        )
    return out


def character_of(scenario: Mapping[str, Any]) -> str | None:
    """The persona a choice scenario is judged against: its profile's character brief (and roleplay level)."""
    ch = _obj(_obj(scenario.get("profile")).get("character"))
    if not ch.get("brief"):
        return None
    return (
        f"Character: {ch.get('name') or 'the player'}\nBrief: {ch['brief']}\nRoleplay: {ch.get('roleplay') or 'light'}"
    )


def fit(
    ps: Sequence[Pick],
    character: str,
    client: Any,
    model_under_test: str | None,
    *,
    call: judge_mod.Call = judge_mod.call_client,
) -> dict[str, Any]:
    """The persona judge over the picks: {status, score, judge_model, summary?, cost_usd?, call_costs?, error?}
    and a {score, why} per pick (in `items`, by pick order). The judge never scores its own model."""
    out: dict[str, Any] = {"status": judge_mod.FAILED, "score": None, "judge_model": getattr(client, "model", None)}
    if judge_mod.same_model(out["judge_model"], model_under_test):
        return dict(out, error=f"the judge {out['judge_model']} is the model under test ({model_under_test})")
    if not ps:
        return dict(out, status=judge_mod.NO_DECISIONS)
    got = judge_mod.judge_decisions(client, character, decisions(ps), call=call)
    keep = ("status", "score", "summary", "cost_usd", "call_costs", "error")
    out.update({k: got[k] for k in keep if got.get(k) is not None})
    if got.get("items"):
        out["items"] = [{"score": d.get("score"), "why": d.get("why")} for d in got["items"]]
    return out


def grade(
    records: Iterable[Mapping[str, Any]],
    *,
    character: str | None = None,
    client: Any = None,
    model_under_test: str | None = None,
    call: judge_mod.Call = judge_mod.call_client,
) -> dict[str, Any]:
    """{picks, fit}: fit is None without a judge client or a character, else fit()'s result."""
    ps = picks(records)
    judged = fit(ps, character, client, model_under_test, call=call) if client is not None and character else None
    return {"picks": ps, "fit": judged}


def _label(run: Mapping[str, Any]) -> str:
    return run["model"] if run.get("condition") is None else f"{run['model']} · {run['condition']}"


def table(runs: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Release runs (results.v1 `runs`, valid ones with `choices`) -> one entry per (scenario, scene):
    {scenario, scene, columns: [model label], runs: {label: valid runs that reached the scene},
     rows: [{pick, counts: {label: runs that picked it}}] (most picked first), fit: {label: median fit or None}}.
    A run that answered a scene twice (a reload) counts once per distinct pick."""
    scenes: dict[tuple[str, str], dict[str, Any]] = {}
    fits: dict[str, dict[str, list[float]]] = {}
    for run in runs:
        ch = run.get("choices")
        if not run.get("valid") or not ch:
            continue
        label = _label(run)
        if ch.get("fit") is not None:
            fits.setdefault(run["scenario"], {}).setdefault(label, []).append(ch["fit"])
        seen: dict[str, set[str]] = {}
        for p in ch["picks"]:
            seen.setdefault(p["scene"], set()).add(p["pick"])
        for scene, chosen in seen.items():
            s = scenes.setdefault((run["scenario"], scene), {"runs": {}, "counts": {}})
            s["runs"][label] = s["runs"].get(label, 0) + 1
            for pick in chosen:
                cell = s["counts"].setdefault(pick, {})
                cell[label] = cell.get(label, 0) + 1
    out: list[dict[str, Any]] = []
    for (scenario, scene), s in sorted(scenes.items()):
        rows = sorted(s["counts"].items(), key=lambda kv: (-sum(kv[1].values()), kv[0]))
        columns = sorted(s["runs"])
        scenario_fits = fits.get(scenario, {})
        out.append(
            {
                "scenario": scenario,
                "scene": scene,
                "columns": columns,
                "runs": dict(s["runs"]),
                "rows": [{"pick": pick, "counts": counts} for pick, counts in rows],
                "fit": {c: _median(scenario_fits.get(c, [])) for c in columns},
            }
        )
    return out


def _median(xs: Sequence[float]) -> float | None:
    if not xs:
        return None
    s = sorted(xs)
    m = len(s) // 2
    return s[m] if len(s) % 2 else (s[m - 1] + s[m]) / 2


def release_entry(result_choices: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """A result's `choices` as a release run carries it: the scene and pick of each, and the fit score."""
    if not result_choices:
        return None
    f = _obj(result_choices.get("fit"))
    return {
        "picks": [{"scene": p["scene"], "pick": p["pick"]} for p in result_choices["picks"]],
        "fit": f.get("score") if f.get("status") == judge_mod.OK else None,
    }

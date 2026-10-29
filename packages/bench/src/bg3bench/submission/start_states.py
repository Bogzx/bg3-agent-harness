# pyright: basic
"""Start-state fingerprints (the validator's X01) of the suite's save scenarios, recorded without a model.

capture plays each start save the way a bench run starts (the live RunHost's `start`: the save loaded, the settings
checked and the avatar renamed, facts.jsonl's run_start, load and settings marks), waits as long as a run's first
facts poll comes after its kickoff (the progress loop's first poll, POLL_S: 15.5-16 s after run_start in every
2026-10-25 pilot run), takes that poll (the runner's facts_now), then ends the run (`record`: run_end, the game
stopped, the nav files and saves put back). No agent process starts, so nothing is spent. The fingerprint is
consistency.fingerprint over that facts.jsonl, the same function X01 applies to a submitted run. Each probe's run
directory (runtime/sessions/start-state-<save>-<time>/) stays as the evidence. Saves whose Story copy isn't the
listed one (its .lsv sha1), whose start fails or whose first snapshot shows no party get an error instead.

apply writes a capture file's fingerprints into saves.json and the shipped release (releases/<suite>.json) for every
scenario of each save, as `python -m bg3bench.submission.release` would build them from the new saves.json, so the
release's sync test keeps passing. It refuses another suite, a save that isn't listed with that sha1, and a save
whose recorded fingerprint differs (unless --replace).

refresh recomputes a capture file's fingerprints from its probes' run directories (their facts.jsonl, through
`checked`), so a change to consistency.fingerprint needs no new capture; a save whose run directory is missing or
whose facts no longer check is refused, and the file is left as it was.

    play.py bench start-states [--save NAME ...] [--all] [--out FILE] [--wait-s 15]   (live, the harness's host)
    python -m bg3bench.submission.start_states apply FILE [--replace]                  (offline)
    python -m bg3bench.submission.start_states refresh FILE [--sessions DIR]           (offline)

capture's default saves: the installed suite's saves.json entries without a fingerprint. --out defaults to
runtime/start-states/<suite>.json; a later capture adds to it (one entry per save, the newest kept).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, TextIO

from bg3harness.session import SessionOutcome

from bg3bench import conditions, saves, suite
from bg3bench.submission import consistency, pack, release

Json = dict[str, Any]

SCHEMA = 1
KIND = "bg3bench.start_states"
FIRST_POLL_S = 15.0  # the progress loop's poll (bg3ai.progress.POLL_S): a run's first snapshot comes this long in
PROBE_CLIENT = "claude"  # no profile, no API key: its run files are dumped offline and its process never starts
PROBE_MODEL = "no-model"
CONDITION = "rules"
SESSIONS = Path("runtime/sessions")


class Runner(Protocol):
    def facts_now(self) -> None: ...


class LiveHost(Protocol):
    """The harness's bench RunHost (bg3harness.runner.run_host.LiveRunHost) as capture uses it: start puts the
    game at a scenario's start, runs holds each open run's runner, record ends a run, abort drops it."""

    @property
    def runs(self) -> Mapping[Path, tuple[Any, ...]]: ...
    def start(self, job: Json, sc: Json, run_dir: Path) -> Any: ...
    def record(self, job: Json, sc: Json, run_dir: Path, outcome: SessionOutcome) -> Json: ...
    def abort(self, when: str = ...) -> None: ...


@dataclass
class NoAgent:
    """The session result of a probe: no agent process ran (session.AgentResult's fields)."""

    started: float
    ended: float
    reason: str = "start_state"
    exit_code: int | None = None
    fault_at: float | None = None
    claude_session: str = ""


def probe_job(sc: Json) -> Json:
    """The job a probe starts: the scenario under rules, a client that never starts (PROBE_CLIENT), no verdict."""
    cond = conditions.parse(CONDITION)
    return {
        "scenario": sc["name"],
        "client": PROBE_CLIENT,
        "model": PROBE_MODEL,
        "effort": None,
        "profile": None,
        "condition": CONDITION,
        "tools_off": cond.removed_tools(),
        "prompt_condition": cond.prompt_condition(),
        "verdict": lambda: None,
    }


def read_facts(path: Path) -> list[Json]:
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            break
    return out


def probe(
    host: LiveHost,
    sc: Json,
    run_dir: Path,
    wait_s: float = FIRST_POLL_S,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.time,
) -> list[Json]:
    """One start save played to its first facts poll and ended: the probe's facts.jsonl records."""
    job = probe_job(sc)
    host.start(job, sc, run_dir)
    t0 = clock()
    try:
        sleep(wait_s)
        host.runs[run_dir][0].facts_now()
    except BaseException:
        host.abort("start-state probe failed")
        raise
    host.record(job, sc, run_dir, SessionOutcome(NoAgent(t0, clock()), t0))
    return read_facts(run_dir / "facts.jsonl")


def checked(rows: list[Json], save: str) -> tuple[Json | None, str]:
    """(fingerprint, "") of a probe's facts, or (None, why) when they don't show the save's start."""
    loads = [r.get("detail") for r in rows if r.get("kind") == "mark" and r.get("mark") == "load"]
    if not loads or loads[0] != save:
        return None, f"the probe's first load mark is {loads[0] if loads else None}, not {save}"
    fp = consistency.fingerprint(rows)
    if fp is None:
        return None, "no facts snapshot"
    if not fp["party"]:
        return None, "the first snapshot shows no party (the game wasn't in the level yet)"
    return fp, ""


def default_out(repo: Path, tag: str) -> Path:
    return repo / "runtime" / "start-states" / f"{tag}.json"


def read_capture(path: Path, tag: str) -> Json:
    if not path.is_file():
        return {"schema": SCHEMA, "kind": KIND, "suite": tag, "saves": {}, "errors": {}}
    doc = json.loads(path.read_text(encoding="utf-8"))
    if doc.get("kind") != KIND or doc.get("schema") != SCHEMA or doc.get("suite") != tag:
        raise ValueError(f"{path}: not a {tag} start-states file")
    return doc


def _write(path: Path, doc: Json) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")


def capture(
    host: LiveHost,
    index: Mapping[str, saves.Save],
    scenarios: Mapping[str, Json],
    names: Sequence[str],
    out: Path,
    sessions: Path,
    save_sha1: Callable[[str], str | None],
    *,
    tag: str,
    wait_s: float = FIRST_POLL_S,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.time,
    log: Callable[[str], None] = print,
) -> Json:
    """Probe each named save (one of its scenarios) and record its fingerprint or why not into `out`, written after
    every save so a stopped capture keeps what it has."""
    doc = read_capture(out, tag)
    for name in names:
        s = index[name]
        doc["errors"].pop(name, None)
        got = save_sha1(name)
        if got != s.save_sha1:
            doc["errors"][name] = f"the Story copy's .lsv sha1 is {got}, saves.json's {s.save_sha1}: install it first"
            log(f"{name}: skipped: {doc['errors'][name]}")
            _write(out, doc)
            continue
        sc = scenarios[s.scenarios[0]]
        run_dir = sessions / f"start-state-{name}-{time.strftime('%Y%m%d-%H%M%S', time.localtime(clock()))}"
        run_dir.mkdir(parents=True, exist_ok=True)
        log(f"{name}: probing with scenario {sc['name']} into {run_dir}")
        try:
            rows = probe(host, sc, run_dir, wait_s, sleep, clock)
        except Exception as e:  # noqa: BLE001 - a start that failed (the host cleaned up): the next save still runs
            fp, why = None, f"{type(e).__name__}: {e}"
        else:
            fp, why = checked(rows, name)
        if fp is None:
            doc["errors"][name] = why
            log(f"{name}: no fingerprint: {why}")
        else:
            doc["saves"][name] = {
                "save_sha1": s.save_sha1,
                "scenario": sc["name"],
                "run_dir": run_dir.name,
                "fingerprint": fp,
            }
            same = (
                ""
                if s.fingerprint is None
                else " (= saves.json's)"
                if s.fingerprint == fp
                else " (saves.json's differs)"
            )
            log(f"{name}: {json.dumps(fp, ensure_ascii=False)}{same}")
        _write(out, doc)
    return doc


def refresh(cap: Json, sessions: Path) -> list[str]:
    """Recompute each save's fingerprint in `cap` (in place) from sessions/<run_dir>/facts.jsonl. Returns the saves
    whose fingerprint changed; raises ValueError (nothing changed) when a probe's facts are missing or don't check."""
    got = {}
    for name, entry in sorted((cap.get("saves") or {}).items()):
        facts = sessions / entry["run_dir"] / "facts.jsonl"
        if not facts.is_file():
            raise ValueError(f"{name}: no {facts}")
        fp, why = checked(read_facts(facts), name)
        if fp is None:
            raise ValueError(f"{name}: {why}")
        got[name] = fp
    changed = [n for n, fp in got.items() if cap["saves"][n]["fingerprint"] != fp]
    for n, fp in got.items():
        cap["saves"][n]["fingerprint"] = fp
    return changed


def apply(cap: Json, saves_doc: Json, release_doc: Json, *, replace: bool = False) -> list[str]:
    """Write a capture's fingerprints into saves.json's and the release's documents (in place). Returns the saves
    whose fingerprint changed; raises ValueError (nothing changed) on a refusal."""
    tag = cap.get("suite")
    if cap.get("kind") != KIND or tag != release_doc.get("suite") or tag not in saves_doc["suites"]:
        raise ValueError(f"the capture is for {tag}, the release for {release_doc.get('suite')}")
    listed = {s["name"]: s for s in saves_doc["suites"][tag]["saves"]}
    changed = []
    for name, got in sorted((cap.get("saves") or {}).items()):
        s = listed.get(name)
        if s is None or s["save_sha1"] != got.get("save_sha1"):
            raise ValueError(f"{name}: not listed in saves.json for {tag} with .lsv sha1 {got.get('save_sha1')}")
        if s.get("fingerprint") not in (None, got["fingerprint"]) and not replace:
            raise ValueError(f"{name}: saves.json records another fingerprint (--replace to overwrite it)")
        if s.get("fingerprint") != got["fingerprint"]:
            changed.append(name)
    new_saves = json.loads(json.dumps(saves_doc))
    new_release = json.loads(json.dumps(release_doc))
    for s in new_saves["suites"][tag]["saves"]:
        if s["name"] in changed:
            s["fingerprint"] = cap["saves"][s["name"]]["fingerprint"]
    for sc in new_release["scenarios"].values():
        if sc["save"] in changed:
            sc["fingerprint"] = cap["saves"][sc["save"]]["fingerprint"]
    if errs := release.errors(new_release):
        raise ValueError("the release would break release.v1: " + "; ".join(errs[:5]))
    saves_doc.clear()
    saves_doc.update(new_saves)
    release_doc.clear()
    release_doc.update(new_release)
    return changed


def main(
    argv: Sequence[str] | None = None,
    *,
    repo: Path = suite.PACKAGE_ROOT.parents[1],
    host: LiveHost | None = None,
    save_sha1: Callable[[str], str | None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    out: TextIO | None = None,
) -> int:
    out = out or sys.stdout
    ap = argparse.ArgumentParser(prog="start-states", description=(__doc__ or "").split("\n\n")[0])
    sub = ap.add_subparsers(dest="command")
    ap.add_argument("--save", action="append", default=[], help="a listed save to probe (repeat)")
    ap.add_argument("--all", action="store_true", help="every listed save, fingerprinted or not")
    ap.add_argument("--out", type=Path)
    ap.add_argument("--wait-s", type=float, default=FIRST_POLL_S)
    ap_apply = sub.add_parser("apply", help="write a capture file into saves.json and the shipped release")
    ap_apply.add_argument("file", type=Path)
    ap_apply.add_argument("--replace", action="store_true")
    ap_apply.add_argument("--saves-json", type=Path, default=saves.SAVES_JSON)
    ap_apply.add_argument("--release", type=Path)
    ap_refresh = sub.add_parser("refresh", help="recompute a capture file's fingerprints from its probes' run dirs")
    ap_refresh.add_argument("file", type=Path)
    ap_refresh.add_argument("--sessions", type=Path, help="the probes' run dirs' parent (default: runtime/sessions)")
    a = ap.parse_args(argv)
    tag = suite.suite_version()
    if a.command == "refresh":
        try:
            cap = read_capture(a.file, tag)
            changed = refresh(cap, a.sessions or repo / SESSIONS)
        except (ValueError, OSError) as e:
            print(f"refused: {e}", file=sys.stderr)
            return 1
        _write(a.file, cap)
        print(f"fingerprints recomputed for {len(cap['saves'])} saves, {len(changed)} changed", file=out)
        return 0
    if a.command == "apply":
        rel = a.release or release.default_path(tag)
        try:
            saves_doc = json.loads(a.saves_json.read_text(encoding="utf-8"))
            release_doc = json.loads(rel.read_text(encoding="utf-8"))
            changed = apply(read_capture(a.file, tag), saves_doc, release_doc, replace=a.replace)
        except (ValueError, OSError) as e:
            print(f"refused: {e}", file=sys.stderr)
            return 1
        _write(a.saves_json, saves_doc)
        _write(rel, release_doc)
        print(f"fingerprints written for {len(changed)} saves: {', '.join(changed) or '-'}", file=out)
        if missing := release.unfingerprinted(release_doc):
            print(f"still without a fingerprint: {', '.join(missing)}", file=out)
        return 0
    if host is None:
        print("refusing: capture needs the game (play.py bench start-states)", file=sys.stderr)
        return 2
    index = saves.load().get(tag, {})
    if unknown := [n for n in a.save if n not in index]:
        print(f"refusing: not a listed {tag} save: {', '.join(unknown)}", file=sys.stderr)
        return 2
    names = a.save or [n for n, s in sorted(index.items()) if a.all or s.fingerprint is None]
    if save_sha1 is None:
        from bg3harness import steam_paths

        save_sha1 = saves.DirStory(steam_paths.saves_dir()).lsv_sha1
    target = a.out or default_out(repo, tag)
    try:
        doc = capture(
            host,
            index,
            pack.load_scenarios(),
            names,
            target,
            repo / SESSIONS,
            save_sha1,
            tag=tag,
            wait_s=a.wait_s,
            sleep=sleep,
            log=lambda m: print(m, file=out, flush=True),
        )
    except ValueError as e:
        print(f"refusing: {e}", file=sys.stderr)
        return 2
    print(f"wrote {target}: {len(doc['saves'])} fingerprints, {len(doc['errors'])} errors", file=out)
    return 1 if any(n in doc["errors"] for n in names) else 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

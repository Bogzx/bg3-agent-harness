# pyright: basic
"""The submission validator: one code path for the panel (before upload) and the site's validation job (after).

`validate(bundle_dir, release)` runs the checks of bg3bench.submission.codes that the caller's `where` performs
and returns a Verdict: every check's code, ok (True, False, or None when it couldn't run here: no data to compare
with), severity and detail. A bundle is accepted when no `reject` check failed; failed `flag` checks become flags.

What the checks compare against is the released suite (Release, from the suite's release file): its versions and
pins, each scenario's digest, start save sha1 and start-state fingerprint, and each condition's tool list and served
prompt hash per client (or the condition's variant whose tools the run names: a client profile turned tool groups
off). Cross-submission data (D01 duplicates, D02 near-copies, X07 the verified cell) and the
YouTube lookups (E01, E02) are injected by the caller; without them those checks report ok None.

The T checks run only against a release with `tickets`. They compare the bundle with the ticket the site stored
(`ticket`, the job's --ticket file), and they fail closed: where the job has no ticket data T04-T06 are false, not
null, because a null never rejects. The panel has no server data, so it reports T04-T07 null.

The bundle is untrusted input: the files are read as bytes, JSON depth is capped, frames.tar is listed, never
extracted, and nothing in it is executed.

    python -m bg3bench.submission.validate BUNDLE_DIR [--release FILE] [--where panel|job] [--draft] [--ticket FILE]

--release defaults to the installed suite's release file (packages/bench/releases/<suite>.json).
"""

from __future__ import annotations

import argparse
import io
import json
import sys
import tarfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from itertools import pairwise
from pathlib import Path
from typing import Any

import bg3harness
import jsonschema
from bg3harness.run_manifest import tools_digest

import bg3bench
from bg3bench import facts, luck, scenario
from bg3bench.submission import bundle, codes, consistency, pack, scrub, ticket_hash
from bg3bench.submission.release import CODE_PIN, condition_entry, defs_digest
from bg3bench.submission.release import default_path as release_default_path
from bg3bench.submission.release import errors as release_errors

Json = dict[str, Any]
Outcome = tuple[bool | None, str]

MAX_JSON_DEPTH = 32
SCHEMA_DIR = bundle.SCHEMA_PATH.parent
FRAME_EVERY_S = 30.0
FRAME_SLACK = 2
VIDEO_SLACK_S = 3.0
VIDEO_WALL_SLACK_S = 10.0
# The times a ticket's beats are held to (T05, T06, T07). Provisional until step X1 of RUN-TICKETS.md has measured them
# over more runs.
MAX_OFFSET_S = 300  # the machine's clock against the server's
START_SLACK_S = 5  # run_start before issued_at
MAX_STALE_S = 240  # how far the lines may fall behind the server's clock
WALL_SLACK_S = 300  # wall_s past what the server watched
MAX_GAP_S = 300  # between beats inside the run
MAX_UNCOVERED = 0.5  # of wall_s


@dataclass(frozen=True)
class Release:
    """A released suite as the validator compares against it (the suite's release file)."""

    suite: str
    versions: Json  # {"harness", "bench"}
    pins: Json  # {"game_build", "bg3le_sha256", "mod_sha1"} (+ "code_sha1" from bench-v0.10)
    scenarios: dict[str, Json]  # name -> {"digest", "save_sha1", "fingerprint"}
    conditions: dict[str, Json]  # id -> {"tools": {"allowlist_sha256", "names"}, "prompts": {client: {"sha256"}}}
    tickets: Json | None = None  # {"version"}: present, its runs need a ticket from the site and the T checks run

    @classmethod
    def from_json(cls, doc: Json) -> Release:
        """From a release file (release.v1); ValueError when it isn't one."""
        if errs := release_errors(doc):
            raise ValueError("not a release.v1 file: " + "; ".join(errs[:5]))
        return cls(doc["suite"], doc["versions"], doc["pins"], doc["scenarios"], doc["conditions"], doc.get("tickets"))


@dataclass(frozen=True)
class Result:
    code: str
    ok: bool | None
    detail: str = ""

    @property
    def check(self) -> codes.Check:
        return codes.BY_CODE[self.code]


@dataclass
class Verdict:
    validator_version: str
    bundle_sha256: str | None
    checks: list[Result] = field(default_factory=list[Result])

    @property
    def failed(self) -> list[str]:
        return [r.code for r in self.checks if r.ok is False]

    @property
    def accepted(self) -> bool:
        return not any(r.ok is False and r.check.severity == "reject" for r in self.checks)

    @property
    def flags(self) -> list[str]:
        return sorted(
            {r.check.flag for r in self.checks if r.ok is False and r.check.flag and r.check.severity == "flag"}
        )

    def as_json(self) -> Json:
        return {
            "validator_version": self.validator_version,
            "bundle_sha256": self.bundle_sha256,
            "accepted": self.accepted,
            "flags": self.flags,
            "checks": [
                {"code": r.code, "ok": r.ok, "severity": r.check.severity, "detail": r.detail} for r in self.checks
            ],
        }


@dataclass(frozen=True)
class Context:
    """What the caller injects for the checks that need outside data; None: that check reports ok None."""

    duplicate: Callable[[str, str], str | None] | None = None  # (bundle sha256, run_id) -> why it is a duplicate
    near_copy: Callable[[list[tuple[str, str]]], str | None] | None = None  # (tool, args) sequence -> which copy
    reference: Callable[[str], Json | None] | None = None  # cell -> the verified cell's ranges
    oembed: Callable[[str], Json | None] | None = None  # youtube id -> oEmbed answer, None when it isn't 200
    duration: Callable[[str], float | None] | None = None  # youtube id -> seconds


NO_CONTEXT = Context()


@dataclass
class Loaded:
    """A bundle's files after the format checks passed."""

    dir: Path
    envelope: Json
    raw: dict[str, bytes]
    manifest: Json
    result: Json
    facts: list[Json]
    rolls: list[Json] | None
    events: dict[str, list[Json]]
    scenario: Json
    release_scenario: Json
    release_condition: Json

    def all_events(self) -> list[Json]:
        return [e for name in sorted(self.events) for e in self.events[name]]


def _depth(v: Any, d: int = 1) -> int:
    if isinstance(v, dict):
        return max([d, *(_depth(x, d + 1) for x in v.values())])
    if isinstance(v, list):
        return max([d, *(_depth(x, d + 1) for x in v)])
    return d


def _num(v: Any) -> bool:
    return isinstance(v, int | float) and not isinstance(v, bool)


def _no_constant(name: str) -> Any:
    raise ValueError(f"{name} is not JSON")  # Python reads NaN and Infinity; JSON (and the site's parser) doesn't


def _loads(data: bytes | str) -> Any:
    return json.loads(data, parse_constant=_no_constant)


def _jsonl(data: bytes) -> list[Any]:
    return [_loads(ln) for ln in data.decode().splitlines() if ln.strip()]


def _schema_errors(doc: Any, schema_file: str) -> list[str]:
    v = jsonschema.Draft202012Validator(json.loads((SCHEMA_DIR / schema_file).read_text()))
    return [f"{'/'.join(map(str, e.absolute_path))}: {e.message}"[:200] for e in v.iter_errors(doc)]


def _record_errors(name: str, rows: list[Any], schema: Json, limit: int = 3) -> list[str]:
    """The first `limit` records of a JSONL file that break its schema (the harness's facts.v1 and events.v1, the
    bench's fights.v1): the later checks and the behaviour card read their fields as the schema types them."""
    v = jsonschema.Draft202012Validator(schema)
    out = []
    for i, r in enumerate(rows):
        if (e := next(iter(v.iter_errors(r)), None)) is not None:
            out.append(f"{name}: line {i + 1} {'/'.join(map(str, e.absolute_path))}: {e.message}"[:200])
            if len(out) >= limit:
                break
    return out


# ------------------------------------------------------------------------------------------------- format checks


def f01_files(d: Path, env: Json | None) -> Outcome:
    present = sorted(p.name for p in d.iterdir())
    if env is None:
        return False, "no readable bundle.json"
    listed = [f.get("name") for f in env.get("files") or [] if isinstance(f, dict)]
    want = sorted([bundle.ENVELOPE, *map(str, listed)])
    if present != want:
        extra, missing = sorted(set(present) - set(want)), sorted(set(want) - set(present))
        return False, f"files differ from the list: extra {extra}, missing {missing}"
    if any(not (d / n).is_file() or (d / n).is_symlink() for n in present):
        return False, "a listed name is not a regular file"
    sizes = {f["name"]: f.get("bytes") for f in env["files"]}
    wrong = [n for n, b in sizes.items() if (d / n).stat().st_size != b]
    if wrong:
        return False, f"sizes differ from the list: {wrong}"
    total = sum((d / n).stat().st_size for n in present)
    over = [n for n in present if (d / n).stat().st_size > bundle.MAX_FILE_BYTES]
    frames = (d / bundle.FRAMES_TAR).stat().st_size if bundle.FRAMES_TAR in present else 0
    if over or total > bundle.MAX_TOTAL_BYTES or frames > bundle.MAX_FRAMES_BYTES or len(listed) > bundle.MAX_FILES:
        return False, f"over the size caps: total {total}, files over {bundle.MAX_FILE_BYTES}: {over}"
    return True, ""


def f02_hashes(raw: dict[str, bytes], env: Json) -> Outcome:
    bad = [f["name"] for f in env["files"] if bundle.sha256_bytes(raw[f["name"]]) != f["sha256"]]
    if bad:
        return False, f"sha256 differs: {bad}"
    if not bundle.is_canonical(raw[bundle.ENVELOPE]):
        return False, "bundle.json is not in canonical form"
    if env.get("run_hash") != bundle.run_hash(raw):
        return False, "run_hash doesn't recompute from the record files"
    return True, ""


def f03_schemas(raw: dict[str, bytes], env: Json) -> Outcome:
    errs = [f"bundle.json {e}" for e in _schema_errors(env, "bundle.v1.schema.json")]
    if "render.json" in raw:
        errs += [f"render.json {e}" for e in _schema_errors(json.loads(raw["render.json"]), "render.v1.schema.json")]
    for name, schema_file in (("manifest.json", "manifest.v1.schema.json"), ("result.json", "result.v1.schema.json")):
        if name not in raw:
            errs.append(f"{name} missing")
            continue
        doc = _loads(raw[name])
        errs += [f"{name} {e}" for e in _schema_errors(doc, schema_file)]
    for name, data in raw.items():
        if name.endswith(".json"):
            docs = [_loads(data)]
        elif name.endswith(".jsonl"):
            docs = _jsonl(data)
        else:
            continue
        if any(_depth(x) > MAX_JSON_DEPTH for x in docs):
            errs.append(f"{name}: JSON nested deeper than {MAX_JSON_DEPTH}")
    try:
        errs += _record_errors("facts.jsonl", facts.check(_jsonl(raw["facts.jsonl"])), bg3harness.schema("facts.v1"))
    except (KeyError, facts.FactsError) as e:
        errs.append(f"facts.jsonl: {e}")
    if "rolls.jsonl" in raw:
        errs += [f"rolls.jsonl: {p}" for i, r in enumerate(_jsonl(raw["rolls.jsonl"])) for p in luck.check(r, f"{i}")]
    names = [n for n in raw if bundle.EVENTS.fullmatch(n)]
    if not names:
        errs.append("no events log")
    for n in names:
        errs += _record_errors(n, _jsonl(raw[n]), bg3harness.schema("events.v1"))
        last = -1
        for i, e in enumerate(_jsonl(raw[n])):
            ok = isinstance(e, dict) and e.get("v") == 1 and isinstance(e.get("kind"), str)
            seq, t = (e.get("seq"), e.get("t")) if isinstance(e, dict) else (None, None)
            if not ok or not _num(t) or not isinstance(seq, int) or isinstance(seq, bool) or seq <= last:
                errs.append(f"{n}: line {i + 1} is not an events v1 record in seq order")
                break
            last = seq
    fights_schema = json.loads((SCHEMA_DIR / "fights.v1.schema.json").read_text())
    for n in (n for n in raw if bundle.FIGHTS.fullmatch(n)):
        if n.replace(".fights.", ".events.") not in raw:
            errs.append(f"{n}: no events log of its play process")
        errs += _record_errors(n, _jsonl(raw[n]), fights_schema)
    man = json.loads(raw.get("manifest.json", b"{}"))
    same = {
        "run_id": man.get("run_id"),
        "suite": (man.get("versions") or {}).get("suite"),
        "scenario": (man.get("scenario") or {}).get("name"),
        "scenario_digest": (man.get("scenario") or {}).get("digest"),
        "condition": man.get("condition"),
    }
    errs += [f"bundle.json {k} {env.get(k)!r} != manifest {v!r}" for k, v in same.items() if env.get(k) != v]
    model, client = man.get("model") or {}, man.get("client") or {}
    if env.get("model") != {k: model.get(k) for k in ("slug", "provider_requested", "reasoning_effort")}:
        errs.append("bundle.json model differs from the manifest's")
    if env.get("client") != {"name": client.get("name"), "version": client.get("version")}:
        errs.append("bundle.json client differs from the manifest's")
    # X07 picks its reference runs by this cell
    cell = f"{same['scenario']}/{model.get('slug')}/{same['condition']}"
    if (env.get("attempt") or {}).get("cell") != cell:
        errs.append(f"bundle.json attempt.cell {(env.get('attempt') or {}).get('cell')!r} != {cell!r}")
    return (False, "; ".join(errs[:10])) if errs else (True, "")


def f04_eligible(man: Json, res: Json, scenarios: dict[str, Json]) -> Outcome:
    why = pack.refusal(man, res, scenarios, [man["versions"].get("suite")])
    return (False, why) if why else (True, "")


# ----------------------------------------------------------------------------------------- pins and conditions


def p01_suite(b: Loaded, release: Release) -> Outcome:
    man, env = b.manifest, b.envelope
    errs = []
    if env["suite"] != release.suite:
        errs.append(f"suite {env['suite']} is not the release {release.suite}")
    for k in ("harness", "bench"):
        if env["versions"].get(k) != release.versions.get(k):
            errs.append(f"{k} {env['versions'].get(k)} != {release.versions.get(k)}")
    rs = b.release_scenario
    if not rs:
        errs.append(f"scenario {env['scenario']} is not in the release")
    else:
        if env["scenario_digest"] != rs.get("digest"):
            errs.append("scenario digest differs from the release")
        if man["scenario"].get("start_save_sha1") != rs.get("save_sha1"):
            errs.append(f"start save sha1 {man['scenario'].get('start_save_sha1')} != {rs.get('save_sha1')}")
    return (False, "; ".join(errs)) if errs else (True, "")


def p02_pins(man: Json, release: Release) -> Outcome:
    pins = man.get("pins") or {}
    got = {
        "game_build": pins.get("game_build"),
        "bg3le_sha256": (pins.get("bg3le") or {}).get("sha256"),
        "mod_sha1": (pins.get("mod") or {}).get("sha1"),
    }
    missing = [k for k, v in got.items() if v is None]
    if missing:
        return False, f"missing pins: {', '.join(missing)}"
    wrong = [k for k, v in got.items() if v != release.pins.get(k)]
    if wrong:
        return False, f"pins differ from the release: {', '.join(wrong)}"
    if (pins.get("repo") or {}).get("dirty") is not False:
        return False, "the repo was dirty (or not recorded)"
    want = release.pins.get(CODE_PIN)
    if want and (pins.get("repo") or {}).get(CODE_PIN) != want:  # releases before bench-v0.10 have no code pin
        return False, f"the code differs from the release's (repo.code_sha1 {(pins.get('repo') or {}).get(CODE_PIN)})"
    return True, ""


def c01_prompt_tools(b: Loaded) -> Outcome:
    rc, man = b.release_condition, b.manifest
    if not rc:
        return False, f"condition {man['condition']} is not in the release"
    want = (rc.get("prompts") or {}).get(man["client"]["name"]) or {}
    if not want:
        return False, f"the release serves no prompt to client {man['client']['name']}"
    if (man.get("prompt") or {}).get("sha256") != want.get("sha256"):
        return False, "prompt sha256 differs from the condition's"
    if (man.get("tools") or {}).get("allowlist_sha256") != (rc.get("tools") or {}).get("allowlist_sha256"):
        return False, "tool allowlist sha256 differs from the condition's"
    return True, ""


def c02_served(b: Loaded) -> Outcome:
    names = (b.release_condition.get("tools") or {}).get("names") or []
    if tools_digest(names)["allowlist_sha256"] != (b.release_condition.get("tools") or {}).get("allowlist_sha256"):
        return False, "the release's tool names don't hash to its allowlist"
    if "bg3-tools.json" not in b.raw:
        return False, "bg3-tools.json missing"
    listed = json.loads(b.raw["bg3-tools.json"])
    dumped = {t.get("name") for t in listed if isinstance(t, dict)}
    if missing := sorted(set(names) - dumped):
        return False, f"allowed tools the server didn't list: {missing[:5]}"
    want = (b.release_condition.get("tools") or {}).get("defs_sha256")
    if want and defs_digest(listed, names) != want:
        return False, "the served tool definitions differ from the release's (descriptions, parameters or hints)"
    prompt = b.manifest.get("prompt") or {}
    parts = {p.get("file"): p.get("sha256") for p in prompt.get("parts") or []}
    if "bg3-instructions.txt" not in b.raw:
        return False, "bg3-instructions.txt missing"
    if bundle.sha256_bytes(b.raw["bg3-instructions.txt"].decode().strip().encode()) != parts.get("server:instructions"):
        return False, "bg3-instructions.txt doesn't hash to the prompt's server:instructions part"
    if b.manifest["client"]["name"] == "pi" and bundle.sha256_bytes(b.raw.get("pi-system.txt", b"")) != prompt.get(
        "sha256"
    ):
        return False, "pi-system.txt doesn't hash to the prompt sha256"
    return True, ""


def c03_settings(b: Loaded) -> Outcome:
    sc, man = b.scenario, b.manifest
    errs = []
    if {k: man["caps"].get(k) for k in ("run_hours", "max_cost", "max_tokens")} != {
        k: sc["caps"].get(k) for k in ("run_hours", "max_cost", "max_tokens")
    }:
        errs.append("caps differ from the scenario's")
    gs = sc["game_settings"]
    if man.get("seed") != gs.get("seed"):
        errs.append(f"seed {man.get('seed')} != the scenario's {gs.get('seed')}")
    s = consistency.settings_mark(b.facts)
    seed = s.get("seed") if isinstance(s.get("seed"), dict) else {}
    if gs.get("seed") is not None and (seed or {}).get("applied", {}).get("base_seed") != gs.get("seed"):
        errs.append("the facts' settings mark doesn't show the seed applied")
    if gs.get("difficulty") and s.get("difficulty") != gs.get("difficulty"):
        errs.append(f"difficulty {s.get('difficulty')} != the scenario's {gs.get('difficulty')}")
    return (False, "; ".join(errs)) if errs else (True, "")


def c04_model(b: Loaded) -> Outcome:
    """The model the events say played is the manifest's: every process's session_start and init name the manifest's
    slug, and the effort the endpoint served (the usage and result events' served.effort, after the client's clamp)
    is the declared reasoning_effort. A client that reports no served effort (Claude Code) is held to the names."""
    model = b.manifest.get("model") or {}
    slug, effort = model.get("slug"), model.get("reasoning_effort")
    names, served = set(), set()
    for evs in b.events.values():
        for e in evs:
            if e.get("kind") == "session_start":
                names.add((e.get("detail") or {}).get("model"))
            elif e.get("kind") == "init":
                names.add(e.get("model"))
            if e.get("kind") in ("usage", "result"):
                served.update((e.get("served") or {}).get("effort") or ())
    errs = []
    if other := sorted(str(n) for n in names - {slug}):
        errs.append(f"the events name model {', '.join(other)}, the manifest {slug}")
    if served and served != {effort}:
        errs.append(f"served effort {', '.join(sorted(served))}, declared {effort}")
    return (False, "; ".join(errs)) if errs else (True, "")


# ------------------------------------------------------------------------------------------- evidence and scrub


def e01_video(b: Loaded, ctx: Context, submission: bool) -> Outcome:
    video = b.envelope.get("video")
    if not video or not video.get("youtube_id"):
        return (False, "no video") if submission else (None, "no video yet")
    if ctx.oembed is None:
        return None, "no YouTube lookup here"
    ans = ctx.oembed(video["youtube_id"])
    if ans is None:
        return False, "the video doesn't resolve or can't be embedded"
    if b.envelope["run_id"] not in str(ans.get("title") or ""):
        return False, "the video's title doesn't contain the run id"
    return True, ""


def _render(b: Loaded) -> Json | None:
    return json.loads(b.raw["render.json"]) if "render.json" in b.raw else None


def e02_duration(b: Loaded, ctx: Context) -> Outcome:
    video, render = b.envelope.get("video"), _render(b)
    if not video or not video.get("youtube_id") or render is None or ctx.duration is None:
        return None, "no video, render.json or duration lookup here"
    span = render["video_end"] - render["video_start"]
    got = ctx.duration(video["youtube_id"])
    if got is None:
        return None, "the video's duration is unavailable"
    wall = b.result.get("wall_s") or 0
    if abs(got - span) > VIDEO_SLACK_S or abs(span - wall) > VIDEO_WALL_SLACK_S:
        return False, f"video {got:.0f} s, render span {span:.0f} s, wall_s {wall:.0f} s"
    return True, ""


def e03_render(b: Loaded, submission: bool) -> Outcome:
    render = b.raw.get("render.json")
    if render is None:
        return (False, "render.json missing") if submission else (None, "not rendered yet")
    r = json.loads(render)  # its shape is F03's
    if r["run_hash"] != b.envelope["run_hash"]:
        return False, "render.json was made for another run (run_hash)"
    span = r["video_end"] - r["video_start"]
    known = {(name, e.get("seq")) for name, evs in b.events.items() for e in evs}
    if unknown := [e for e in r["events"] if (e["file"], e["seq"]) not in known]:
        return False, f"{len(unknown)} render events not in the events logs"
    ats = [e["at_s"] for e in r["events"]]
    if ats != sorted(ats) or any(not 0 <= a <= span for a in ats):
        return False, "render events not monotone or outside the video"
    return True, ""


def e04_frames(b: Loaded, submission: bool) -> Outcome:
    data = b.raw.get(bundle.FRAMES_TAR)
    if data is None:
        return (False, "frames.tar missing") if submission else (None, "no contact sheet yet")
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as tar:
            members = tar.getmembers()
            bad = [m.name for m in members if not m.isreg() or not bundle.FRAMES_MEMBER.fullmatch(m.name)]
            if bad:
                return False, f"frames.tar holds other entries: {bad[:3]}"
            jpegs = [(tar.extractfile(m) or io.BytesIO()).read() for m in members]
    except tarfile.TarError as e:
        return False, f"frames.tar unreadable: {e}"
    if any(not (j.startswith(b"\xff\xd8") and j.rstrip(b"\0").endswith(b"\xff\xd9")) for j in jpegs):
        return False, "a frame is not a JPEG"
    want = (b.result.get("wall_s") or 0) / FRAME_EVERY_S
    if abs(len(jpegs) - want) > FRAME_SLACK:
        return False, f"{len(jpegs)} frames for {want:.0f} expected"
    return True, ""


def s01_scrub(raw: dict[str, bytes], literals: Sequence[str]) -> Outcome:
    hits = {n: scrub.file_hits(n, d, literals) for n, d in raw.items()}
    hits = {n: h for n, h in hits.items() if h}
    if hits:
        return False, "; ".join(f"{n}: {', '.join(sorted(set(h)))}" for n, h in sorted(hits.items()))
    return True, ""


def s02_reasoning(b: Loaded) -> Outcome:
    for name, evs in sorted(b.events.items()):
        for e in evs:
            if (e.get("kind") == "thinking" and e.get("text")) or any(e.get(k) for k in ("reasoning", "thinking")):
                return False, f"{name} seq {e.get('seq')}: hidden reasoning"
    return True, ""


def _tool_sequence(b: Loaded) -> list[tuple[str, str]]:
    return [
        (str(e.get("tool")), json.dumps(e.get("args"), sort_keys=True))
        for e in b.all_events()
        if e.get("kind") == "tool_call"
    ]


# -------------------------------------------------------------------------------------------------- run tickets


def t03_recompute(raw: dict[str, bytes], env: Json) -> Outcome:
    """root and lines from the bundle's own files. A valid last line with no newline would be read by the graders and
    counted by no hash, so every stream must end in a complete line."""
    t = env["ticket"]
    if torn := sorted(n for n, d in raw.items() if ticket_hash.is_stream(n) and d and not d.endswith(b"\n")):
        return False, f"bytes after the last complete line of {', '.join(torn)}"
    want = ticket_hash.counts(raw)
    if t["lines"] != want:
        differ = sorted(n for n in {*want, *t["lines"]} if want.get(n) != t["lines"].get(n))
        return False, f"lines differ from the files: {', '.join(differ)}"
    if ticket_hash.root_at(t["nonce"], raw, want) != t["root"]:
        return False, "the root doesn't recompute from the files (a line edited, or the scrub differs from the run's)"
    return True, ""


def _beats(ticket: Json) -> list[Json]:
    return sorted(ticket["beats"], key=lambda beat: beat["s"])


def t04_beats(raw: dict[str, bytes], env: Json, ticket: Json) -> Outcome:
    """Every beat the server stored, against the bundle's files under the server's nonce."""
    if env["ticket"]["nonce"] != ticket["nonce"]:
        return False, "the nonce isn't the server's"
    beats = _beats(ticket)
    if not beats:
        return False, "the server holds no beats for this ticket"
    wanted: dict[str, set[int]] = {}
    for b in beats:
        for name, n in b["l"].items():
            wanted.setdefault(name, set()).add(n)
    try:
        hashes = {n: ticket_hash.digests_at(ticket["nonce"], n, raw.get(n, b""), c) for n, c in wanted.items()}
    except ValueError as e:
        return False, str(e)
    last: dict[str, int] = {}
    for b in beats:
        fell = sorted(n for n, c in last.items() if b["l"].get(n, 0) < c)
        if fell:
            return False, f"beat {b['s']}: the line count fell in {', '.join(fell)}"
        last = b["l"]
        if ticket_hash.root((n, c, hashes[n][c]) for n, c in b["l"].items()) != b["r"]:
            return False, f"beat {b['s']}: the root doesn't recompute (a committed line differs)"
        if b.get("e") and b["r"] != env["ticket"]["root"]:
            return False, "the end's root isn't the bundle's"
    return True, ""


def _latest_t(data: bytes) -> list[float]:
    """Per line count: the largest `t` among that many first lines of a stream ([0] is -inf: nothing covered)."""
    out = [float("-inf")]
    for line in ticket_hash.complete_lines(data):
        row = _loads(line) if line.strip() else None
        t = row.get("t") if isinstance(row, dict) else None
        out.append(max(out[-1], float(t)) if t is not None and _num(t) else out[-1])
    return out


@dataclass(frozen=True)
class BeatTimes:
    """A ticket's beats read against a run: what T05 and T06 share."""

    regular: list[Json]  # the beats that aren't the end, in order
    stale: list[tuple[Json, float]]  # (beat, seconds behind the server) of each regular beat that covers lines
    offset: float | None  # server - machine; None: no beat or end covers a line
    start: float  # run_start.t on the machine's clock
    end: float  # run_end.t on the machine's clock
    end_line: int  # how many lines of facts.jsonl it takes to cover run_end
    latest: float  # the largest t of any line


def _beat_times(b: Loaded, ticket: Json) -> BeatTimes:
    latest_t = {n: _latest_t(d) for n, d in b.raw.items() if ticket_hash.is_stream(n)}

    def behind(beat: Json) -> float | None:
        newest = max((latest_t[n][c] for n, c in beat["l"].items() if n in latest_t), default=float("-inf"))
        return beat["a"] - newest if newest != float("-inf") else None

    beats = _beats(ticket)
    regular = [x for x in beats if not x.get("e")]
    stale = [(x, s) for x in regular if (s := behind(x)) is not None]
    end_beat = next((x for x in beats if x.get("e")), None)
    offset = min((s for _, s in stale), default=behind(end_beat) if end_beat else None)
    rows = [_loads(ln) if ln.strip() else {} for ln in ticket_hash.complete_lines(b.raw[ticket_hash.FACTS])]
    marks = {r["mark"]: (i, r["t"]) for i, r in enumerate(rows, 1) if r.get("kind") == "mark"}
    _, start = marks["run_start"]  # a missing mark fails the check that asked (_guarded); more than one is X02's
    end_line, end = marks["run_end"]
    latest = max(v[-1] for v in latest_t.values())
    return BeatTimes(regular, stale, offset, start, end, end_line, latest)


def t05_times(b: Loaded, ticket: Json) -> Outcome:
    """The lines' own times against the server's. offset = min(stale): the machine's clock error plus the shortest
    delay; an end counts only for a run with no regular beat over its lines, because it may arrive days late."""
    bt = _beat_times(b, ticket)
    if bt.offset is None:
        return False, "no beat or end covers a line"
    if abs(bt.offset) > MAX_OFFSET_S:
        return False, f"the lines' times are {bt.offset:.0f} s off the server's clock"
    if bt.start + bt.offset < ticket["issued_at"] - START_SLACK_S:
        return False, f"the run started {ticket['issued_at'] - bt.start - bt.offset:.0f} s before its ticket"
    for beat, stale in bt.stale:
        # host.record writes no line after run_end: it stops the game and moves saves
        if beat["l"].get(ticket_hash.FACTS, 0) < bt.end_line and stale - bt.offset > MAX_STALE_S:
            return False, f"beat {beat['s']}: the lines were {stale - bt.offset:.0f} s behind the server's clock"
    wall, last = b.result["wall_s"], max(x["a"] for x in ticket["beats"])
    if wall > (last - ticket["issued_at"]) + WALL_SLACK_S:
        return False, f"wall_s {wall:.0f} is longer than the {last - ticket['issued_at']} s the server watched"
    if bt.latest + bt.offset > ticket["expires_at"]:
        return False, "a line's time is past the ticket's expiry"
    return True, ""


def _beat_gaps(b: Loaded, ticket: Json) -> tuple[list[float], float]:
    """(The gaps between the regular beats inside the run, its start and end counted as points; wall_s). A late end
    adds nothing here. ValueError with no offset to place the run on the server's clock."""
    bt = _beat_times(b, ticket)
    if bt.offset is None:
        raise ValueError("no beat or end covers a line")
    start, end = bt.start + bt.offset, bt.end + bt.offset
    points = [start, *sorted(x["a"] for x in bt.regular if start <= x["a"] <= end), end]
    return [y - x for x, y in pairwise(points)], float(b.result["wall_s"])


def t06_coverage(b: Loaded, ticket: Json) -> Outcome:
    gaps, wall = _beat_gaps(b, ticket)
    uncovered = sum(g - MAX_GAP_S for g in gaps if g > MAX_GAP_S)
    if uncovered > MAX_UNCOVERED * wall:
        return False, f"{uncovered:.0f} s of the {wall:.0f} s run are outside beat range"
    return True, ""


def t07_gaps(b: Loaded, ticket: Json) -> Outcome:
    gaps, _ = _beat_gaps(b, ticket)
    if over := [g for g in gaps if g > MAX_GAP_S]:
        return False, f"{len(over)} gaps between beats over {MAX_GAP_S} s, the longest {max(over):.0f} s"
    return True, ""


def _ticket_checks(b: Loaded, ticket: Json | None, where: codes.Where) -> list[tuple[str, Outcome]]:
    """T01 and T03-T07 (T02 is the Worker's, at the verdict: the job reports it null, as D01). `ticket`: what the site
    stored, None when the job got none. The panel has no server data; the job fails closed without it."""
    env = b.envelope
    named = "ticket" in env  # its form is the schema's: F03 has passed
    out: list[tuple[str, Outcome]] = [
        ("T01", (True, "") if named else (False, "bundle.json names no ticket")),
        ("T02", (None, "the site checks this at the verdict")),
        ("T03", _guarded(t03_recompute, b.raw, env) if named else (False, "no ticket to recompute")),
    ]
    if where != "job":
        return [*out, *((c, (None, "no server data here")) for c in ("T04", "T05", "T06", "T07"))]
    if ticket is None or not named:
        why = "the site holds no such ticket" if named else "bundle.json names no ticket"
        return [*out, *((c, (False, why)) for c in ("T04", "T05", "T06")), ("T07", (None, why))]
    out.append(("T04", _guarded(t04_beats, b.raw, env, ticket)))
    out.append(("T05", _guarded(t05_times, b, ticket)))
    out.append(("T06", _guarded(t06_coverage, b, ticket)))
    out.append(("T07", _guarded(t07_gaps, b, ticket)))
    return out


def read_ticket(path: Path) -> Json | None:
    """The ticket file the job wrote: the server's ticket, or None for a missing file or {"found": false}."""
    try:
        doc = _loads(path.read_bytes())
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    return doc if isinstance(doc, dict) and doc.get("found") is not False else None


# ---------------------------------------------------------------------------------------------------- the driver


def _read(d: Path) -> tuple[Json | None, dict[str, bytes]]:
    raw = {p.name: p.read_bytes() for p in sorted(d.iterdir()) if p.is_file() and not p.is_symlink()}
    try:
        env = _loads(raw[bundle.ENVELOPE])
    except (KeyError, ValueError, UnicodeDecodeError):
        return None, raw
    return (env if isinstance(env, dict) else None), raw


def validate(
    bundle_dir: str | Path,
    release: Release,
    *,
    where: codes.Where = "panel",
    submission: bool = True,
    scenarios: dict[str, Json] | None = None,
    ctx: Context = NO_CONTEXT,
    literals: Sequence[str] = (),
    version: str = bg3bench.__version__,
    ticket: Json | None = None,
) -> Verdict:
    """Check the bundle in bundle_dir against the release. `where`: which checks run (codes.Check.where).
    `submission`: the video, render.json and frames.tar are required (False: before rendering, they are not run).
    `scenarios`: the suite's scenario files by name (default the installed ones). `literals`: values S01 must not
    find (the submitter's .env values). `ticket`: what the site stored for the bundle's ticket (a release with
    `tickets`; the job's T04-T06 fail without it)."""
    d = Path(bundle_dir)
    scenarios = pack.load_scenarios() if scenarios is None else scenarios
    env, raw = _read(d)
    verdict = Verdict(version, bundle.sha256_bytes(raw[bundle.ENVELOPE]) if bundle.ENVELOPE in raw else None)
    # the job re-runs the Worker's checks too: one Python code path backs every verdict
    runs_here = {c.code for c in codes.CHECKS if where in c.where or (where == "job" and "worker" in c.where)}
    if release.tickets is None:
        runs_here = {c for c in runs_here if not c.startswith("T")}

    def add(code: str, outcome: Outcome) -> None:
        if code in runs_here:
            verdict.checks.append(Result(code, outcome[0], outcome[1]))

    try:
        add("F01", f01_files(d, env))
        if env is None or verdict.failed:
            return _skip_rest(verdict, runs_here, "the files don't match bundle.json")
        add("F02", f02_hashes(raw, env))
        add("F03", f03_schemas(raw, env))
    except (ValueError, UnicodeDecodeError, KeyError, TypeError) as e:
        add("F03", (False, f"unreadable: {type(e).__name__}: {e}"[:300]))
    add("S01", s01_scrub(raw, literals))
    if verdict.failed:
        return _skip_rest(verdict, runs_here, "the format checks failed")
    assert env is not None
    man, res = json.loads(raw["manifest.json"]), json.loads(raw["result.json"])
    add("F04", _guarded(f04_eligible, man, res, scenarios))
    if verdict.failed:
        return _skip_rest(verdict, runs_here, "not an eligible run")
    sc = scenarios[man["scenario"]["name"]]
    b = Loaded(
        dir=d,
        envelope=env,
        raw=raw,
        manifest=man,
        result=res,
        facts=_jsonl(raw["facts.jsonl"]),
        rolls=_jsonl(raw["rolls.jsonl"]) if "rolls.jsonl" in raw else None,
        events={n: _jsonl(data) for n, data in raw.items() if bundle.EVENTS.fullmatch(n)},
        scenario=scenario.with_defaults(sc),
        release_scenario=release.scenarios.get(env["scenario"]) or {},
        release_condition=condition_entry(
            release.conditions.get(env["condition"]) or {}, (man.get("tools") or {}).get("allowlist_sha256")
        ),
    )
    add("P01", _guarded(p01_suite, b, release))
    add("P02", _guarded(p02_pins, man, release))
    add("C01", _guarded(c01_prompt_tools, b))
    add("C02", _guarded(c02_served, b))
    add("C03", _guarded(c03_settings, b))
    add("C04", _guarded(c04_model, b))
    if "D01" in runs_here:
        add("D01", (None, "no submission store here") if ctx.duplicate is None else _dup(ctx.duplicate, verdict, env))
    if "D02" in runs_here:
        found = None if ctx.near_copy is None else ctx.near_copy(_tool_sequence(b))
        add("D02", (None, "no submission store here") if ctx.near_copy is None else (found is None, found or ""))
    add("X01", _guarded(consistency.x01_start_state, b))
    add("X02", _guarded(consistency.x02_time_marks, b))
    add("X03", _guarded(consistency.x03_events_vs_facts, b))
    add("X04", _guarded(consistency.x04_rolls, b))
    add("X05", _guarded(consistency.x05_tokens_cost, b))
    add("X06", _guarded(consistency.x06_resumes, b))
    if "X07" in runs_here:
        add(
            "X07",
            _guarded(
                consistency.x07_outlier, b, None if ctx.reference is None else ctx.reference(env["attempt"]["cell"])
            ),
        )
    add("G01", _guarded(consistency.g01_regrade, b))
    add("G02", _guarded(consistency.g02_losses, b))
    add("A01", _guarded(consistency.a01_fairness, b))
    add("A02", _guarded(consistency.a02_allowlist, b))
    add("E01", _guarded(e01_video, b, ctx, submission))
    if "E02" in runs_here:
        add("E02", _guarded(e02_duration, b, ctx))
    add("E03", _guarded(e03_render, b, submission))
    add("E04", _guarded(e04_frames, b, submission))
    add("S02", _guarded(s02_reasoning, b))
    if release.tickets is not None:
        for code, outcome in _ticket_checks(b, ticket, where):
            add(code, outcome)
    return _ordered(verdict)


def _guarded(check: Callable[..., Outcome], *args: Any) -> Outcome:
    """A check over the bundle's parsed files. F03 holds them to their schemas; what a check still can't read (a
    shape the schemas allow and the check doesn't expect) fails that check's code instead of the validation."""
    try:
        return check(*args)
    except (KeyError, IndexError, TypeError, ValueError, AttributeError, ArithmeticError) as e:
        return False, f"malformed input: {type(e).__name__}: {e}"[:300]


def _dup(duplicate: Callable[[str, str], str | None], verdict: Verdict, env: Json) -> Outcome:
    why = duplicate(verdict.bundle_sha256 or "", env["run_id"])
    return (False, why) if why else (True, "")


def _skip_rest(verdict: Verdict, runs_here: set[str], why: str) -> Verdict:
    done = {r.code for r in verdict.checks}
    verdict.checks += [Result(c.code, None, f"not run: {why}") for c in codes.CHECKS if c.code in runs_here - done]
    return _ordered(verdict)


def _ordered(verdict: Verdict) -> Verdict:
    """The checks in the code table's order."""
    order = {c.code: i for i, c in enumerate(codes.CHECKS)}
    verdict.checks.sort(key=lambda r: order[r.code])
    return verdict


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m bg3bench.submission.validate", description="Validate a bundle.")
    ap.add_argument("bundle_dir", type=Path)
    ap.add_argument("--release", type=Path, default=None, help="the suite's release file (default: the installed one)")
    ap.add_argument("--where", choices=("panel", "job"), default="panel")
    ap.add_argument("--draft", action="store_true", help="not rendered yet: don't require the video and frames")
    ap.add_argument("--ticket", type=Path, help='the ticket the site stored, or {"found": false} (a ticketed release)')
    a = ap.parse_args(argv)
    path = a.release or release_default_path()
    try:
        rel = Release.from_json(json.loads(path.read_text()))
    except (OSError, ValueError) as e:
        print(f"no usable release file {path}: {e}", file=sys.stderr)
        return 2
    ticket = read_ticket(a.ticket) if a.ticket else None
    v = validate(a.bundle_dir, rel, where=a.where, submission=not a.draft, ticket=ticket)
    print(json.dumps(v.as_json(), indent=1))
    return 0 if v.accepted else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

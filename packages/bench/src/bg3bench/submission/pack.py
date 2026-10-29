# pyright: basic
"""Pack a finished run into a submission bundle: <out>/bundle/ (the files + bundle.json) and <out>/bundle.tar.gz.

The bundle holds what the graders, the luck number and the checks read (BUNDLE_FILES, every play process's
normalized events log, and the renderer's render.json and frames.tar once they exist), each through the scrub. It
leaves out the raw client streams (the only files with hidden reasoning), the client's session files, logs, triage,
metrics, screenshots, memory and saves. The archive is deterministic (sorted, fixed times and owners).

Refused (PackError): a pilot run, an invalid run, a holdout or unknown scenario, a suite version that is not
released, a run without its manifest, result, facts or events, and a bundle over the size caps.

The evidence video is rendered from the packed bundle (its record files are scrubbed, and its run_hash is in
bundle.json), then add_render puts the renderer's render.json and its frames/<n>.jpg (tarred into frames.tar) into
the bundle: they are not record files, so the run_hash stays; bundle.json's files[] and the archive are rewritten.

    python -m bg3bench.submission.pack RUN_DIR OUT_DIR --attempt N [--unsubmitted-valid K]
    python -m bg3bench.submission.pack --add-render RENDER_DIR OUT_DIR    # RENDER_DIR: render.json, frames/
"""

from __future__ import annotations

import argparse
import gzip
import io
import json
import sys
import tarfile
import time
import uuid
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bg3bench import scenario, suite
from bg3bench.submission import bundle, scrub

Json = dict[str, Any]

REQUIRED = ("manifest.json", "result.json", "facts.jsonl")  # rolls.jsonl only when the mod logged rolls
OPTIONAL = ("rolls.jsonl", "bg3-tools.json", "bg3-instructions.txt", "pi-system.txt", "render.json", bundle.FRAMES_TAR)
EVENTS = bundle.EVENTS
BUNDLE_DIR = "bundle"
ARCHIVE = "bundle.tar.gz"
SCENARIO_DIR = suite.PACKAGE_ROOT / "scenarios"


class PackError(ValueError):
    pass


@dataclass(frozen=True)
class Attempt:
    n: int  # this run's number in its cell on this install
    unsubmitted_valid: int = 0  # earlier valid runs of the cell not submitted


@dataclass(frozen=True)
class Packed:
    envelope: Json
    bundle_sha256: str
    bundle_dir: Path
    archive: Path


def bundle_files(run_dir: Path) -> list[str]:
    """The run dir's files that go into a bundle, sorted."""
    names = [n for n in (*REQUIRED, *OPTIONAL) if (run_dir / n).is_file()]
    names += [p.name for p in run_dir.iterdir() if p.is_file() and EVENTS.fullmatch(p.name)]
    return sorted(names)


def load_scenarios(root: Path = SCENARIO_DIR) -> dict[str, Json]:
    """The suite's scenario v2 files by name."""
    return {p.stem: scenario.load(p) for p in sorted(root.glob("*.json"))}


def cost_kind(manifest: Json, events: Iterable[Json]) -> str:
    """notional for a subscription client (Claude Code reports its own figure), billed when the endpoint reported the
    cost of every priced call, else estimated."""
    if (manifest.get("client") or {}).get("name") == "claude":
        return "notional"
    usage = [e for e in events if e.get("kind") == "usage"]
    return "billed" if usage and all(e.get("cost_usd") is not None for e in usage) else "estimated"


def refusal(man: Json, res: Json, scenarios: Mapping[str, Json], released: Collection[str]) -> str | None:
    """Why this run can't be submitted, or None."""
    if res.get("pilot"):
        return f"a pilot run: {'; '.join(map(str, res['pilot']))}"
    if res.get("valid") is not True or res.get("invalid_reason"):
        return f"an invalid run ({res.get('invalid_reason')})"
    name = (man.get("scenario") or {}).get("name")
    if not isinstance(name, str) or name not in scenarios:
        return f"scenario {name!r} is not in the suite"
    if scenarios[name].get("holdout"):
        return f"scenario {name!r} is a holdout"
    tag = (man.get("versions") or {}).get("suite")
    if tag not in released:
        return f"suite {tag!r} is not released"
    if not (man.get("scenario") or {}).get("digest"):
        return "the manifest has no scenario digest"
    return None


def _jsonl(data: bytes) -> list[Json]:
    return [json.loads(ln) for ln in data.decode().splitlines() if ln.strip()]


def envelope(
    man: Json,
    res: Json,
    files: Mapping[str, bytes],
    *,
    attempt: Attempt,
    replacements: int,
    bundle_id: str,
    packed: float,
    panel_version: str | None = None,
    video: Json | None = None,
    recording: Json | None = None,
    kind: str | None = None,
) -> Json:
    model, client, sc = man["model"], man["client"], man["scenario"]
    events = [e for n, d in files.items() if EVENTS.fullmatch(n) for e in _jsonl(d)]
    return {
        "schema": bundle.SCHEMA,
        "bundle_id": bundle_id,
        "run_id": man["run_id"],
        "suite": man["versions"]["suite"],
        "scenario": sc["name"],
        "scenario_digest": sc["digest"],
        "condition": man["condition"],
        "model": {k: model.get(k) for k in ("slug", "provider_requested", "reasoning_effort")},
        "client": {"name": client["name"], "version": client.get("version")},
        "versions": {
            "harness": man["versions"].get("harness"),
            "bench": man["versions"]["bench"],
            "panel": panel_version,
        },
        "attempt": {
            "n": attempt.n,
            "unsubmitted_valid": attempt.unsubmitted_valid,
            "cell": f"{sc['name']}/{model['slug']}/{man['condition']}",
        },
        "cost": {"usd": res.get("cost_usd"), "kind": kind or cost_kind(man, events)},
        "run_hash": bundle.run_hash(files),
        "files": [{"name": n, "sha256": bundle.sha256_bytes(d), "bytes": len(d)} for n, d in sorted(files.items())],
        "video": video,
        "recording": recording,
        "scrub": {"version": scrub.VERSION, "replacements": replacements},
        "packed": packed,
    }


def size_problems(files: Mapping[str, bytes]) -> list[str]:
    """The size caps a bundle's files (bundle.json included) break."""
    out = [
        f"{n}: {len(d)} bytes over {bundle.MAX_FILE_BYTES}" for n, d in files.items() if len(d) > bundle.MAX_FILE_BYTES
    ]
    if len(files.get(bundle.FRAMES_TAR, b"")) > bundle.MAX_FRAMES_BYTES:
        out.append(f"{bundle.FRAMES_TAR} over {bundle.MAX_FRAMES_BYTES} bytes")
    if (total := sum(map(len, files.values()))) > bundle.MAX_TOTAL_BYTES:
        out.append(f"{total} bytes in all, over {bundle.MAX_TOTAL_BYTES}")
    if len(files) - (bundle.ENVELOPE in files) > bundle.MAX_FILES:
        out.append(f"{len(files)} files, over {bundle.MAX_FILES}")
    return out


def frames_tar(frames_dir: Path) -> bytes:
    """The renderer's frames/<n>.jpg as one uncompressed tar (members frames/<n>.jpg in frame order, mtime 0, owner 0,
    mode 644); refuses any other entry in the folder."""
    entries = sorted(frames_dir.iterdir(), key=lambda p: (len(p.name), p.name))
    bad = [
        p.name
        for p in entries
        if not p.is_file() or p.is_symlink() or not bundle.FRAMES_MEMBER.fullmatch(f"frames/{p.name}")
    ]
    if bad:
        raise PackError(f"{frames_dir}: not frames/<n>.jpg: {bad[:3]}")
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.USTAR_FORMAT) as tar:
        for p in entries:
            info = tarfile.TarInfo(f"frames/{p.name}")
            info.size, info.mtime, info.mode = p.stat().st_size, 0, 0o644
            tar.addfile(info, io.BytesIO(p.read_bytes()))
    return raw.getvalue()


def archive_bytes(files: Mapping[str, bytes]) -> bytes:
    """A deterministic .tar.gz of the files: sorted, mtime 0, owner 0, mode 644."""
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for name in sorted(files):
            info = tarfile.TarInfo(name)
            info.size, info.mtime, info.mode = len(files[name]), 0, 0o644
            tar.addfile(info, io.BytesIO(files[name]))
    out = io.BytesIO()
    with gzip.GzipFile(fileobj=out, mode="wb", mtime=0) as gz:
        gz.write(raw.getvalue())
    return out.getvalue()


def pack(
    run_dir: str | Path,
    out_dir: str | Path,
    *,
    attempt: Attempt,
    scenarios: Mapping[str, Json] | None = None,
    released: Collection[str] | None = None,
    literals: Iterable[str] = (),
    panel_version: str | None = None,
    video: Json | None = None,
    recording: Json | None = None,
    kind: str | None = None,
    new_id: Callable[[], str] = lambda: str(uuid.uuid4()),
    now: Callable[[], float] = time.time,
) -> Packed:
    """Pack run_dir into out_dir (bundle/ and bundle.tar.gz; out_dir must not hold a bundle yet). `scenarios` and
    `released` default to the installed suite's scenario files and pinned suite versions; `literals` are values
    to scrub verbatim (the submitter's .env values)."""
    run_dir, out_dir = Path(run_dir), Path(out_dir)
    if not run_dir.is_dir():
        raise PackError(f"{run_dir}: not a run dir")
    names = bundle_files(run_dir)
    missing = [n for n in REQUIRED if n not in names]
    if missing or not any(EVENTS.fullmatch(n) for n in names):
        raise PackError(f"{run_dir}: missing {', '.join(missing) or 'the events log'}")
    man = json.loads((run_dir / "manifest.json").read_bytes())
    res = json.loads((run_dir / "result.json").read_bytes())
    scenarios = load_scenarios() if scenarios is None else scenarios
    released = suite.load_pins().keys() if released is None else released
    if why := refusal(man, res, scenarios, released):
        raise PackError(f"{run_dir.name}: refused, {why}")
    scrubber = scrub.Scrubber(literals)
    files = {n: scrubber.file(n, (run_dir / n).read_bytes()) for n in names}
    man = json.loads(files["manifest.json"])  # scrubbed
    env = envelope(
        man,
        res,
        files,
        attempt=attempt,
        replacements=scrubber.replacements,
        bundle_id=new_id(),
        packed=round(now(), 3),
        panel_version=panel_version,
        video=video,
        recording=recording,
        kind=kind,
    )
    files[bundle.ENVELOPE] = bundle.canonical_bytes(env)
    if problems := size_problems(files):
        raise PackError(f"{run_dir.name}: refused, {'; '.join(problems)}")
    bdir, arch = out_dir / BUNDLE_DIR, out_dir / ARCHIVE
    if bdir.exists() or arch.exists():
        raise PackError(f"{out_dir} already holds a bundle")
    bdir.mkdir(parents=True)
    for n, d in files.items():
        (bdir / n).write_bytes(d)
    arch.write_bytes(archive_bytes(files))
    return Packed(env, bundle.sha256_bytes(files[bundle.ENVELOPE]), bdir, arch)


def add_render(render_dir: str | Path, out_dir: str | Path, literals: Iterable[str] = ()) -> Packed:
    """Put the renderer's render.json and frames/ (as frames.tar) from render_dir into the bundle packed in out_dir.
    Refuses a bundle that already has them, a render.json made for another run_hash, and the size caps."""
    render_dir, bdir = Path(render_dir), Path(out_dir) / BUNDLE_DIR
    files = {p.name: p.read_bytes() for p in sorted(bdir.iterdir()) if p.is_file()}
    env = json.loads(files.pop(bundle.ENVELOPE))
    if "render.json" in files or bundle.FRAMES_TAR in files:
        raise PackError(f"{bdir} already holds a render")
    scrubber = scrub.Scrubber(literals)
    render = scrubber.file("render.json", (render_dir / "render.json").read_bytes())
    if json.loads(render).get("run_hash") != env["run_hash"]:
        raise PackError(f"{render_dir}/render.json was rendered for another run_hash")
    new = {"render.json": render}
    if (render_dir / "frames").is_dir():
        new[bundle.FRAMES_TAR] = frames_tar(render_dir / "frames")
    files |= new
    env["files"] = [{"name": n, "sha256": bundle.sha256_bytes(d), "bytes": len(d)} for n, d in sorted(files.items())]
    env["scrub"]["replacements"] += scrubber.replacements
    files[bundle.ENVELOPE] = bundle.canonical_bytes(env)
    if problems := size_problems(files):
        raise PackError(f"{bdir}: refused, {'; '.join(problems)}")
    for n in (*new, bundle.ENVELOPE):
        (bdir / n).write_bytes(files[n])
    arch = Path(out_dir) / ARCHIVE
    arch.write_bytes(archive_bytes(files))
    return Packed(env, bundle.sha256_bytes(files[bundle.ENVELOPE]), bdir, arch)


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m bg3bench.submission.pack", description=(__doc__ or "").splitlines()[0])
    ap.add_argument("run_dir", type=Path, help="the run dir (--add-render: the renderer's output dir)")
    ap.add_argument("out_dir", type=Path)
    ap.add_argument("--attempt", type=int, help="this run's number in its cell on this install")
    ap.add_argument("--unsubmitted-valid", type=int, default=0, help="earlier valid runs of the cell not submitted")
    ap.add_argument("--add-render", action="store_true", help="add render.json and frames/ to the bundle in OUT_DIR")
    a = ap.parse_args(argv)
    if not a.add_render and a.attempt is None:
        ap.error("--attempt is required")
    try:
        if a.add_render:
            p = add_render(a.run_dir, a.out_dir)
        else:
            p = pack(a.run_dir, a.out_dir, attempt=Attempt(a.attempt, a.unsubmitted_valid))
    except PackError as e:
        print(e, file=sys.stderr)
        return 1
    print(f"bundle {p.bundle_sha256}  {p.archive}  ({p.envelope['scrub']['replacements']} scrubbed)")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

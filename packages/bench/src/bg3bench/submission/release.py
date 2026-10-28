# pyright: basic
"""The suite's release file (schema/release.v1.schema.json): what submissions of a released suite are checked
against and what the website serves as `<suite>/suite.json` next to the start save zips.

It holds the harness and bg3bench versions and the game pins a run must cite, every non-holdout scenario's digest,
start save, .lsv sha1 and start-state fingerprint (saves.json), the published save zips, and per condition the bg3
tools a play process is served and the sha256 of the prompt each client is served. The tools and prompts are worked
out the way the runner works them out for a run (bg3harness.client_setup.run_files: the bg3 server dumped offline
in the condition's env, its tool list minus the condition's tools off, the manifest's prompt fields), so a run of
the suite through an unmodified harness hashes to them.

    python -m bg3bench.submission.release --suite bench-v0.8 --pins-from MANIFEST... [--out FILE]
        [--condition rules ...] [--client pi --client claude] [--game-build B --bg3le-sha256 H --mod-sha1 H]

--out defaults to packages/bench/releases/<suite>.json (default_path), where the validator looks for it.
--pins-from reads the game pins from the reference runs' manifest.json files, which must all cite the same pins;
the flags override them. The build refuses a suite that isn't the installed one, suite files not pinned for it
(bg3bench.suite.check), saves.json disagreeing with the scenario files, reference runs with different pins, and
missing pins.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import jsonschema
from bg3harness import client_setup
from bg3harness.run_manifest import tools_digest

from bg3bench import conditions, pins, results, saves, scenario, suite
from bg3bench.submission import bundle, pack

Json = dict[str, Any]

SCHEMA = 1
KIND = "bg3bench.release"
SCHEMA_PATH = bundle.SCHEMA_PATH.parent / "release.v1.schema.json"
RELEASES = suite.PACKAGE_ROOT / "releases"  # <suite>.json: committed when the suite is released
CLIENTS = ("pi", "claude")
NO_MEMORY = "memory"  # the bridge's memory tool group: off in every bench run (bg3harness.runner.run_host)
PIN_KEYS = ("game_build", "bg3le_sha256", "mod_sha1")
# The tool groups a client profile can turn off on top of the condition's (bg3harness.profiles.tools_off: screenshots
# for a pi model without image input), and the clients whose profiles do. Each is a variant of every condition.
VARIANTS = (("screenshots",),)
VARIANT_CLIENTS = ("pi",)
ABOUT = (
    "A released bg3bench suite (schema release.v1, written by python -m bg3bench.submission.release). versions and "
    "pins: what a run's manifest must cite. scenarios: every non-holdout scenario's digest, start save folder, .lsv "
    "sha1 and start-state fingerprint (null: none recorded yet). saves: the start save zips, their storage key, size "
    "and sha256 (null until published). conditions: per condition id the bg3 tools a play process is served (names, "
    "count, allowlist_sha256) and per client the sha256 of the prompt it is served; its variants: the same under a client "
    "profile's tool groups off (keyed by those groups), which a run naming those tools is checked against."
)


class ReleaseError(ValueError):
    pass


@dataclass(frozen=True)
class Served:
    """What a condition's play processes are served: the bg3 tool names and, per client, the manifest's prompt."""

    names: list[str]
    prompts: dict[str, Json]  # client -> {"sha256", "condition"}


def default_path(tag: str | None = None) -> Path:
    """Where a suite's release file lives (default: the installed suite's): packages/bench/releases/<suite>.json."""
    return RELEASES / f"{tag or suite.suite_version()}.json"


def errors(doc: Any) -> list[str]:
    """Where `doc` breaks release.v1 ([] = a release file)."""
    v = jsonschema.Draft202012Validator(json.loads(SCHEMA_PATH.read_text()))
    return [f"{'/'.join(map(str, e.absolute_path))}: {e.message}"[:200] for e in v.iter_errors(doc)]


def session_env(cid: str, environ: Mapping[str, str], groups_off: Sequence[str] = ()) -> dict[str, str]:
    """The env a bench job of condition `cid` gives its play processes' server: the prompt condition and the tools off
    (memory, then the condition's removed tools, then the profile's groups_off), as bg3harness.runner.run_host and
    bg3harness.profiles.tool_env set them."""
    cond = conditions.parse(cid)
    env = {k: v for k, v in environ.items() if k not in ("BG3AI_TOOLS", "BG3AI_TOOLS_OFF", "BG3AI_PROMPT_CONDITION")}
    env["BG3AI_PROMPT_CONDITION"] = cond.prompt_condition()
    env["BG3AI_TOOLS_OFF"] = ",".join([NO_MEMORY, *cond.removed_tools(), *groups_off])
    return env


def serve(
    cid: str,
    clients: Iterable[str],
    repo: Path,
    environ: Mapping[str, str] = os.environ,
    dump: Callable[[Path, Path, Path, Path], None] | None = None,
    groups_off: Sequence[str] = (),
) -> Served:
    """The tools and prompts condition `cid` serves each client, from the bg3 server dumped offline in its env
    (dump: client_setup.run_files' server dump, default the bridge's tools/pi/dump_server.py); groups_off: a profile
    variant's tool groups off."""
    env = session_env(cid, environ, groups_off)
    prompts: dict[str, Json] = {}
    names: list[str] | None = None
    with tempfile.TemporaryDirectory(prefix="bg3bench-release-") as tmp:
        for client in clients:
            d = Path(tmp) / client
            files = client_setup.run_files(
                d, client, None, None, repo, lambda _msg: None, dump=dump, version=lambda _c: None, environ=env
            )
            man = files["manifest"]
            if "error" in man:
                raise ReleaseError(f"{cid} / {client}: {man['error']}")
            got = client_setup.served_tools(client_setup.tool_names(d / "bg3-tools.json"), env)
            if names is not None and got != names:
                raise ReleaseError(f"{cid}: clients are served different tools")
            names = got
            prompts[client] = {"sha256": man["prompt"]["sha256"], "condition": man["condition"]}
    if names is None:
        raise ReleaseError("no client")
    return Served(names, prompts)


def build(
    tag: str,
    versions: Json,
    game_pins: Json,
    scenarios: Mapping[str, Json],
    suite_saves: Mapping[str, saves.Save],
    served: Mapping[str, Served],
    variants: Mapping[str, Mapping[str, Served]] | None = None,
) -> Json:
    """The release document. scenarios: the suite's scenario files by name (holdouts are left out); suite_saves: its
    saves.json entries; served: per condition id what its play processes are served; variants: per condition id,
    what they are served under a profile's tools off (variant_key: the groups off)."""
    if missing := [k for k in PIN_KEYS if not game_pins.get(k)]:
        raise ReleaseError(f"missing pins: {', '.join(missing)}")
    if bad := saves.disagreements(suite_saves, scenarios):
        raise ReleaseError("saves.json and the scenarios disagree: " + "; ".join(bad))
    by_save = {s.name: s for s in suite_saves.values()}
    out_sc: Json = {}
    for name, sc in sorted(scenarios.items()):
        if sc.get("holdout"):
            continue
        start = sc.get("start") or {}
        s = by_save.get(start.get("save") or "")
        out_sc[name] = {
            "digest": scenario.digest(sc),
            "save": start.get("save"),
            "save_sha1": start.get("save_sha1"),
            "fingerprint": s.fingerprint if s else None,
        }
    doc = {
        "schema": SCHEMA,
        "kind": KIND,
        "about": ABOUT,
        "suite": tag,
        "versions": {"harness": versions["harness"], "bench": versions["bench"]},
        "pins": {k: game_pins[k] for k in PIN_KEYS},
        "scenarios": out_sc,
        "saves": [
            {
                "name": s.name,
                "scenarios": list(s.scenarios),
                "save_sha1": s.save_sha1,
                "zip": s.zip,
                "key": s.key,
                "bytes": s.bytes,
                "sha256": s.sha256,
            }
            for s in sorted(suite_saves.values(), key=lambda s: s.name)
        ],
        "conditions": {
            cid: _entry(sv)
            | (
                {"variants": {k: _entry(v) for k, v in sorted(variants[cid].items())}}
                if variants and variants.get(cid)
                else {}
            )
            for cid, sv in sorted(served.items())
        },
    }
    if errs := errors(doc):
        raise ReleaseError("not a release.v1 document: " + "; ".join(errs[:5]))
    return doc


def _entry(sv: Served) -> Json:
    return {"tools": {**tools_digest(sv.names), "names": sorted(sv.names)}, "prompts": dict(sorted(sv.prompts.items()))}


def variant_key(groups_off: Sequence[str]) -> str:
    return "-".join(sorted(groups_off))


def condition_entry(condition: Json, allowlist_sha256: str | None) -> Json:
    """A release condition's entry a run is checked against: the variant whose tools the run's manifest names (a
    profile turned those groups off), else the condition's own."""
    for v in (condition.get("variants") or {}).values():
        if (v.get("tools") or {}).get("allowlist_sha256") == allowlist_sha256:
            return v
    return {k: v for k, v in condition.items() if k != "variants"}


def pins_of(manifest: Json) -> Json:
    """The game pins a run's manifest cites, in the release's keys."""
    return results.game_pins(manifest)


def common_pins(manifests: Sequence[Json]) -> Json:
    """The game pins every reference run cites; refuses runs whose pins differ (one release, one game setup)."""
    found = {json.dumps(pins_of(m), sort_keys=True) for m in manifests}
    if len(found) > 1:
        runs = ", ".join(sorted(str(m.get("run_id")) for m in manifests))
        raise ReleaseError(f"the reference runs ({runs}) cite different pins: {' / '.join(sorted(found))}")
    return pins_of(manifests[0]) if manifests else {}


def unfingerprinted(doc: Json) -> list[str]:
    """The release's scenarios without a start-state fingerprint (X01 rejects their runs until one is recorded)."""
    return sorted(n for n, s in doc["scenarios"].items() if s["fingerprint"] is None)


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m bg3bench.submission.release", description=(__doc__ or "").split("\n\n")[0]
    )
    ap.add_argument("--suite", required=True, help="the suite tag, e.g. bench-v0.8: must be the installed suite")
    ap.add_argument("--out", type=Path, help="default: packages/bench/releases/<suite>.json")
    ap.add_argument("--pins-from", type=Path, nargs="+", default=[], help="reference runs' manifest.json files")
    for k in PIN_KEYS:
        ap.add_argument(f"--{k.replace('_', '-')}")
    ap.add_argument("--condition", action="append", help="a condition id (repeat); default: rules")
    ap.add_argument("--client", action="append", choices=CLIENTS, help="a client (repeat); default: pi and claude")
    ap.add_argument("--repo", type=Path, default=suite.PACKAGE_ROOT.parents[1], help="the harness repo (bridge/)")
    a = ap.parse_args(argv)
    try:
        if a.suite != suite.suite_version():
            raise ReleaseError(f"{a.suite} is not the installed suite {suite.suite_version()}")
        if bad := suite.check():
            raise ReleaseError("; ".join(bad))
        game_pins = common_pins([json.loads(p.read_text()) for p in a.pins_from])
        game_pins |= {k: getattr(a, k) for k in PIN_KEYS if getattr(a, k)}
        cids = a.condition or ["rules"]
        clients = a.client or list(CLIENTS)
        served = {cid: serve(cid, clients, a.repo) for cid in cids}
        vclients = [c for c in clients if c in VARIANT_CLIENTS]
        variants = (
            {cid: {variant_key(g): serve(cid, vclients, a.repo, groups_off=g) for g in VARIANTS} for cid in cids}
            if vclients
            else {}
        )
        doc = build(
            a.suite, pins.versions(), game_pins, pack.load_scenarios(), saves.load().get(a.suite, {}), served, variants
        )
    except (ReleaseError, saves.SavesError, ValueError, OSError) as e:
        print(f"refused: {e}", file=sys.stderr)
        return 1
    a.out = a.out or default_path(a.suite)
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps(doc, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    if missing := unfingerprinted(doc):
        print(f"no start-state fingerprint yet (X01 rejects their runs): {', '.join(missing)}", file=sys.stderr)
    print(f"wrote {a.out}: {len(doc['scenarios'])} scenarios, {len(doc['conditions'])} conditions", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""The server's GET payloads checked against the web client's types (web/src/lib/api.ts, types.ts) by tsc.

Each payload is assigned to the return type of the api.ts function that fetches it, so a field the client declares
that the server renamed, dropped or sends with another type (a null where it expects a number, say) fails the
compile. Fields the server sends that the client doesn't declare are fine (no excess-property check). Needs
web/node_modules (npm ci), as the built web app does.
"""

from __future__ import annotations

import json
import re
import subprocess
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import quote

WEB = Path(__file__).resolve().parents[2] / "web"
TSC = WEB / "node_modules" / ".bin" / "tsc"


def get(url: str) -> Any:
    with urllib.request.urlopen(url, timeout=30) as r:
        return json.loads(r.read())


def payloads(url: str, pt: str) -> dict[str, tuple[str, Any]]:
    """Every GET payload the screens read for playthrough `pt` -> {name: (api.ts function, payload)}."""
    api, p = f"{url}/api/v1", f"{url}/api/v1/playthroughs/{quote(pt)}"
    out: dict[str, tuple[str, Any]] = {
        name: (fn, get(f"{api}/{path}"))
        for name, fn, path in [
            ("status", "status", "status"),
            ("playthroughs", "playthroughs", "playthroughs"),
            ("live", "live", "live"),
            ("latency", "latency", "latency"),
            ("progress", "progress", "progress"),
            ("goals", "goals", "goals"),
            ("recorder", "recorder", "recorder"),
            ("video_index", "videoIndex", "video/index"),
            ("supervisor", "supervisor", "supervisor"),
        ]
    }
    for name, fn in [
        ("watch", "watch"),
        ("sessions", "sessions"),
        ("memory/tree", "memoryTree"),
        ("memory/log", "memoryLog"),
        ("timeline", "timeline"),
        ("positions", "positions"),
        ("story", "story"),
        ("bench", "bench"),
        ("config", "config"),
    ]:
        out[name] = (fn, get(f"{p}/{name}"))
    for s in out["sessions"][1]["sessions"]:
        out[f"replay/{s['session']}"] = ("replay", get(f"{p}/replay/{s['session']}"))
    for f in out["memory/tree"][1]["files"]:
        out[f"memory/file {f['path']}"] = ("memoryFile", get(f"{p}/memory/file?path={quote(f['path'])}"))
    for f in out["memory/tree"][1]["files"]:
        for c in out[f"memory/file {f['path']}"][1]["history"][:1]:  # the file's last commit's change to it
            q = f"path={quote(f['path'])}&sha={quote(c['sha'])}"
            out[f"memory/diff {f['path']}"] = ("memoryDiff", get(f"{p}/memory/diff?{q}"))
    return out


def type_errors(found: dict[str, tuple[str, Any]], work: Path) -> str:
    """tsc's errors for the payloads (empty: they all match the client's types), each named by its payload."""
    lines = [
        f'import type {{ api }} from "{WEB / "src" / "lib" / "api"}";',
        "type R<K extends keyof typeof api> = Awaited<ReturnType<(typeof api)[K]>>;",
        "type W<T> = { -readonly [K in keyof T]: W<T[K]> };  // `as const` keeps the strings literal, W unfreezes",
    ]
    for i, (name, (fn, payload)) in enumerate(found.items()):
        lines.append(f"// {name}")
        lines.append(
            f"const raw{i} = {json.dumps(payload, ensure_ascii=False)} as const;"
            f" export const p{i}: R<{json.dumps(fn)}> = raw{i} as W<typeof raw{i}>;"
        )
    src = work / "contract.ts"
    src.write_text("\n".join(lines) + "\n")
    opts: dict[str, Any] = {
        "strict": True,
        "noUncheckedIndexedAccess": True,
        "skipLibCheck": True,
        "noEmit": True,
        "types": [],
    }
    opts |= {"target": "ES2022", "module": "ESNext", "moduleResolution": "bundler", "lib": ["ES2022", "DOM"]}
    (work / "tsconfig.json").write_text(json.dumps({"compilerOptions": opts, "files": [src.name]}))
    r = subprocess.run([str(TSC), "-p", str(work), "--pretty", "false"], capture_output=True, text=True, timeout=120)
    if r.returncode == 0:
        return ""
    names = list(found)
    out: list[str] = []
    for ln in r.stdout.splitlines():  # ...contract.ts(<line>,<col>): error ...; line 5 is payload 0, 7 payload 1, ...
        m = re.search(r"contract\.ts\((\d+),\d+\): (.*)", ln)
        if m:
            n = int(m.group(1))
            ln = f"[{names[(n - 5) // 2] if n >= 5 else '?'}] {m.group(2)[:3000]}"
        out.append(ln)
    return "\n".join(out) or r.stderr

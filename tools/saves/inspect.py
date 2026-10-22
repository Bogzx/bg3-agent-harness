"""What a BG3 save (.lsv) embeds: members, mod list, game version, character and profile names, and anything that
looks like a Steam ID, an e-mail address or a user path (research/arch/game.md T6; before any save is published).
Game-install paths (steamapps, a Proton compatdata prefix) carry no user and are listed apart, not as findings.
An LSF is scanned field by field (its names, each text value, each opaque buffer), never across a field's end: the
values section is binary, and float or UUID bytes run into the next string look like `hB@j.rD` (live6 rows 19-22).

Read-only and on COPIES only: it refuses a path inside the game's Larian Studios folder (copy the .lsv out first).
zstd members (BG3 4.x writes them) need Python 3.14+ (`compression.zstd`).

    cp ".../Savegames/Story/<folder>/<label>.lsv" /tmp/x.lsv
    uv run --python 3.14 python -I tools/saves/inspect.py /tmp/x.lsv            # values masked
    uv run --python 3.14 python -I tools/saves/inspect.py /tmp/x.lsv --show     # full values

Run with -I (or from another directory): this file is named inspect.py, like the stdlib module.
"""

import os
import sys

# Run as a script, its own folder first on sys.path would shadow the stdlib `inspect` for everything imported below.
if __name__ == "__main__":
    _HERE = os.path.dirname(os.path.abspath(__file__))
    sys.path[:] = [p for p in sys.path if os.path.abspath(p or ".") != _HERE]

import argparse
import hashlib
import json
import re
from pathlib import Path

from bg3ai import lsf, lspk
from bg3ai.difficulty import option_name

PATTERNS = {
    "steam_id": re.compile(rb"7656119\d{10}"),
    "email": re.compile(rb"[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9-]{1,63}(?:\.[A-Za-z0-9-]{1,63})*\.[A-Za-z]{2,12}"),
    "user_path": re.compile(
        rb"(?:/home/[A-Za-z0-9._-]+|/Users/[A-Za-z0-9._ -]+|[A-Za-z]:[\\/]+Users[\\/]+[^\\/\x00\"<>|]+"
        rb"|/var/home/[A-Za-z0-9._-]+|/(?:run/)?media/[A-Za-z0-9._-]+)"
    ),
    "install_path": re.compile(rb"steamapps|compatdata/\d+"),  # reported apart: no user in it
}
# In an opaque LSF buffer (ScratchBuffer) an e-mail needs a lower-case domain ending the text: binary bytes rarely do.
BUFFER_PATTERNS = dict(
    PATTERNS,
    email=re.compile(rb"[A-Za-z0-9._%+-]{1,64}@[a-z0-9-]{1,63}(?:\.[a-z0-9-]{1,63})*\.[a-z]{2,12}(?![A-Za-z0-9-])"),
)
TEXT_TYPES = lsf.STRING_TYPES | {28, 33}  # 28/33: TranslatedString (a handle)
BUFFER_TYPE = 25
# Osiris masks the story's strings by XOR 0xAD (LSLib OsiReader).
_UNMASK = bytes(b ^ 0xAD for b in range(256))
SKIP_SCAN = (".webp", ".png", ".jpg", ".dds")


def mask(kind: str, value: str) -> str:
    if kind == "steam_id":
        return value[:4] + "*" * (len(value) - 6) + value[-2:]
    if kind == "email":
        user, _, host = value.partition("@")
        return user[:1] + "***@" + host
    return re.sub(r"(/home/|/Users/|Users[\\/]+)([^\\/]+)", lambda m: m.group(1) + "<user>", value)


def scan(blobs: list[tuple[dict, bytes]], member: str, found: dict, limit: int = 20) -> None:
    for patterns, blob in blobs:
        for kind, rx in patterns.items():
            for m in rx.finditer(blob):
                value = m.group().decode("utf-8", "replace")
                hit = found.setdefault((kind, value), {"kind": kind, "value": value, "count": 0, "members": []})
                hit["count"] += 1
                if member not in hit["members"] and len(hit["members"]) < limit:
                    hit["members"].append(member)


def _scan_blobs(name: str, data: bytes) -> list[tuple[dict, bytes]]:
    """What to scan with which patterns. Fields are joined by NUL, which no pattern crosses."""
    if data[:4] == lsf.MAGIC:
        s = lsf.sections(data)
        text, buffers = [], []
        for t, raw in lsf.raw_values(s):
            if t in TEXT_TYPES:
                text.append(raw)
            elif t == BUFFER_TYPE:
                buffers.append(raw)
        names = [n.encode() for bucket in lsf.names(s.strings) for n in bucket]
        return [(PATTERNS, b"\0".join(names + text)), (BUFFER_PATTERNS, b"\0".join(buffers))]
    if name.endswith(".bin"):
        return [(PATTERNS, data), (PATTERNS, data.translate(_UNMASK))]
    return [(PATTERNS, data)]


def meta_summary(attrs: list[lsf.Attr]) -> dict:
    out: dict = {"mods": [], "characters": [], "fields": {}}
    mod: dict = {}
    for a in attrs:
        if a.path.endswith("ModuleShortDesc"):
            if a.name in mod:
                out["mods"].append(mod)
                mod = {}
            mod[a.name] = a.value
        elif a.path.endswith("GameVersions/GameVersion"):
            out.setdefault("game_versions", []).append(a.value)
        elif a.path.endswith("CharacterMetaData"):
            if a.name == "CharacterIconID":
                out["characters"].append({"icon": a.value})
            elif out["characters"]:
                out["characters"][-1][a.name] = a.value
        elif a.path.endswith("MetaData/MetaData"):
            out["fields"][a.name] = a.value
    if mod:
        out["mods"].append(mod)
    return out


def save_info(data: bytes) -> dict:
    info = json.loads(data)
    party = [
        {k: c.get(k) for k in ("Origin", "Race", "Level", "Classes")}
        for c in (info.get("Active Party") or {}).get("Characters") or []
    ]
    keep = ("Save Name", "Game Version", "Platform", "Current Level", "Difficulty")
    names = info.get("Difficulty") if isinstance(info.get("Difficulty"), list) else [info.get("Difficulty")]
    picked = next((d for d in map(option_name, names) if d), None)  # ["DifficultyMedium", "RulesetLarian"]
    return dict({k: info.get(k) for k in keep}, difficulty=picked, party=party, keys=sorted(info))


def inspect_save(path: Path, show: bool = False, deep: bool = True) -> dict:
    data = path.read_bytes()
    pkg = lspk.parse(data)
    report: dict = {
        "file": str(path),
        "sha1": hashlib.sha1(data).hexdigest(),
        "size": len(data),
        "lspk": {"version": pkg.version, "parts": pkg.parts, "flags": pkg.flags},
        "members": [
            {"name": e.name, "method": e.method, "size_on_disk": e.size_on_disk, "size": e.size} for e in pkg.entries
        ],
        "unread": [],
    }
    found: dict = {}
    for e in pkg.entries:
        body = pkg.read(e)
        if body is None:
            report["unread"].append(f"{e.name}: {e.method} needs Python 3.14+")
            continue
        low = e.name.lower()
        if low == "saveinfo.json":
            report["save_info"] = save_info(body)
        elif low == "meta.lsf":
            report["meta"] = meta_summary(lsf.attributes(body))
        if (deep or low in ("saveinfo.json", "meta.lsf")) and not low.endswith(SKIP_SCAN):
            try:
                scan(_scan_blobs(low, body), e.name, found)
            except (lsf.LsfError, lspk.LspkError, IndexError, ValueError) as err:
                report["unread"].append(f"{e.name}: {err}")
    hits = sorted(found.values(), key=lambda h: (h["kind"], -h["count"], h["value"]))
    if not show:
        for h in hits:
            h["value"] = mask(h["kind"], h["value"])
    report["findings"] = [h for h in hits if h["kind"] != "install_path"]
    report["install_paths"] = [h for h in hits if h["kind"] == "install_path"]
    return report


def in_game_folder(path: Path) -> bool:
    return "Larian Studios" in path.resolve().parts


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    ap.add_argument("save", type=Path, help="a COPY of a .lsv")
    ap.add_argument("--show", action="store_true", help="print found IDs/paths unmasked")
    ap.add_argument("--shallow", action="store_true", help="scan only SaveInfo.json and meta.lsf (fast)")
    args = ap.parse_args(argv)
    if in_game_folder(args.save):
        print(f"refused: {args.save} is inside the game's folder; copy it out first (cp it to /tmp)", file=sys.stderr)
        return 2
    report = inspect_save(args.save, show=args.show, deep=not args.shallow)
    print(json.dumps(report, indent=1, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())

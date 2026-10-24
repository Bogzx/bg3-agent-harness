"""Render basics every renderer shares: `Handles` (GUID -> short id), number, position, class and resource
text, and compact(). render.py re-exports these names."""

from __future__ import annotations

import re
from typing import Any

UUID_RE = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I)
# Template-prefixed GUIDs ("S_Player_Laezel_58a69333-...") also occur in Osiris land.
PREFIXED_UUID_RE = re.compile(r"\b\w+_([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\b", re.I)


class Handles:
    def __init__(self) -> None:
        self.by_uuid: dict[str, str] = {}
        self.by_handle: dict[str, str] = {}
        self.names: dict[str, str] = {}  # handle -> last known display name
        self._n = 0

    def get(self, uuid: str, name: str | None = None) -> str:
        uuid = self._bare(uuid)
        h = self.by_uuid.get(uuid)
        if h is None:
            self._n += 1
            h = f"e{self._n}"
            self.by_uuid[uuid] = h
            self.by_handle[h] = uuid
        if name:
            self.names[h] = name
        return h

    @staticmethod
    def _bare(uuid: str) -> str:
        m = PREFIXED_UUID_RE.fullmatch(uuid)
        return (m.group(1) if m else uuid).lower()

    def resolve(self, ref: str) -> str:
        """Agent reference (handle, or a unique visible/party name) -> uuid."""
        if ref is None:
            raise ValueError("missing target")
        r = str(ref).strip()
        if r.lower() in self.by_handle:
            return self.by_handle[r.lower()]
        # live4: trade(npc=<uuid>) failed "unknown target 'e246'" (the error's uuid scrubbed to its own handle): a uuid
        # this map already handed a handle for is that target
        if (UUID_RE.fullmatch(r) or PREFIXED_UUID_RE.fullmatch(r)) and self._bare(r) in self.by_uuid:
            return self._bare(r)
        matches = self.matches(r)
        if len(matches) == 1:
            return self.by_handle[matches[0]]
        if len(matches) > 1:
            opts = ", ".join(f"{h} ({self.names[h]})" for h in matches[:8])
            raise ValueError(f"'{ref}' is ambiguous: {opts}")
        raise ValueError(f"unknown target '{ref}'; use an id like e12 from get_nearby/get_status, or a party member name")

    def matches(self, ref: str) -> list[str]:
        """Handles whose name equals ref (or, failing that, starts with it), case-insensitive."""
        low = str(ref).strip().lower()
        out = [h for h, n in self.names.items() if n and n.lower() == low]
        out = out or [h for h, n in self.names.items() if n and n.lower().startswith(low)]
        if out or len(low) < 3:
            return out
        # S045 00:15: look_at("Aylin") for "Dame Aylin" → unknown target: a whole word of the name matches too
        # S055 06:41 "Zanner Toobin" for "Gondian Zanner Toobin": whole words in a row match too
        words = " ".join(re.findall(r"[\w-]+", low))
        return [h for h, n in self.names.items()
                if n and words and f" {words} " in " " + " ".join(re.findall(r"[\w-]+", n.lower())) + " "]

    def scrub(self, text: str) -> str:
        """Replace any GUID that slipped into free text with its handle."""
        text = PREFIXED_UUID_RE.sub(lambda m: self.get(m.group(1)), text)
        return UUID_RE.sub(lambda m: self.get(m.group(0)), text)



def raised_ladder(l: dict) -> str:
    """A ladder pulled up out of reach (Navigation.lua N.ladderRaised; S059 08:56 Wyrm's Rock e683)."""
    gap = num(l.get("raised_m"))
    if l.get("leads") == "up":
        return f"PULLED UP: its foot hangs {gap}m above the floor, can't be climbed from below (lowered from above)"
    return f"(its foot hangs {gap}m above the floor below)"

def num(x: Any) -> str:
    if isinstance(x, float):
        return f"{x:.1f}".rstrip("0").rstrip(".")
    return str(x)


def pos(p: Any) -> str:
    if isinstance(p, (list, tuple)) and len(p) >= 3:
        return f"({num(p[0])},{num(p[1])},{num(p[2])})"
    return str(p)


def compact(v: Any, H: Handles, depth: int = 0) -> str:
    """Generic fallback renderer: JSON-ish but terse, with GUIDs swapped for handles."""
    if isinstance(v, dict):
        parts = []
        for k, x in v.items():
            if x is None or x == [] or x == {} or x is False:
                continue
            if k in ("uuid", "id", "guid") and isinstance(x, str) and UUID_RE.search(x):
                parts.append(H.get(x, v.get("name")))
                continue
            if x is True:
                parts.append(k)
            elif k == "resources" and isinstance(x, dict):
                parts.append(f"resources: {resources_text(x)}")
            elif k == "classes" and isinstance(x, list):
                parts.append(classes_text(x))
            elif k in ("pos", "position"):
                parts.append(f"@{pos(x)}")
            else:
                parts.append(f"{k}={compact(x, H, depth + 1)}")
        s = " ".join(parts)
        return s if depth == 0 else "{" + s + "}"
    if isinstance(v, list):
        if all(not isinstance(x, (dict, list)) for x in v):
            return "[" + ",".join(compact(x, H, depth + 1) for x in v) + "]"
        if depth == 0:
            return "\n".join("- " + compact(x, H, 1).strip("{}") for x in v)
        return "[" + "; ".join(compact(x, H, depth + 1).strip("{}") for x in v) + "]"
    if isinstance(v, str):
        return H.scrub(v)
    return num(v)


def classes_text(cls: list) -> str:
    out = []
    for x in cls:
        if isinstance(x, dict):
            name = x.get("class") or x.get("name") or "?"
            t = f"{name}{x.get('level', '')}"
            if x.get("subclass"):
                t += f"({x['subclass']})"
            out.append(t)
        else:
            out.append(str(x))
    return "/".join(out)


_RES_ALIASES = {"ActionPoint": "Action", "BonusActionPoint": "Bonus", "ReactionActionPoint": "Reaction",
                "Movement": "Move", "SpellSlot": "Slots"}


def resources_text(res: dict) -> str:
    """{'ActionPoint':[1,1],'SpellSlot':{'1':[4,4]}} -> 'Action 1/1 · Slots L1 4/4 ...' (fixed order first).
    MoveBlocked (State.lua moveView: the status that keeps the member from walking) -> 'Move 0/9m (Lethargic)'."""
    order = ["ActionPoint", "BonusActionPoint", "Movement", "ReactionActionPoint", "SpellSlot"]
    blocked = res.get("MoveBlocked")
    keys = [k for k in order if k in res] + sorted(k for k in res if k not in order and k != "MoveBlocked")
    parts = []
    for k in keys:
        v = res[k]
        name = _RES_ALIASES.get(k, k)
        if isinstance(v, dict):
            slots = " ".join(f"L{lvl} {num(cm[0])}/{num(cm[1])}" for lvl, cm in sorted(v.items(), key=lambda kv: str(kv[0]))
                             if isinstance(cm, (list, tuple)) and len(cm) == 2)
            parts.append(f"{name} {slots}")
        elif isinstance(v, (list, tuple)) and len(v) == 2:
            parts.append(f"{name} {num(v[0])}/{num(v[1])}" + ("m" if k == "Movement" else "")
                         + (f" ({blocked})" if k == "Movement" and blocked else ""))
        else:
            parts.append(f"{name} {num(v)}")
    return " · ".join(parts)


SAME_MIN = 3  # this many plain items with one name and kind are listed as one line

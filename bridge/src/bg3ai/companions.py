"""Companions at camp and when to swap them in: the PARTY hint (stdlib only, pure helpers).

The mod's state.status carries `camp` rows (recruited companions waiting at camp: Server/State.lua
S.campRoster, the engine's CampPresence component) and state.camp the same with skills and gear plus the
party's skills. get_status/look print one PARTY line when it changes (a new region, quest, party or camp
roster), e.g.

    PARTY: Halsin (camp, Druid 5) is tied to Shadow-Cursed Lands / quest "Lift the Shadow Curse" → swap_party(out="Gale", into="Halsin") | no Sleight of Hand above +2: Astarion (camp) +7 | camp level-ups: Wyll L3→4 → level_up(who="Wyll") swaps in, levels, swaps back

The ties come from play/knowledge/companions.md (one table row per companion: regions; quest keywords).
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any, Callable

# Roles per class (the party-and-leveling playbook's four roles).
ROLES = {
    "Cleric": {"healer", "aoe"}, "Druid": {"healer", "aoe"}, "Bard": {"healer", "face"},
    "Paladin": {"tank", "face"}, "Fighter": {"tank"}, "Barbarian": {"tank"},
    "Monk": {"tank"}, "Ranger": {"ranged"}, "Rogue": {"ranged", "locks"},
    "Warlock": {"ranged", "face"}, "Sorcerer": {"aoe", "ranged", "face"}, "Wizard": {"aoe", "ranged"},
}
ROLE_TEXT = {"healer": "healer", "tank": "frontline", "ranged": "ranged damage", "aoe": "area control"}
# Skill needs: (state skill key, label, a party best below this is a gap).
SKILL_NEEDS = [("SleightOfHand", "Sleight of Hand (locks/traps)", 4),
               ("Persuasion", "Persuasion (face)", 4),
               ("Perception", "Perception (traps/secrets)", 4)]
SKILL_GAIN = 3  # a camp member fills a skill gap when it beats the party's best by this much

KNOWLEDGE_ROW = re.compile(r"^\|\s*(?P<name>[^|]+?)\s*\|\s*(?P<cls>[^|]*?)\s*\|\s*(?P<regions>[^|]*?)\s*\|"
                           r"\s*(?P<quests>[^|]*?)\s*\|\s*(?P<why>[^|]*?)\s*\|\s*$")


def load_ties(text: str) -> dict[str, dict]:
    """companions.md table → {name lowercased: {name, regions, quests, why}} (header/separator rows skipped)."""
    out = {}
    for ln in (text or "").splitlines():
        m = KNOWLEDGE_ROW.match(ln.strip())
        if not m or m.group("name").lower() in ("companion",) or set(m.group("name")) <= set("-: "):
            continue
        split = lambda s: [x.strip() for x in s.split(";") if x.strip()]  # noqa: E731
        out[m.group("name").strip().lower()] = {"name": m.group("name").strip(), "regions": split(m.group("regions")),
                                                "quests": split(m.group("quests")), "why": m.group("why").strip()}
    return out


_ties_cache: dict[str, Any] = {"path": None, "mtime": None, "ties": {}}


def ties(path: Path) -> dict[str, dict]:
    """load_ties of the file, cached by mtime ({} when missing)."""
    try:
        mt = path.stat().st_mtime
    except OSError:
        return {}
    if _ties_cache["path"] != str(path) or _ties_cache["mtime"] != mt:
        try:
            _ties_cache.update(path=str(path), mtime=mt, ties=load_ties(path.read_text()))
        except OSError:
            return {}
    return _ties_cache["ties"]


def first_name(name: Any) -> str:
    """'Shadowheart' from 'Shadowheart', 'Halsin' from 'Halsin the Druid'; matching key for the ties table."""
    return str(name or "").split()[0].strip(",").lower() if str(name or "").strip() else ""


def tie_for(name: Any, table: dict[str, dict]) -> dict | None:
    return table.get(first_name(name)) or table.get(str(name or "").strip().lower())


def class_text(row: dict) -> str:
    cls = row.get("classes") or []
    if cls:
        return "/".join(f"{c.get('class')} {c.get('level')}" for c in cls if isinstance(c, dict))
    return f"L{row.get('level')}" if row.get("level") else "?"


def roles_of(row: dict) -> set:
    out: set = set()
    for c in row.get("classes") or []:
        if isinstance(c, dict):
            out |= ROLES.get(str(c.get("class")), set())
    return out


def matches(tie: dict, region: str, quests: list[str]) -> list[str]:
    """What ties a companion to here: ['Shadow-Cursed Lands', 'quest "Lift the Shadow Curse"'] ([] = nothing)."""
    out = []
    reg = (region or "").lower()
    for r in tie.get("regions") or []:
        if reg and (r.lower() in reg or reg in r.lower()):
            out.append(r)
            break
    for q in quests:
        ql = q.lower()
        if any(k.lower() in ql for k in tie.get("quests") or []):
            out.append(f'quest "{q}"')
            break
    return out


def swap_out(live: list[dict], swappable: list[dict], incoming: dict, keep: set = frozenset()) -> dict | None:
    """Who to send to camp for `incoming`: a swappable member (not the avatar, not tied to here) whose roles
    the rest of the party plus the newcomer still cover; ties → the lowest level. None when nobody fits."""
    best, key = None, None
    for r in swappable:
        if r.get("uuid") in keep:
            continue
        others = set().union(*(roles_of(o) for o in live if o is not r)) if len(live) > 1 else set()
        lost = roles_of(r) - others - roles_of(incoming)
        k = (len(lost), r.get("level") or 0)
        if key is None or k < key:
            best, key = r, k
    return best


def hint(party: list[dict], camp: list[dict], region: str, quests: list[str], table: dict[str, dict],
         label: Callable[[str], str] = lambda u: u, skills: dict | None = None) -> list[str]:
    """PARTY hint bits ([] = nothing to suggest).
    party/camp: state rows {uuid, name, level, classes, level_up, dead?, avatar?}; skills: {uuid: {skill: bonus}}
    from state.camp (None = skip skill gaps). Ordered: story ties first (they cost story progress), then
    composition holes, skill gaps, camp level-ups."""
    bits = []
    live = [r for r in party if not r.get("dead") and not r.get("summon")]
    swappable = [r for r in live if not r.get("avatar")]
    # (a) a camp companion tied to this region or an active quest; a party member tied too is kept.
    tied_party = {r.get("uuid") for r in live if (t := tie_for(r.get("name"), table)) and matches(t, region, quests)}
    for c in camp:
        t = tie_for(c.get("name"), table)
        why = matches(t, region, quests) if t else []
        if not why:
            continue
        out_pick = swap_out(live, swappable, c, keep=tied_party)
        call = f'swap_party(out="{out_pick.get("name")}", into="{c.get("name")}")' if out_pick else "swap_party"
        bits.append(f"{c.get('name')} (camp, {class_text(c)}) is tied to {' / '.join(why)}: {t.get('why')} → {call}")
    # (b) composition holes the camp could fill
    have = set().union(*(roles_of(r) for r in live)) if live else set()
    for role in ("healer", "tank", "ranged", "aoe"):
        if role in have:
            continue
        fit = [c for c in camp if role in roles_of(c)]
        bits.append(f"no {ROLE_TEXT[role]} in the party" + (f": {', '.join(c.get('name') for c in fit)} (camp)" if fit else ""))
    dead = [r for r in party if r.get("dead")]
    if dead:
        bits.append("dead: " + ", ".join(label(r["uuid"]) for r in dead) + " (Scroll of Revivify, or Withers at camp)"
                    + (f"; camp can stand in: {', '.join(c.get('name') for c in camp)}" if camp else ""))
    # (c) skill gaps a camp member fills
    if skills:
        for key, text, low in SKILL_NEEDS:
            best = max((skills.get(r.get("uuid"), {}).get(key, -99) for r in live), default=-99)
            if best >= low:
                continue
            cands = sorted(((skills.get(c.get("uuid"), {}).get(key, -99), c) for c in camp), key=lambda x: -x[0])
            if cands and cands[0][0] >= best + SKILL_GAIN:
                v, c = cands[0]
                bits.append(f"party best {text} {best:+d}; {c.get('name')} (camp) {v:+d}")
    # (d) camp members waiting for their level-ups (done once they rejoin)
    lv = [c for c in camp if c.get("level_up")]
    if lv:
        bits.append("camp level-ups: " + ", ".join(f"{c.get('name')} L{c.get('level')}→{c.get('level_up')}" for c in lv)
                    + " → level_up(who=\"" + str(lv[0].get("name")) + "\") swaps in, levels, swaps back")
    return bits


def camp_lines(camp: list[dict]) -> list[str]:
    """get_party(camp=true): one line per camp companion: name, class, level (pending), key skills, gear."""
    out = []
    for c in camp:
        s = c.get("skills") or {}
        top = sorted(((v, k) for k, v in s.items() if isinstance(v, (int, float)) and v >= 4), reverse=True)[:4]
        eq = c.get("equipment") or {}
        gear = [f"{slot}={(it or {}).get('name')}" for slot, it in sorted(eq.items())
                if slot == "Breast" or "Hand" in slot]
        ln = f"{c.get('name')} (camp) {class_text(c)}"
        if c.get("level_up") or c.get("can_reach"):
            ln += f", level up pending → L{c.get('can_reach') or c.get('level_up')} (level_up after it joins)"
        if top:
            ln += " · skills " + ", ".join(f"{k} {int(v):+d}" for v, k in top)
        if gear:
            ln += " · " + "; ".join(gear)
        out.append(ln)
    return out or ["camp: nobody waiting at camp"]


class HintTracker:
    """Print the PARTY hint only when it changes or on a full view, and refresh the skill read (state.camp)
    only when the roster or the region changed: per get_status it costs one dict comparison."""

    def __init__(self) -> None:
        self.sig: Any = None
        self.text = ""
        self.skills: dict | None = None
        self.skill_sig: Any = None
        self.avatars: set = set()
        self.at = 0.0

    def need_skills(self, party: list[dict], camp: list[dict]) -> bool:
        sig = (tuple(sorted(str(r.get("uuid")) for r in party)), tuple(sorted(str(c.get("uuid")) for c in camp)))
        if sig != self.skill_sig:
            self.skill_sig = sig
            return True
        return False

    def line(self, bits: list[str], force: bool = False) -> str:
        text = ("PARTY: " + " | ".join(bits)) if bits else ""
        if text == self.text and not force:
            return ""
        self.text, self.at = text, time.monotonic()
        return text


# --- TALK: the "!" over a companion who wants to talk (State.lua wants_talk: eoc HasExclamationDialog) -----
# S023 (Gale) and S026 (Withers): the agent saw the "!" only on a screenshot. Party rows carry it always (they
# stand by you), camp rows only while the party is at camp (where the player sees it).

def talk_bits(st: Any, label: Callable[[str], str]) -> list[str]:
    if not isinstance(st, dict) or st.get("in_combat") or st.get("in_dialogue"):
        return []
    out = []
    for r in st.get("party") or []:
        if isinstance(r, dict) and r.get("wants_talk") and not r.get("summon") and r.get("uuid"):
            out.append(f"{label(r['uuid'])} wants to talk (the '!' over them)")
    for c in st.get("camp") or []:
        if isinstance(c, dict) and c.get("wants_talk") and c.get("uuid"):
            out.append(f"{label(c['uuid'])} (at camp) wants to talk (the '!' over them)")
    return out


class TalkTracker:
    """'TALK: e5 Wyll wants to talk … → talk_to(e5)' when the set changes or on a full view."""

    def __init__(self) -> None:
        self.text = ""

    def line(self, st: Any, label: Callable[[str], str], force: bool = False) -> str:
        bits = talk_bits(st, label)
        text = ("TALK: " + " | ".join(bits) + " → talk_to(<id>) now (a companion's own scene; it may not wait)") if bits else ""
        if text == self.text and not force:
            return ""
        self.text = text
        return text


# --- swap_party: the companion's own dialogue (the fair way a player swaps) ------------------------------
# Live texts: a party member's menu "I need you to remain in the camp for a while, rather than on the road
# with me." (S023, Gale); a recruit with a full party "Looks like your troop's all trooped up …" →
# "True. Meet me at camp." / "Gale could do with a rest." / "Take Shadowheart's place." / "You can trade
# places with Wyll." / "I'll be back." (S021, Karlach).
JOIN_RX = re.compile(r"\b(join|in (?:my|the|our) party|with me|with us|on the road|come along|come with|"
                     r"travel(?:l?ing)? with|accompany|by my side|need you|back in|let'?s go|come on)\b", re.I)
STAY_RX = re.compile(r"\b(remain|wait|stay|rest\b|leave|camp|later|be back|goodbye|farewell|nevermind|never mind)", re.I)


def _opts(options: list[dict]) -> list[dict]:
    return [o for o in options or [] if isinstance(o, dict) and not o.get("off")]


def _chat_question(text: str) -> bool:
    """The join words sit mid-question: camp chat, not the request. Live 2026-10-10: Halsin's "Still feeling at
    home here with us? ..." is chat; Gale's "Join me, why don't you? There's plenty of adventure to be had." joins."""
    for sent in re.findall(r"[^.!?]*[.!?]?", text):
        m = JOIN_RX.search(sent)
        if m:
            return sent.rstrip().endswith("?") and bool(sent[:m.start()].strip())
    return False


def pick_join(options: list[dict]) -> int | None:
    """The 'join my party' answer in a camp companion's menu: matches JOIN_RX, no skill check tag, and no
    stay/leave/camp words (so a party member's 'remain in the camp … on the road with me' never matches)."""
    for o in _opts(options):
        t = str(o.get("text") or "")
        if JOIN_RX.search(t) and not STAY_RX.search(t) and not o.get("tags") and not _chat_question(t):
            return int(o.get("i") or 0) or None
    return None


def pick_swap(options: list[dict], out_name: str) -> int | None:
    """The full-party menu's answer that sends `out_name` to camp ('Take Shadowheart's place.', 'Gale could
    do with a rest.', 'You can trade places with Wyll.')."""
    key = first_name(out_name)
    if not key:
        return None
    for o in _opts(options):
        if re.search(r"\b" + re.escape(key) + r"(?:'s)?\b", str(o.get("text") or ""), re.I):
            return int(o.get("i") or 0) or None
    return None


def pick_dismiss(options: list[dict]) -> int | None:
    """A party member's 'remain in the camp' answer."""
    for o in _opts(options):
        t = str(o.get("text") or "")
        if re.search(r"\b(remain|wait|stay)\b", t, re.I) and re.search(r"\bcamp\b", t, re.I) and not o.get("tags"):
            return int(o.get("i") or 0) or None
    return None


def find_row(rows: list[dict], ref: str, resolve: Callable[[str], str] | None = None) -> dict | None:
    """The row named `ref` (first name, full name, or a handle/uuid via resolve)."""
    key = str(ref or "").strip().lower()
    if not key:
        return None
    for r in rows:
        if str(r.get("name") or "").lower() == key or first_name(r.get("name")) == first_name(key):
            return r
    if resolve:
        try:
            u = resolve(ref)
        except Exception:  # noqa: BLE001
            return None
        return next((r for r in rows if r.get("uuid") == u), None)
    return None

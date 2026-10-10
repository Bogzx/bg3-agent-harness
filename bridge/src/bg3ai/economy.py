"""Resources and economy: camp supplies, junk, gear upgrades, and the ALERTS line.

Live S025/S026: the party long-rested partially twice (supplies 27/40, 31/40) and went into the Mausoleum
puzzle at 34/88 HP, paid 1000 of 1093 gold for a key and then couldn't buy food, carried bags of sellable
junk while Encumbered, and left a rare Sentinel Shield in a bag. Every fact needed was in the game; the
agent had to open panels to learn it. This module turns state.inventory (items with their stats, the
party's supply total, per-member proficiencies and encumbrance) plus state.status into:

* alerts(): one compact "ALERTS ..." line (low HP out of combat, supplies < 40, gold < 200, Encumbered,
  better gear in a bag, a partial rest), shown in get_status and in look/get_status deltas when it changes;
* junk(): what a skilled player sells at every trader (never equipped/story/consumables/food/keys/kept);
* upgrades(): better-than-equipped gear in the party's bags, per member, proficiency-aware;
* gear_report(): the same as Markdown lines for the next session's notes.

Pure functions over the mod's payloads, so it is tested offline (tests/test_economy.py).
"""

from __future__ import annotations

import re
from typing import Any, Callable

SUPPLIES_FULL_REST = 40   # a full long rest consumes 40 camp supplies (game rule)
GOLD_RESERVE = 200        # strategy.md: keep a reserve for food, potions and scrolls
LOW_HP = 0.5

_RARITY = {None: 0, "": 0, "common": 0, "uncommon": 1, "rare": 2, "very_rare": 3, "legendary": 4}
# Items a player keeps even when they look like clutter (tools, light, keys, camp kit).
_KEEP_NAME = re.compile(r"key|thieves' tools|trap disarm|torch|shovel|rope|camp supply|keychain|pouch|"
                        r"backpack|bag\b|sack|tadpole|soul coin|idol|orb|letter|note|map|journal|diary", re.I)
_CONSUMABLE_NAME = re.compile(r"potion|elixir|scroll|arrow|bolt|grenade|alchemist's fire|acid vial|bomb|oil of|"
                              r"poison|salve|grease|smokepowder|runepowder|water", re.I)
_CONSUMABLE_CAT = re.compile(r"^(Potion|MagicScroll|Arrow|Throwable|Poison|Ingredient)")
_JUNK_CAT = re.compile(r"^(Junk|Luxury|RottenFood|Dye|PreciousGem)")
_SLOT_ALIASES = {"Ring2": "Ring"}
_ARMOR_PROF = {"Light": "LightArmor", "Leather": "LightArmor", "Padded": "LightArmor", "StuddedLeather": "LightArmor",
               "Hide": "MediumArmor", "ChainShirt": "MediumArmor", "ScaleMail": "MediumArmor",
               "BreastPlate": "MediumArmor", "HalfPlate": "MediumArmor",
               "RingMail": "HeavyArmor", "ChainMail": "HeavyArmor", "Splint": "HeavyArmor", "Plate": "HeavyArmor"}


def _caster(prof: set[str]) -> bool:
    """Wizard/Sorcerer-like: no martial weapons and no medium armour (they fight with spells)."""
    return bool(prof) and not prof & {"MartialWeapons", "MediumArmor", "HeavyArmor"}


def _rar(it: dict) -> int:
    return _RARITY.get(it.get("rarity"), 1 if it.get("rarity") else 0)


def _count(it: dict) -> int:
    c = it.get("count")
    return c if isinstance(c, int) and c > 0 else 1


def supplies_text(inv: dict) -> str | None:
    """'18/40 (12 carried + 6 in the camp chest)': what the rest screen counts (carried + camp chest)."""
    sup = inv.get("supplies") if isinstance(inv, dict) else None
    if not isinstance(sup, (int, float)):
        return None
    camp = inv.get("camp_supplies")
    if isinstance(camp, (int, float)):
        return f"{sup + camp}/{SUPPLIES_FULL_REST} ({sup} carried + {camp} in the camp chest)"
    return f"{sup}/{SUPPLIES_FULL_REST} carried"


def total_supplies(inv: dict) -> int | float | None:
    sup = inv.get("supplies") if isinstance(inv, dict) else None
    if not isinstance(sup, (int, float)):
        return None
    camp = inv.get("camp_supplies")
    return sup + (camp if isinstance(camp, (int, float)) else 0)


def is_food(it: dict) -> bool:
    return bool(it.get("supply"))


def is_consumable(it: dict) -> bool:
    return (it.get("tab") in ("Consumable", "Magical") or bool(_CONSUMABLE_CAT.match(it.get("cat") or ""))
            or bool(_CONSUMABLE_NAME.search(it.get("name") or "")))


def _equipable(it: dict) -> bool:
    return bool(it.get("slot_type")) or "ac" in it or "dmg" in it


def is_junk(it: dict, keep: set[str] | None = None, wanted: set[str] | None = None) -> bool:
    """Sell/stash candidates: what a skilled player sells at every trader. Never: equipped, story, bags,
    food, consumables (potions, scrolls, arrows, throwables), keys/tools/light, kept items (keep), gear
    someone should wear (wanted), magic gear (rarity >= uncommon: the agent decides)."""
    if (it.get("equipped_slot") or it.get("story") or it.get("container") or it.get("unique") or it.get("gold")
            or is_food(it) or is_consumable(it)):
        return False
    if it.get("uuid") in (keep or set()) or it.get("uuid") in (wanted or set()):
        return False
    if it.get("tab") == "BooksAndKeys" or _KEEP_NAME.search(it.get("name") or ""):
        return False
    if _equipable(it):
        return _rar(it) == 0 and (it.get("value") or 0) > 0
    if _JUNK_CAT.match(it.get("cat") or ""):
        return True
    return it.get("tab") == "Misc" and _rar(it) == 0 and (it.get("value") or 0) > 0


def junk(inv: dict, keep: set[str] | None = None) -> list[dict]:
    items = [it for it in (inv or {}).get("items") or [] if isinstance(it, dict)]
    wanted = {u["uuid"] for u in upgrades(inv)}
    return [it for it in items if is_junk(it, keep, wanted)]


# --- gear ---------------------------------------------------------------------------------------------

def _dice_avg(d: str | None) -> float:
    total = 0.0
    for n, s in re.findall(r"(\d+)d(\d+)", d or ""):
        total += int(n) * (int(s) + 1) / 2
    plus = re.findall(r"[+](\d+)(?!d)", d or "")
    return total + sum(int(p) for p in plus)


def _slot(it: dict) -> str:
    s = it.get("equipped_slot") or it.get("slot_type") or ""
    s = _SLOT_ALIASES.get(s, s)
    if it.get("shield"):
        return "MeleeOffHand"
    if s in ("Melee Main Weapon", "Melee Offhand Weapon", "MeleeMainHand"):
        return "MeleeMainHand"
    if s in ("Ranged Main Weapon", "Ranged Offhand Weapon", "RangedMainHand"):
        return "RangedMainHand"
    return s


def _proficient(it: dict, prof: set[str]) -> bool:
    if not prof:
        return True  # unknown proficiencies: don't block
    if "ac" in it:
        if it.get("shield"):
            return "Shields" in prof
        if _slot(it) != "Breast":
            return True  # boots/helmets/gloves carry an armour type but need no proficiency
        need = _ARMOR_PROF.get(it.get("armor") or "")
        if not need:
            groups = set((it.get("prof") or "").split("|")) - {""}
            need = next(iter(groups & {"LightArmor", "MediumArmor", "HeavyArmor"}), None)
        return need is None or need in prof
    if "dmg" in it:
        groups = set((it.get("prof") or "").split("|")) - {""}
        return not groups or bool(groups & prof)
    return True


def _score(it: dict) -> float:
    """Comparable value of an item in its slot: armour/shield AC, weapon average damage + enchantment, else
    rarity; rarity always breaks ties."""
    if "ac" in it and (it.get("ac") or it.get("shield") or _slot(it) == "Breast"):
        return (it.get("ac") or 0) * 10 + _rar(it)
    if "dmg" in it:
        return (_dice_avg(it.get("dmg")) + 2 * (it.get("ench") or 0)) * 10 + _rar(it) * 3
    return _rar(it) * 10


def _why(it: dict, cur: dict | None) -> str:
    def desc(x: dict) -> str:
        if x.get("ac") or x.get("shield") or (x.get("ac") is not None and _slot(x) == "Breast"):
            return f"AC {x.get('ac')}"
        if "dmg" in x:
            return f"{x.get('dmg')}" + (f"+{x['ench']}" if x.get("ench") else "")
        return (x.get("rarity") or "common").replace("_", " ")
    return f"{desc(it)} vs {desc(cur) + ' ' + str(cur.get('name')) if cur else 'empty slot'}"


def upgrades(inv: dict) -> list[dict]:
    """[{who, uuid, name, slot, holder, why, gain}] best first: an item in a party bag that beats what a
    member wears in that slot and that the member is proficient with. Each item goes to one member."""
    items = [it for it in (inv or {}).get("items") or [] if isinstance(it, dict)]
    members = (inv or {}).get("members") or {}
    worn: dict[str, dict[str, dict]] = {}
    for it in items:
        if it.get("equipped_slot") and it.get("owner"):
            worn.setdefault(it["owner"], {})[_slot(it) if not it["equipped_slot"].startswith("Ring")
                                             else it["equipped_slot"]] = it
    spare = [it for it in items if not it.get("equipped_slot") and _equipable(it) and not it.get("story")
             and _slot(it) not in ("", "Underwear", "VanityBody", "VanityBoots", "MusicalInstrument")]
    offers: list[dict] = []
    for who in members:
        prof = set((members.get(who) or {}).get("prof") or [])
        mine = worn.get(who, {})
        main = mine.get("MeleeMainHand")
        for it in spare:
            slot = _slot(it)
            if not _proficient(it, prof):
                continue
            if slot == "Ring":
                cands = [mine.get("Ring"), mine.get("Ring2")]
                cur = None if None in cands else min(cands, key=_score)
            else:
                cur = mine.get(slot)
            if cur and cur.get("story"):
                continue  # a story item in the slot (Moonlantern): the agent put it there on purpose
            if slot == "MeleeOffHand":
                if not it.get("shield") or (main and main.get("twohanded")):
                    continue
                if _caster(prof) and _rar(it) == 0:
                    continue
            if slot == "MeleeMainHand":
                if it.get("twohanded") and mine.get("MeleeOffHand", {}).get("shield"):
                    continue
                if _caster(prof) and _rar(it) <= _rar(cur or {}):
                    continue  # casters fight with spells: only magic weapons are worth a swap
            gain = _score(it) - (_score(cur) if cur else 0)
            if cur is None and "ac" not in it and "dmg" not in it and _rar(it) == 0:
                continue  # a plain hat in an empty slot is not worth a line
            if gain > 0:
                offers.append({"who": who, "uuid": it["uuid"], "name": it.get("name"), "slot": slot,
                               "holder": it.get("owner"), "why": _why(it, cur), "gain": gain,
                               "rarity": it.get("rarity"), "caster": _caster(prof)})
    # Best gain first; on a tie a fighter gets the shield/weapon before a caster, then the one carrying it.
    offers.sort(key=lambda o: (-o["gain"], o["caster"] and o["slot"] in ("MeleeOffHand", "MeleeMainHand"),
                               o["holder"] != o["who"]))
    taken_items, taken_slots, out = set(), set(), []
    for o in offers:
        if o["uuid"] in taken_items or (o["who"], o["slot"]) in taken_slots:
            continue
        taken_items.add(o["uuid"])
        taken_slots.add((o["who"], o["slot"]))
        out.append(o)
    return out


# --- alerts -------------------------------------------------------------------------------------------

def alerts(status: dict | None, inv: dict | None, name: Callable[[str], str] = lambda u: u,
           partial_rest: str | None = None, short_rests: int | None = None) -> list[str]:
    """Short alert phrases, most urgent first. status: state.status; inv: state.inventory; short_rests: the
    party's short rests left (ui.rest_info), None when unknown."""
    out: list[str] = []
    st = status if isinstance(status, dict) else {}
    if not st.get("in_combat"):
        low = []
        for m in st.get("party") or []:
            hp, mx = m.get("hp"), m.get("max_hp")
            if isinstance(hp, (int, float)) and isinstance(mx, (int, float)) and mx > 0 and hp < LOW_HP * mx:
                low.append(f"{m.get('name') or name(m.get('uuid', '?'))} {hp}/{mx}")
        if low:
            sup = total_supplies(inv or {})
            if isinstance(short_rests, int) and short_rests > 0:
                fix = f"short rests left {short_rests}: short_rest"
            elif short_rests == 0:
                fix = (f"no short rests left: long rest needs {SUPPLIES_FULL_REST} supplies, have "
                       f"{sup if isinstance(sup, (int, float)) else '?'}")
            else:
                fix = "short_rest/heal before a fight or puzzle"
            out.append("low HP " + ", ".join(low) + f" ({fix})")
    if partial_rest:
        out.append(partial_rest)
    if isinstance(inv, dict) and "items" in inv:
        sup = total_supplies(inv)
        if sup is not None and sup < SUPPLIES_FULL_REST:
            out.append(f"camp supplies {supplies_text(inv)} (pick up/buy food before a long rest)")
        gold = inv.get("gold")
        if isinstance(gold, (int, float)) and gold < GOLD_RESERVE:
            out.append(f"gold {gold} < {GOLD_RESERVE} reserve (sell junk: trade sell=junk)")
        for u, m in (inv.get("members") or {}).items():
            if (m or {}).get("enc"):
                kg = ""
                if isinstance(m.get("kg"), (int, float)) and isinstance(m.get("limit"), (int, float)):
                    kg = f" {m['kg']}/{m['limit']}kg (drop {max(0.1, round(m['kg'] - m['limit'], 1))}kg"
                    if m["enc"] < 2 and isinstance(m.get("heavy"), (int, float)):
                        kg += f"; Heavily at {m['heavy']}kg"
                    kg += ")"
                out.append(f"{name(u)} {'Heavily ' if m['enc'] >= 2 else ''}Encumbered{kg} "
                           "(sell or stash junk: manage_inventory op=camp item=junk)")
        ups = upgrades(inv)
        if ups:
            shown = "; ".join(f"{name(o['who'])}: {o['name']}" for o in ups[:3])
            out.append(f"better gear in bags: {shown}{' …' if len(ups) > 3 else ''} (equip_best)")
    return out


class AlertTracker:
    """ALERTS line: always in a full get_status; in deltas only when the set of alerts changed."""

    def __init__(self) -> None:
        self.last: tuple = ()
        self.partial_rest: str | None = None
        self.short_rests: int | None = None  # from ui.rest_info (get_status) and rest results

    def line(self, alerts_: list[str], force: bool = False) -> str:
        key = tuple(alerts_)
        changed = key != self.last
        self.last = key
        if not alerts_ or not (force or changed):
            return ""
        return "ALERTS " + " | ".join(alerts_)

    def rested(self, result: Any) -> None:
        """Remember the last long rest's outcome (from ui.long_rest's result)."""
        if not isinstance(result, dict) or result.get("rest") not in ("full", "partial"):
            return
        if result["rest"] == "full":
            self.short_rests = None  # restored; the next get_status reads the real count
        if result["rest"] == "partial":
            self.partial_rest = (f"last long rest was PARTIAL ({result.get('supplies_used', '?')}/"
                                 f"{result.get('supplies_required', '?')} supplies: half HP and slots)")
        else:
            self.partial_rest = None


def short_rest_text(before: dict | None, after: dict | None, result: Any) -> str:
    """'HP Claude 34→62/88, Gale 46→64/64 · short rests left 1/2' from state.status before/after a short rest."""
    def hp(st):
        return {m.get("uuid"): (m.get("name"), m.get("hp"), m.get("max_hp"))
                for m in (st or {}).get("party") or [] if isinstance(m, dict)}
    b, a = hp(before), hp(after)
    parts = [f"{a[u][0]} {b[u][1]}→{a[u][1]}/{a[u][2]}" for u in a if u in b and a[u][1] != b[u][1]]
    out = "HP " + ", ".join(parts) if parts else "HP unchanged"
    if isinstance(result, dict) and result.get("short_rests_left") is not None:
        out += f" · short rests left {result['short_rests_left']}" + (f"/{result['of']}" if result.get("of") else "")
    return out


def gear_report(inv: dict, name: Callable[[str], str] = lambda u: u) -> list[str]:
    """Markdown bullet lines for the next session (inventory-notes / HANDOFF)."""
    lines = []
    for o in upgrades(inv):
        held = "" if o["holder"] == o["who"] else f" (in {name(o['holder'])}'s bag)"
        lines.append(f"- {name(o['who'])}: equip {o['name']}{held} in {o['slot']}: {o['why']}")
    j = junk(inv)
    if j:
        val = sum((it.get("value") or 0) * _count(it) for it in j)
        kg = sum((it.get("weight") or 0) * _count(it) for it in j)
        lines.append(f"- junk to sell: {len(j)} items, ~{val} g, {kg:.1f} kg (trade sell=junk at any trader)")
    if supplies_text(inv):
        lines.append(f"- camp supplies: {supplies_text(inv)}")
    return lines

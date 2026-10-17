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


_SPELL_GEAR_NAME = re.compile(r"spellcrux|spell ?(save|slot|attack)", re.I)


def _spell_gear(it: dict) -> bool:
    """Gear that only helps a spellcaster (spell save DC / attack / slots): the mod's spellgear flag, or the name."""
    return bool(it.get("spellgear") or _SPELL_GEAR_NAME.search(str(it.get("name") or "")))


def _casts(mem: dict, prof: set[str]) -> bool:
    """Has spell slots (the mod's caster flag); without it, a Wizard-like proficiency set."""
    c = mem.get("caster")
    return bool(c) if c is not None else _caster(prof)


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


def _prof_reason(it: dict, prof: set[str]) -> str:
    """Which of the member's proficiencies (engine Proficiency.Flags: class AND race, e.g. a Human wizard has
    LightArmor + Shields) makes the item legal, so the agent doesn't undo a right pick (S027: Gale)."""
    if not prof:
        return ""
    if it.get("shield"):
        need = "Shields"
    elif "ac" in it and _slot(it) == "Breast":
        need = _ARMOR_PROF.get(it.get("armor") or "") or next(iter(
            (set((it.get("prof") or "").split("|")) - {""}) & {"LightArmor", "MediumArmor", "HeavyArmor"}), None)
    elif "dmg" in it:
        need = next(iter(sorted((set((it.get("prof") or "").split("|")) - {""}) & prof)), None)
    else:
        need = None
    return f"; proficient: {need} (game's own flags, race included)" if need and need in prof else ""


def _is_weapon(it: dict) -> bool:
    """A real weapon: weapon stats in a Simple/Martial proficiency group. A torch (Clubs group, the engine's
    'Torch' property) or a weapon-stat item with no group is a tool/improvised: never ranked as a weapon."""
    groups = set(str(it.get("prof") or "").split("|"))
    return "dmg" in it and not it.get("torch") and bool(groups & {"SimpleWeapons", "MartialWeapons"})


def _ability_mod(it: dict, m: dict | None) -> int:
    """The member's attack/damage modifier with this weapon: DEX for ranged, the better of STR/DEX for finesse,
    else STR (5e/BG3 rule). 0 when the scores are unknown."""
    def md(v: Any) -> int:
        return (v - 10) // 2 if isinstance(v, int) else 0
    if not m:
        return 0
    if _slot(it) == "RangedMainHand":
        return md(m.get("dex"))
    best = max(md(m.get("str")), md(m.get("dex"))) if it.get("finesse") else md(m.get("str"))
    if m.get("binds") == "pact":
        best = max(best, md(m.get("cha")))  # Pact of the Blade: the bound melee weapon attacks with CHA
    return best


def _melee_caster(prof: set[str], mem: dict) -> bool:
    """Fights with spells, so only magic counts in the weapon hand. A Pact of the Blade warlock has a caster's
    proficiencies but fights with the pact weapon (S032 Wyll: Booming Blade, Deepened Pact extra attack)."""
    return _caster(prof) and mem.get("binds") != "pact"


def _score(it: dict, m: dict | None = None) -> float:
    """Comparable value of an item in its slot: armour/shield AC, weapon average damage + enchantment (counted
    twice: it adds to hit and damage) + the wielder's ability modifier, else rarity; rarity always breaks ties.
    A torch or other non-weapon in a weapon slot scores like an empty hand."""
    if "ac" in it and (it.get("ac") or it.get("shield") or _slot(it) == "Breast"):
        return (it.get("ac") or 0) * 10 + _rar(it)
    if "dmg" in it:
        if not _is_weapon(it):
            return 0
        return (_dice_avg(it.get("dmg")) + 2 * (it.get("ench") or 0) + _ability_mod(it, m)) * 10 + _rar(it) * 3
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
    members = (inv or {}).get("members") or {}
    # A temporary follower (S053: Zanner Toobin got Ketheric's Shield) is neither dressed nor stripped.
    temp = {u for u, m in members.items() if (m or {}).get("follower")}
    members = {u: m for u, m in members.items() if u not in temp}
    items = [it for it in (inv or {}).get("items") or [] if isinstance(it, dict) and it.get("owner") not in temp]
    worn: dict[str, dict[str, dict]] = {}
    for it in items:
        if it.get("equipped_slot") and it.get("owner"):
            worn.setdefault(it["owner"], {})[_slot(it) if not it["equipped_slot"].startswith("Ring")
                                             else it["equipped_slot"]] = it
    spare = [it for it in items if not it.get("equipped_slot") and _equipable(it) and not it.get("story")
             and _slot(it) not in ("", "Underwear", "VanityBody", "VanityBoots", "MusicalInstrument")]
    offers: list[dict] = []
    for who in members:
        mem = members.get(who) or {}
        prof = set(mem.get("prof") or [])
        mine = worn.get(who, {})
        main = mine.get("MeleeMainHand")
        for it in spare:
            slot = _slot(it)
            if not _proficient(it, prof) or ("dmg" in it and not _is_weapon(it)):
                continue  # never a torch/tool as a weapon
            if slot == "Ring":
                cands = [mine.get("Ring"), mine.get("Ring2")]
                cur = None if None in cands else min(cands, key=_score)
            else:
                cur = mine.get(slot)
            if cur and cur.get("story"):
                continue  # a story item in the slot (Moonlantern): the agent put it there on purpose
            if cur and cur.get("bound"):
                continue  # pact/weapon-bond weapon: re-binding costs an action, a summoned pact weapon vanishes
            if it.get("bound") and it.get("owner") != who:
                continue  # bound to its owner (it returns to them): never handed to someone else
            if cur and _rar(cur) > _rar(it):
                continue  # worn gear is magic (properties beyond AC/damage): never swapped for plainer stats
            if _spell_gear(it) and not _casts(mem, prof):
                continue  # S028: the Spellcrux Amulet (restores a spell slot) went to Karlach by rarity alone
            if slot == "MeleeOffHand":
                if not it.get("shield") or (main and main.get("twohanded")):
                    continue
                if _caster(prof) and _rar(it) == 0:
                    continue
            if slot == "MeleeMainHand":
                if it.get("twohanded") and mine.get("MeleeOffHand", {}).get("shield"):
                    continue
                if _melee_caster(prof, mem) and _rar(it) <= _rar(cur or {}):
                    continue  # casters fight with spells: only magic weapons are worth a swap
            gain = _score(it, mem) - (_score(cur, mem) if cur else 0)
            if slot == "MeleeMainHand" and _melee_caster(prof, mem) and cur:
                # a caster fights with spells: only the magic counts, so a melee member wins a contested weapon
                # (live 13:4x: STR-8 Gale's finesse bonus outbid Shadowheart for the +1 Syringe)
                gain = min(gain, (_rar(it) - _rar(cur)) * 10)
            if cur is None and "ac" not in it and "dmg" not in it and _rar(it) == 0:
                continue  # a plain hat in an empty slot is not worth a line
            if gain > 0:
                offers.append({"who": who, "uuid": it["uuid"], "name": it.get("name"), "slot": slot,
                               "holder": it.get("owner"), "why": _why(it, cur) + _prof_reason(it, prof)
                               + ("; bind it as the pact weapon (Bind Pact Weapon, 1 action) to attack with CHA"
                                  if mem.get("binds") == "pact" and slot == "MeleeMainHand" and not it.get("bound")
                                  else ""), "gain": gain,
                               "rarity": it.get("rarity"), "caster": _melee_caster(prof, mem)})
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


def _kind(it: dict) -> str:
    """'dagger' from the weapon's proficiency groups ('Daggers|SimpleWeapons')."""
    groups = [g for g in str(it.get("prof") or "").split("|") if g and not g.endswith("Weapons")]
    g = groups[0] if groups else ""
    return (g[:-1] if g.endswith("s") else g).lower()


def main_hands(inv: dict, name: Callable[[str], str]) -> str:
    """'MAIN HANDS Gale Moonlantern (light) | Shadowheart Syringe +1 dagger 1d4+1 uncommon | …': what each member
    fights with, so an odd name reads as the weapon it is (acceptance 12:2x: Shadowheart's Syringe is a +1 finesse
    dagger, her best one-hander beside the shield, but looked like junk left in her hand)."""
    items = [it for it in (inv or {}).get("items") or [] if isinstance(it, dict)]
    out = []
    for who in (inv or {}).get("members") or {}:
        it = next((i for i in items if i.get("owner") == who and i.get("equipped_slot") == "MeleeMainHand"), None)
        if it is None:
            out.append(f"{name(who)} (empty)")
            continue
        if _is_lantern(it):
            out.append(f"{name(who)} {it.get('name')} (light)")
            continue
        ench = it.get("ench") or 0
        dmg = f"{it.get('dmg')}{f'+{ench}' if ench else ''}" if it.get("dmg") else ""
        kind = _kind(it)
        desc = " ".join(x for x in (f"+{ench}" if ench and kind else "", kind, dmg,
                                    "2-handed" if it.get("twohanded") else "",
                                    str(it.get("rarity") or "") if _rar(it) else "",
                                    {"pact": "pact weapon", "weapon_bond": "bonded"}.get(it.get("bound") or "", "")) if x)
        out.append(f"{name(who)} {it.get('name')}" + (f" ({desc})" if desc else ""))
    return "MAIN HANDS " + " | ".join(out) if out else ""


def _is_lantern(it: dict) -> bool:
    """A story light held in the main hand for its aura (the Moonlantern: Torch|Unstowable, SCL_MOONSHIELD on
    everyone near the member who EQUIPS it; in a bag it does nothing, S025)."""
    return bool(it.get("story") and (it.get("torch") or "lantern" in str(it.get("name") or "").lower()))


def lantern_plan(inv: dict) -> dict | None:
    """{lantern, name, frm, to} when a weapon user holds the story lantern in the main hand while a caster could
    carry it: the aura works from whoever holds it, so the caster (who fights with spells) holds the light and
    the fighter gets a real weapon back (S025-S027: Claude, a paladin, fought with the Moonlantern while the
    Sword of Justice sat in a bag). Only when the fighter then has a weapon to equip (a spare one in the bags,
    or the caster's own once it is freed)."""
    items = [it for it in (inv or {}).get("items") or [] if isinstance(it, dict)]
    members = (inv or {}).get("members") or {}
    lant = next((it for it in items if _is_lantern(it) and _slot(it) == "MeleeMainHand"
                 and it.get("equipped_slot") and it.get("owner") in members), None)
    if not lant:
        return None
    frm = lant["owner"]
    if _melee_caster(set((members.get(frm) or {}).get("prof") or []), members.get(frm) or {}):
        return None  # a caster already holds it: right holder

    def main_of(u: str) -> dict:
        return next((it for it in items if it.get("owner") == u and it.get("equipped_slot")
                     and _slot(it) == "MeleeMainHand"), {})
    casters = [u for u, m in members.items() if u != frm and not (m or {}).get("follower")
               and _melee_caster(set((m or {}).get("prof") or []), m or {})]   # never to a temporary follower
    if not casters:
        return None
    to = min(casters, key=lambda u: (_rar(main_of(u)), _score(main_of(u)) if main_of(u) else 0))
    after = apply_lantern(inv, {"lantern": lant["uuid"], "frm": frm, "to": to})
    if not any(o["who"] == frm and o["slot"] == "MeleeMainHand" for o in upgrades(after)):
        return None  # nothing to fight with instead: the lantern stays (a torch beats empty hands)
    return {"lantern": lant["uuid"], "name": lant.get("name") or "Moonlantern", "frm": frm, "to": to}


def apply_lantern(inv: dict, plan: dict) -> dict:
    """The inventory after the plan's moves (lantern to the caster's main hand, the caster's weapon to their
    bag, the fighter's main hand empty): what upgrades() then fills with the best weapon."""
    items = []
    for it in (inv or {}).get("items") or []:
        if not isinstance(it, dict):
            continue
        it = dict(it)
        if it.get("uuid") == plan["lantern"]:
            it["owner"], it["equipped_slot"] = plan["to"], "MeleeMainHand"
        elif it.get("owner") == plan["to"] and it.get("equipped_slot") and _slot(it) == "MeleeMainHand":
            it["slot_type"] = it.get("slot_type") or it.pop("equipped_slot")
            it.pop("equipped_slot", None)
        items.append(it)
    return dict(inv, items=items)


# --- alerts -------------------------------------------------------------------------------------------

def revive_scrolls(inv: dict | None) -> int:
    """Scrolls of Revivify in the party's bags (stack counts)."""
    n = 0
    for it in (inv or {}).get("items") or [] if isinstance(inv, dict) else []:
        if isinstance(it, dict) and "revivify" in str(it.get("name") or "").lower():
            n += int(it.get("count") or 1)
    return n


def alerts(status: dict | None, inv: dict | None, name: Callable[[str], str] = lambda u: u,
           partial_rest: str | None = None, short_rests: int | None = None,
           dead_allies: list[dict] | None = None) -> list[str]:
    """Short alert phrases, most urgent first. status: state.status; inv: state.inventory; short_rests: the
    party's short rests left (ui.rest_info), None when unknown; dead_allies: allied NPCs that died ({uuid, name},
    debrief.Tracker.dead_allies) and were not revived."""
    out: list[str] = []
    st = status if isinstance(status, dict) else {}
    # Deaths first (S043 Jaheira, S045 Aylin died with 5 Scrolls of Revivify in the bags, never mentioned again).
    dead = [m for m in st.get("party") or [] if isinstance(m, dict) and m.get("dead") and not m.get("summon")]
    if dead or dead_allies:
        n = revive_scrolls(inv) if isinstance(inv, dict) and "items" in inv else None
        have = (f"{n} Scroll(s) of Revivify in the bags" if n else "no Scroll of Revivify in the bags" if n == 0
                else "Scroll of Revivify")
        if dead:
            out.append("DEAD " + ", ".join(m.get("name") or name(m.get("uuid", "?")) for m in dead)
                       + f" (revive: {have} on the body, a Revivify spell, or Withers at camp for 200 g)")
        if dead_allies:
            out.append("ally DIED " + ", ".join(f"{name(a['uuid'])}" if a.get("uuid") else str(a.get("name"))
                                                for a in dead_allies)
                       + f" (revive: {have} on the body before leaving; Withers can't raise an ally; or reload)")
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
        lp = lantern_plan(inv)
        if lp:
            out.append(f"{name(lp['frm'])} fights with the {lp['name']} in the main hand: {name(lp['to'])} (caster) "
                       "can hold the light for everyone (equip_best moves it and arms "
                       f"{name(lp['frm'])})")
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
        n = lambda v: int(v) if isinstance(v, float) and v.is_integer() else v  # the mod sends 1.0/2.0
        out += f" · short rests left {n(result['short_rests_left'])}" + (f"/{n(result['of'])}" if result.get("of") else "")
    return out


def gear_report(inv: dict, name: Callable[[str], str] = lambda u: u) -> list[str]:
    """Markdown bullet lines for the next session (inventory-notes / HANDOFF)."""
    lines = []
    lp = lantern_plan(inv)
    if lp:
        lines.append(f"- {name(lp['to'])}: hold the {lp['name']} (its aura covers the party from any holder); "
                     f"{name(lp['frm'])} fights with a weapon (equip_best)")
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


# ---------------------------------------------------------------------------
# Trade screen as text. S023-S026: every trade call (open, offer, request, confirm) returned one 4-6.6 KB line
# (60 wares + 60 own items, unsorted, as compact() renders dicts); the agent scanned it for food by name. Now:
# wares grouped (food with its supply value, consumables, gear by rarity, other), sorted, capped per group, with
# totals; own items only when listing (top by price + the junk total, sell="junk" sells them); offer/confirm
# calls show the table and the balance only.
# ---------------------------------------------------------------------------

TRADE_GROUP_MAX = 10
TRADE_YOURS_MAX = 12
_GEAR_NAME = re.compile(r"armou?r|mail|plate|shirt|robe|cloak|boots|gloves|gauntlets|bracers|helm|hat|circlet|"
                        r"shield|ring\b|amulet|necklace|sword|axe|mace|hammer|dagger|bow\b|crossbow|staff|spear|"
                        r"rapier|scimitar|flail|glaive|halberd|pike|club|sickle|trident|whip|javelin|morningstar|"
                        r"quarterstaff|wand|rod\b", re.I)


def _trade_rar(it: dict) -> int:
    r = str(it.get("rarity") or "").lower().replace(" ", "_")
    return _RARITY.get({"veryrare": "very_rare"}.get(r, r), 1 if r else 0)


def trade_group(it: dict) -> str:
    """food | consumable | gear | other, from the item's stats (supply, ModifierList kind, category) or name."""
    if is_food(it):
        return "food"
    if is_consumable(it):
        return "consumable"
    if it.get("kind") in ("Armor", "Weapon") or _equipable(it) or _GEAR_NAME.search(it.get("name") or ""):
        return "gear"
    return "other"


def _ware(it: dict, h: Callable[[dict], str]) -> str:
    s = f"{h(it)} {it.get('name') or '?'}"
    if _count(it) > 1:
        s += f" x{_count(it)}"
    s += f" {it.get('price') if it.get('price') is not None else '?'}g"
    if it.get("supply"):
        s += f" supply={it['supply'] * _count(it)}" + (f" ({it['supply']} each)" if _count(it) > 1 else "")
    if it.get("stolen"):
        s += " stolen"
    if it.get("story"):
        s += " story"
    return s


def _price(it: dict) -> int:
    p = it.get("price")
    return p if isinstance(p, (int, float)) else 0


def trade_text(res: dict, h: Callable[[dict], str], listing: bool = True, full: bool = False) -> str:
    """The trade screen for the agent. h(item) -> its handle (e12). listing: show wares and your items (opening or
    trade() with no args); else only the table/balance. full: no caps."""
    gmax = 10 ** 6 if full else TRADE_GROUP_MAX
    lines = []
    head = []
    if res.get("party_gold") is not None:
        head.append(f"your gold {res['party_gold']}")
    if res.get("trader_gold") is not None:
        head.append(f"trader gold {res['trader_gold']}")
    if res.get("price_modifier_pct") is not None:
        head.append(f"prices {res['price_modifier_pct']}% of value")
    lines.append("TRADE " + " · ".join(head) + " (price = the whole stack)")
    off = res.get("offer") or {}
    you, them = off.get("you") or {}, off.get("trader") or {}
    bal = res.get("balance") or {}

    def side(s: dict) -> str:
        # the gold on the table is a Gold stack among the items (S055/S069: "(no id) ? x60 60g, 60 gold")
        items = [f"{_count(it)} gold" if it.get("gold") else
                 f"{h(it)} {it.get('name') or '?'}{' x' + str(_count(it)) if _count(it) > 1 else ''} {_price(it)}g"
                 for it in s.get("items") or [] if isinstance(it, dict)]
        return ", ".join(items) or "nothing"
    if you.get("items") or them.get("items") or you.get("gold") or them.get("gold") or not listing:
        give, get = bal.get("you_give") or 0, bal.get("you_get") or 0
        lines.append(f"TABLE you give: {side(you)} (={give}g) | you get: {side(them)} (={get}g) | "
                     + (f"short by {get - give}g (add items/gold or barter)" if get > give else
                        f"you overpay {give - get}g" if give > get else "even"))
    if res.get("trader_gold") == 0 and "wares" in res:
        # S028: Wulbren's hand-over (no wares, no gold) and a broke trader read like any shop until a confirm failed
        if handover(res):
            lines.append("HAND-OVER: they have no wares and no gold (a quest give, not a shop): "
                         "trade(offer=<id>, confirm=true) gives the item")
        else:
            lines.append("TRADER HAS NO GOLD: selling would give your items away; swap for wares of equal value "
                         "(offer=<ids>, request=<ids>) or sell elsewhere")
    probs = [str(p) for p in res.get("problems") or [] if p]
    if probs:  # S028 15:48: "some items couldn't be moved" said nothing about which or why (an equipped hammer)
        lines.append("NOT MOVED: " + "; ".join(probs))
    if res.get("feedback"):
        lines.append(f"TRADER says: {res['feedback']}")
    if res.get("declined"):
        lines.append("DECLINED: the trader refuses this deal")
    if res.get("can_confirm") is False:
        lines.append("(the Trade button is disabled)")
    if not listing:
        lines.append("(wares/your items unchanged: trade() lists them)")
        return "\n".join(lines)

    wares = [w for w in res.get("wares") or [] if isinstance(w, dict)]
    groups: dict[str, list[dict]] = {"food": [], "consumable": [], "gear": [], "other": []}
    for w in wares:
        groups[trade_group(w)].append(w)
    groups["food"].sort(key=lambda w: (-(w.get("supply") or 0) * _count(w), _price(w)))
    groups["consumable"].sort(key=lambda w: -_price(w))
    groups["gear"].sort(key=lambda w: (-_trade_rar(w), -_price(w)))
    groups["other"].sort(key=lambda w: -_price(w))
    total = sum(_price(w) for w in wares)
    more = " (+more on the screen)" if res.get("wares_more") else ""
    lines.append(f"WARES {len(wares)} items, {total}g total{more}")
    label = {"food": "food (supply = camp supplies; a long rest needs 40)", "consumable": "potions/scrolls/ammo",
             "gear": "gear (best rarity first)", "other": "other"}
    for g in ("food", "consumable", "gear", "other"):
        items = groups[g]
        if not items:
            continue
        shown = items[:gmax]
        if g == "gear":
            body = []
            for it in shown:
                r = it.get("rarity")
                body.append((f"[{r}] " if r else "") + _ware(it, h))
        else:
            body = [_ware(it, h) for it in shown]
        tail = f" | +{len(items) - len(shown)} more ({sum(_price(x) for x in items[len(shown):])}g)" \
            if len(items) > len(shown) else ""
        extra = ""
        if g == "food":
            extra = f" — {sum((x.get('supply') or 0) * _count(x) for x in items)} supplies for " \
                    f"{sum(_price(x) for x in items)}g"
        lines.append(f"  {label[g]}{extra}: " + " | ".join(body) + tail)

    yours = [y for y in res.get("your_items") or [] if isinstance(y, dict)]
    if yours:
        ymax = 10 ** 6 if full else TRADE_YOURS_MAX
        ys = sorted(yours, key=lambda y: -_price(y))
        val = sum(_price(y) for y in yours)
        tail = f" | +{len(ys) - ymax} more" if len(ys) > ymax else ""
        more = "+" if res.get("your_items_more") else ""
        lines.append(f"YOUR ITEMS {len(yours)}{more} sellable, {val}g{more} at these prices (top first; "
                     f"trade(sell=\"junk\") sells the junk in one call): "
                     + " | ".join(_ware(y, h) for y in ys[:ymax]) + tail)
    if res.get("unmatched"):
        lines.append(f"({res['unmatched']} item group(s) could not be named)")
    if not full and (any(len(v) > gmax for v in groups.values()) or len(yours) > TRADE_YOURS_MAX):
        lines.append("(trade(full=true) lists everything)")
    return "\n".join(lines)


# --- trade(npc) opens the trade itself: the conversation's trade answer (S020 Arron, S025 Lann Tarv, S026 Mattis:
# trade(npc) FAILED "no trade screen is open", then talk_to + choose_dialogue "Let me see your wares.") ---

_TRADE_ANSWER = [re.compile(p, re.I) for p in (
    r"\bwares\b", r"\[trade\]|\btrade\b|\bbarter\b",
    r"\b(what (do )?you (have|got|sell|offer)|show me what|let me (see|browse)|i('d| would) like to buy)\b")]


def handover(res: dict | None) -> bool:
    """A trade screen that is a quest hand-over, not a shop: the other side has no wares and no gold (S028 15:48:
    Wulbren's "I have the tools you need - take them." opened it; giving anything there is a donation)."""
    if not isinstance(res, dict) or res.get("wares_more"):
        return False
    wares = [w for w in res.get("wares") or [] if isinstance(w, dict)]
    return not wares and res.get("trader_gold") in (0, None) and "wares" in res


def equipped_offers(inv: dict | None, uuids: list[str], name_of: Callable[[str], str]) -> list[str]:
    """Offered items someone wears: the trade screen lists only unequipped items, so they can't be moved."""
    out = []
    for it in (inv or {}).get("items") or []:
        if isinstance(it, dict) and it.get("uuid") in uuids and it.get("equipped_slot"):
            who = name_of(it["owner"]) if it.get("owner") else "someone"
            out.append(f"{it.get('name') or '?'} is equipped by {who} ({it['equipped_slot']}): the trade screen lists "
                       "only unequipped items; equip something else in that slot first, or offer another item")
    return out


def trade_answer(options: list) -> dict | None:
    """The answer a player picks to open a trader's shop ('Let me see your wares.'), or None."""
    opts = [o for o in options or [] if isinstance(o, dict) and isinstance(o.get("i"), int) and not o.get("off")]
    for rx in _TRADE_ANSWER:
        for o in opts:
            if rx.search(str(o.get("text") or "")):
                return o
    return None


# --- pickpocket (Services.lua ui.pickpocket: the window's items with the game's shown odds) ---

def _pocket(it: dict, h: Callable[[dict], str]) -> str:
    s = f"{h(it)} {it.get('name') or '?'}".strip()
    if _count(it) > 1:
        s += f" x{_count(it)}"
    if it.get("price") is not None:
        s += f" {it['price']}g"
    if _odds_unread(it):
        s += " odds ?"
    elif it.get("chance") is not None:
        s += f" {it['chance']}%" + (f" DC{it['dc']}" if it.get("dc") is not None else "")
    if it.get("story"):
        s += " story"
    return s


def _odds_unread(it: dict) -> bool:
    """DC 0 is no check the game shows: the roll fields weren't filled when the item was read (live 12:4x,
    Shadowheart's window: 16 of 20 items '0% DC0' next to 45% DC12 ones)."""
    return it.get("dc") == 0 and not it.get("chance")


def pockets_text(res: dict, h: Callable[[dict], str]) -> str:
    """'POCKETS (chance to steal, best odds first): e40 Gold x12 81% DC8 | e41 Dagger 2g 54% DC12 | …'."""
    items = [it for it in (res or {}).get("items") or [] if isinstance(it, dict)]
    if not items:
        return "POCKETS: nothing to steal (pickpocket(npc, leave=true) closes the window)"
    items.sort(key=lambda it: (-(it.get("chance") if isinstance(it.get("chance"), (int, float)) and not _odds_unread(it)
                                 else -1), -_price(it)))
    more = (res or {}).get("more")
    return ("POCKETS (the game's chance to steal, DC; best odds first): " + " | ".join(_pocket(it, h) for it in items)
            + (f" | +{more} more" if more else "")
            + ' → pickpocket(npc, take="<name or id>"); pickpocket(npc, leave=true) leaves with nothing')


def steal_text(res: dict, h: Callable[[dict], str]) -> str:
    it = (res or {}).get("item") or {}
    what = _pocket({k: v for k, v in it.items() if k not in ("chance", "dc")}, h)
    if res.get("pending"):
        return f"stole? {what}: the roll had not finished after 5 s (get_inventory shows if it came)"
    roll = f"rolled {res.get('rolled')} vs DC {res.get('dc')}" if res.get("rolled") is not None else ""
    odds = f"{res.get('chance')}% chance" if res.get("chance") is not None else ""
    tail = ", ".join(x for x in (odds, roll) if x)
    if res.get("success"):
        return f"stole {what}" + (f" ({tail})" if tail else "")
    return f"CAUGHT? the steal of {what} failed" + (f" ({tail})" if tail else "") + ": the owner may notice (crime)"

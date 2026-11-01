"""Item and rest tools: feed_gale, insert_item, manage_inventory, equip_best, short_rest, long_rest.
"""

from __future__ import annotations

import time
from typing import Any, Callable

from .. import actq, economy, party
from ..game import GameError, JobResult
from . import core, galefeed, overview, plumbing, reporting, travel, world_tools

# ---------------------------------------------------------------------------
# act: items, rest, progression
# ---------------------------------------------------------------------------


def _item_uuid(item: str, who: str | None = None, worn: bool = False) -> str:
    """_uuid, except that a name shared by identical party items (same name, rarity, slot, stats) picks one
    instead of failing 'ambiguous' (S027: equip "Simple Robe" → ambiguous e14,e148,e165,e169, batch stopped).
    Prefers the item in `who`'s bag, unworn (worn=True for unequip: a worn one)."""
    try:
        return _party_item_uuid(item)
    except ValueError as e:
        if "ambiguous" not in str(e):
            raise
        inv = core.game.try_rpc("state.inventory", {"expand": True}) or {}
        plumbing._remember_names(inv)
        rows = [it for it in inv.get("items") or [] if isinstance(it, dict) and it.get("uuid")
                and str(it.get("name") or "").strip().lower() == str(item).strip().lower()]
        # S057 07:49: "'Potion of Superior Healing' is ambiguous: e239, e460" with one in the bags and the other a
        # trader's stock seen earlier: the bags hold the only one this tool can use.
        if len(rows) == 1:
            return rows[0]["uuid"]
        key = lambda it: (it.get("rarity"), it.get("slot_type"), it.get("ac"), str(it.get("dmg")))  # noqa: E731
        if len(rows) < 2 or len({key(it) for it in rows}) != 1:
            raise
        owner = plumbing._who(who)
        rows.sort(key=lambda it: (bool(it.get("equipped_slot")) != worn, it.get("owner") != owner))
        return rows[0]["uuid"]


def _party_item_uuid(item: str) -> str:
    """plumbing._uuid, plus one party inventory read (bags and worn gear) for a name a fresh bridge has not learnt
    (D8: feed_gale("Gloves of Power"), worn, was 'unknown target' until a get_inventory)."""
    try:
        return plumbing._uuid(item)
    except ValueError as e:
        if "unknown target" not in str(e):
            raise
        plumbing._remember_names(core.game.try_rpc("state.inventory", {"expand": True}))
        return core.H.resolve(item)


@core.mcp.tool()
@reporting._guard
def feed_gale(item: str) -> str:
    """Give Gale a magic item to absorb, in the donation window his dialogue opens ('Give Gale a magic artefact'): selects the item, donates it and closes the window (Gale absorbs it then). item: name or handle from get_inventory (in a party bag or worn: the window offers both)."""
    return galefeed.feed(plumbing._uuid(core.H.get(_item_uuid(item))))


def _insert_job(args: dict, wait_s: float = 15) -> JobResult:
    """ui.insert as a JobResult; its refusals (no panel open) come back as failed, not raised."""
    try:
        return core.game.run_job("ui.insert", args, context="client", wait_s=wait_s)
    except Exception as e:
        return JobResult("failed", error=str(e))


def _insert_listing(res: dict) -> str:
    slots = ", ".join(x if x else "empty" for x in res.get("slots") or []) or "?"
    rows = []
    for it in res.get("items") or []:
        u = it.get("uuid")
        h = core.H.get(u, it.get("name")) if isinstance(u, str) and u else "?"
        rows.append(f"{h} {it.get('name') or '?'}" + (" (in the slot)" if it.get("selected") else ""))
    return (f"INSERT panel {res.get('panel') or '?'}: slot(s) {slots}; state {res.get('state') or '?'}\n"
            + ("items it can take: " + "; ".join(rows[:20]) + (f" (+{len(rows) - 20} more)" if len(rows) > 20 else "")
               if rows else "no items listed (the panel shows the selected character's bag)"))


def _opened_dialogue(out: str) -> bool:
    """The interact opened a conversation: its own text says so, or one is on screen now (live3 run 1, the
    Nautiloid's console e99: the dialogue came up after the interact returned, so insert_item said "not every
    pedestal takes items" while the conversation offered "Insert the rune into the socket")."""
    return ("\nDIALOGUE " in "\n" + (out or "") or "dialogue_started" in (out or "")
            or macro_tools._dialogue_needs_answer())


def _insert_poll(out: str, wait_s: float = 4.0) -> JobResult:
    """The panel shows a moment after the interact; a conversation instead of it ends the wait at once."""
    deadline = time.monotonic() + wait_s
    while True:
        r = _insert_job({})
        if (r.status != "failed" or "no Insert Item panel" not in str(r.error) or _opened_dialogue(out)
                or time.monotonic() > deadline):
            return r
        time.sleep(0.25)


def _colocated_usables(target: str, within_m: float = 1.5) -> list[tuple[str, str]]:
    """Other usable objects in sight standing where the target stands (uuid, name), nearest first."""
    near = core.game.try_rpc("state.nearby", {"radius": 20, "kinds": ["usable", "container"]}) or {}
    plumbing._remember_names(near)
    rows = [it for it in near.get("items") or [] if isinstance(it, dict) and isinstance(it.get("pos"), list)]
    me = next((it for it in rows if it.get("uuid") == target), None)
    if not me:
        return []
    def d(it: dict) -> float:
        return sum((float(a) - float(b)) ** 2 for a, b in zip(it["pos"][:3], me["pos"][:3])) ** 0.5
    return [(it["uuid"], str(it.get("name") or "?")) for it in sorted(rows, key=d)
            if it is not me and d(it) <= within_m]


@core.mcp.tool()
@reporting._guard
def insert_item(item: str | None = None, into: str | None = None, who: str | None = None) -> str:
    """Put an item into an "Insert Item" panel (pedestal, plaque, socket: what a player drags into the slot) and press Insert. into: the object (opens its panel with interact when none is open; walks there). item: name or handle; without item it opens the panel and lists the slot and the items it can take. who: who opens/inserts (default: the item's carrier, else the active character). Some story pedestals punish a wrong item: choose by the riddle/notes."""
    t0 = time.monotonic()
    args = {"item": item, "into": into, "who": who}
    u_item = plumbing._uuid(core.H.get(_item_uuid(item))) if item else None
    char = plumbing._who_uuid(who) if who else None
    if char is None and u_item:
        inv = core.game.try_rpc("state.inventory", {}) or {}
        plumbing._remember_names(inv)
        row = next((it for it in inv.get("items") or [] if isinstance(it, dict) and it.get("uuid") == u_item), None)
        char = row.get("owner") if row else None
    char = char or plumbing._active()
    note = ""
    r = _insert_job({})
    if r.status == "failed" and "no Insert Item panel" in str(r.error):
        if not into:
            return reporting._finish("insert_item", args, "insert_item: FAILED — no Insert Item panel is open: pass into=<the pedestal/socket> (interact opens it)", t0)
        if not uitools._hud_has(char):
            # S037 20:15-20:17: who=Shadowheart while Claude was controlled: 3 interacts "used … success", no panel;
            # selecting her and clicking opened it. The client shows the panel only to the controlled character
            # (as pickpocket's window), so take control first, as a player does.
            if core.game.run_job("act.select", {"char": char}, wait_s=5).status == "done":
                note = f"took control of {overview._name_of(char)} (the panel shows for the controlled character)"
        out = plumbing._act("interact", "act.interact", {"char": char, "target": plumbing._uuid(into)}, wait_s=40)
        walked = travel._walk_first("interact", out, char, who, into, actq.Step("interact", {"target": into}))
        if walked:  # far / out of sight: the walk runs, the interact is queued for the arrival
            return reporting._finish("insert_item", args, walked + "\ncall insert_item again when the panel is open", t0)
        r = _insert_poll(out)
        others = _colocated_usables(plumbing._uuid(into)) if r.status == "failed" and "no Insert Item panel" in str(r.error) else []
        if others and not _opened_dialogue(out):
            # S037 20:18-20:20: the Pedestal of Reckoning only talks; the gem slot is the Ancient Altar at the same
            # spot (a hand click on it opened the panel). Try what stands there, as a player clicks the other part.
            h, nm = others[0]
            out2 = plumbing._act("interact", "act.interact", {"char": char, "target": h}, wait_s=40)
            r = _insert_poll(out2)
            if r.status == "done":
                into = core.H.get(h)
                note = "; ".join(x for x in (note, f"the slot is on {core.H.get(h)} {nm} (at the same spot)") if x)
            else:
                out = out2 if _opened_dialogue(out2) else out
        if r.status == "failed" and "no Insert Item panel" not in str(r.error):
            return reporting._finish("insert_item", args, f"insert_item: FAILED — {r.error}", t0)
        if r.status == "failed":
            alt = "; ".join(f"{core.H.get(h)} {nm}" for h, nm in others)
            if _opened_dialogue(out):
                body = (f"insert_item: FAILED — no Insert Item panel: using it opened a conversation instead\n{out}"
                        + (f"\nthe slot may be on what stands at the same spot ({alt}): end the conversation, then "
                           f"insert_item(into={core.H.get(others[0][0])})" if others else ""))
            else:
                body = (f"insert_item: FAILED — no Insert Item panel opened ({out.splitlines()[0] if out else 'interact'})"
                        + (f"; tried {core.H.get(others[0][0])} {others[0][1]} at the same spot too" if others else "")
                        + "; not every pedestal takes items: look at it (get_nearby / screenshot)")
            return reporting._finish("insert_item", args, body, t0)
        note = "; ".join(x for x in (note, f"opened the panel by {overview._name_of(char)}") if x)
    elif r.status == "failed":
        return reporting._finish("insert_item", args, f"insert_item: FAILED — {r.error}", t0)
    listing = dict(r.result) if isinstance(r.result, dict) else {}
    if not listing.get("panel") and into:  # the panel's own VM carries no name (live 17:5x)
        listing["panel"] = overview._name_of(plumbing._uuid(into))
    if not u_item:
        return reporting._finish("insert_item", args, "insert_item: done — " + (note + "\n" if note else "") + _insert_listing(listing)
                       + "\ninsert_item(item=<handle>) puts one in and presses Insert", t0)
    r = _insert_job({"item": u_item})
    if r.status != "done":
        body = f"insert_item: FAILED — {r.error or r.status}\n" + _insert_listing(listing)
        return reporting._finish("insert_item", args, body, t0)
    res = r.result or {}
    name = res.get("item") or res.get("inserted") or res.get("placed") or overview._name_of(u_item)
    if res.get("placed") and not res.get("inserted"):
        # S037 20:23: the Verge altar's 3 gem slots; the first two gems read FAILED though each sat in its slot.
        n, k = res.get("slots_total") or 0, res.get("filled") or 0
        body = (f"insert_item: done — put {name} in the slot ({k} of {n} filled; state {res.get('state')}): the panel "
                f"waits for {n - k} more item(s): insert_item(item=<next>) fills the next slot, Insert presses itself")
    elif res.get("state") == "Fail":
        why = res.get("fail_reason") or "no reason given"
        body = f"insert_item: FAILED — the panel refused {name} ({why}); it is still open"
        if why == "Interrupted":
            # Live 2026-10-10 17:5x (S031-pre-riddle): the game answers Interrupted to a real mouse drag + Insert click
            # too, the same game-side block as long_rest's missing ready check. Retrying or hand clicks won't help.
            body += ("\nthe game interrupted the insert (a real drag and click get the same answer now): don't retry "
                     "by hand; note it and play on elsewhere")
    else:
        # S037 20:17/20:20: "the panel is still open: ESCAPE closes it" after a story insert that closed it (the
        # pedestal vanished) → the ESCAPE was REFUSED. Look once before saying so.
        closed = res.get("closed")
        if not closed:
            time.sleep(0.3)
            again = _insert_job({}, wait_s=3)
            closed = again.status == "failed" and "no Insert Item panel" in str(again.error)
        body = f"insert_item: done — inserted {name} into {listing.get('panel') or 'the panel'}" + (
            " (panel closed)" if closed else f" (state {res.get('state')}; the panel is still open: ESCAPE closes it)")
    return reporting._finish("insert_item", args, body + ("\n" + note if note else ""), t0)


@core.mcp.tool()
@reporting._guard
def manage_inventory(op: str, item: str, who: str | None = None, to: str | None = None) -> str:
    """op: equip | unequip | use (drink/read/eat) | give (to another party member: to=name) | camp (send to camp chest; item="junk" sends all junk at once: what trade sell=junk would sell)."""
    o = op.lower()
    if o in ("equip", "unequip", "use", "give") or (o == "camp" and item.strip().lower() != "junk"):
        item = core.H.get(_item_uuid(item, who if o != "give" else None, worn=o == "unequip"))
    if o == "equip":
        return plumbing._act("manage_inventory", "act.equip", {"char": plumbing._who(who), "item": plumbing._uuid(item)})
    if o == "unequip":
        return plumbing._act("manage_inventory", "act.unequip", {"char": plumbing._who(who), "item": plumbing._uuid(item)})
    if o == "use":
        # Osiris Use on a carried item. In combat only a potion, on the drinker's turn: the mod checks and
        # charges the item's own cost (a bonus action), as the hotbar would.
        return plumbing._act("manage_inventory", "act.interact", {"char": plumbing._who(who), "target": plumbing._uuid(item)})
    if o == "give":
        targs = {"item": plumbing._uuid(item), "to": plumbing._uuid(to) if to else None}
        out = plumbing._act("manage_inventory", "act.transfer", targs)
        if "unequip it first" in out.split("\n", 1)[0]:
            # S028 15:51 (Spellcrux Amulet, Karlach → Gale): a player drags a worn item onto another portrait;
            # here the refusal cost a 3-step batch (unequip, give, equip). Take it off its wearer, then give.
            row = next((it for it in (core.game.try_rpc("state.inventory", {}) or {}).get("items") or []
                        if isinstance(it, dict) and it.get("uuid") == targs["item"] and it.get("equipped_slot")), None)
            if row and row.get("owner"):
                r = core.game.run_job("act.unequip", {"char": row["owner"], "item": targs["item"]}, wait_s=10)
                if r.status != "done":
                    return out + f"\n(unequipping it from {overview._name_of(row['owner'])} failed: {plumbing._job_text('unequip', r)})"
                out = plumbing._act("manage_inventory", "act.transfer", targs)
                head, _, rest = out.partition("\n")
                out = head + f" (unequipped from {overview._name_of(row['owner'])} first)" + ("\n" + rest if rest else "")
        return out
    if o == "camp":
        if item.strip().lower() == "junk":
            return _stash_junk(who)
        return plumbing._act("manage_inventory", "act.transfer", {"item": plumbing._uuid(item), "to": "camp"})
    raise ValueError("op must be equip|unequip|use|give|camp")


def _keep_set(keep: str) -> set[str]:
    return {plumbing._uuid(k.strip()) for k in (keep or "").split(",") if k.strip()}


def _stash_junk(who: str | None, keep: str = "") -> str:
    """manage_inventory(op="camp", item="junk"): send every junk item (economy.is_junk) to the camp chest, one
    engine transfer each (S023 did this by hand: inventory screen, right-click, 6 screenshots)."""
    t0 = time.monotonic()
    inv = core.game.rpc("state.inventory", {"uuid": plumbing._who_uuid(who)} if who else {})
    plumbing._remember_names(inv)
    items = economy.junk(inv, _keep_set(keep))
    sent, failed = [], []
    for it in items:
        r = core.game.run_job("act.transfer", {"item": it["uuid"], "to": "camp"}, wait_s=10)
        (sent if r.status == "done" else failed).append(f"{core.H.get(it['uuid'])} {it.get('name')}"
                                                        + ("" if r.status == "done" else f" ({r.error})"))
        if r.status != "done" and len(failed) >= 3:
            break  # the same refusal (combat, camp unavailable) would repeat for every item
    kg = sum((i.get("weight") or 0) * (i.get("count") or 1) for i in items if f"{core.H.get(i['uuid'])} {i.get('name')}" in sent)
    body = f"manage_inventory: {'done' if sent and not failed else 'FAILED' if not sent else 'partly done'} — sent {len(sent)} junk items to camp ({kg:.1f} kg)"
    if not items:
        body = "manage_inventory: done — no junk to send (equipped, story, consumables, food, keys and magic gear are kept)"
    if sent:
        body += ": " + ", ".join(sent[:20]) + (" …" if len(sent) > 20 else "")
    if failed:
        body += "\nFAILED: " + "; ".join(failed)
    return reporting._finish("manage_inventory", {"op": "camp", "item": "junk"}, body, t0)


@core.mcp.tool()
@reporting._guard
def equip_best(who: str | None = None) -> str:
    """Equip the better gear sitting in party bags (out of combat): for each member (or just who), every item that beats what they wear in that slot (armour/shield AC, weapon damage/enchantment, else rarity) and that they are proficient with; moved from another member's bag first. get_inventory(filter="gear") lists the same without equipping."""
    t0 = time.monotonic()
    if (core.game.try_rpc("state.status") or {}).get("in_combat"):
        # S034 19:15: called as a Displacer Beast ambush began; every give failed "… (use the UI)", which invites a
        # hand click. In combat a swap costs an action and gives are refused: say so, do nothing.
        return reporting._finish("equip_best", {"who": who}, "equip_best: NOT NOW — in combat (gear swaps cost actions and "
                       "items can't change hands mid-fight). Call it again once the fight ends; nothing was moved.", t0)
    inv = core.game.rpc("state.inventory", {})
    plumbing._remember_names(inv)
    done, failed = [], []
    lp = economy.lantern_plan(inv)
    if lp and (not who or plumbing._who_uuid(who) in (lp["frm"], lp["to"])):
        # The lantern's aura works from any holder: the caster carries the light, the fighter gets a weapon
        # (S025-S027 Claude fought with it). Unequip → give → equip: the gap is ~1 s.
        tag = f"{overview._name_of(lp['to'])}: {core.H.get(lp['lantern'])} {lp['name']} (light for the party; was {overview._name_of(lp['frm'])}'s main hand)"
        for op, args in (("act.unequip", {"char": lp["frm"], "item": lp["lantern"]}),
                         ("act.transfer", {"item": lp["lantern"], "to": lp["to"]}),
                         ("act.equip", {"char": lp["to"], "item": lp["lantern"]})):
            r = core.game.run_job(op, args, wait_s=10)
            if r.status != "done":
                failed.append(f"{tag}: {op.split('.')[1]} failed: {r.error}")
                if op == "act.equip":  # never leave it in a bag: the aura needs it held
                    core.game.run_job("act.transfer", {"item": lp["lantern"], "to": lp["frm"]}, wait_s=10)
                if op != "act.unequip":
                    core.game.run_job("act.equip", {"char": lp["frm"], "item": lp["lantern"]}, wait_s=10)
                break
        else:
            done.append(tag)
            inv = core.game.rpc("state.inventory", {})
            plumbing._remember_names(inv)
    ups = economy.upgrades(inv)
    if who:
        ups = [u for u in ups if u["who"] == plumbing._who_uuid(who)]
    if not ups and not done and not failed:
        hands = economy.main_hands(inv, overview._name_of)
        return reporting._finish("equip_best", {"who": who}, "equip_best: done — nothing better in the bags"
                       + (f"\n{hands}" if hands else ""), t0)
    for u in ups:
        tag = f"{overview._name_of(u['who'])}: {core.H.get(u['uuid'])} {u['name']} ({u['why']})"
        if u["holder"] != u["who"]:
            r = core.game.run_job("act.transfer", {"item": u["uuid"], "to": u["who"]}, wait_s=10)
            if r.status != "done":
                failed.append(f"{tag}: give failed: {r.error}")
                continue
        r = core.game.run_job("act.equip", {"char": u["who"], "item": u["uuid"]}, wait_s=10)
        (done if r.status == "done" else failed).append(tag if r.status == "done" else f"{tag}: {r.error}")
    body = f"equip_best: {'done' if done and not failed else 'FAILED' if not done else 'partly done'}"
    if done:
        body += " — equipped " + "; ".join(done)
    if failed:
        body += "\nFAILED: " + "; ".join(failed)
    hands = economy.main_hands(core.game.try_rpc("state.inventory", {}) or inv, overview._name_of)   # after the swaps
    if hands:
        body += "\n" + hands
    return reporting._finish("equip_best", {"who": who}, body, t0)


@core.mcp.tool()
@reporting._guard
def short_rest() -> str:
    """Short rest (2 per long rest): restores some HP and short-rest resources. The result shows HP before→after per member and the short rests left."""
    # live N6 01:53:39: short_rest with a trade screen left open: the press did nothing under it and the rest waited
    # 15 s ("TIMEOUT — timed out"). A player closes the trade window first; a conversation on screen is said as such.
    note = "(closed the trade window first)\n" if plumbing._close_left_trade() else ""
    screen = uitools._ui_screen()[0]
    if screen in ("trade", "dialogue"):
        why = "the trade screen is open and didn't close" if screen == "trade" else "a conversation is on screen"
        return note + "short_rest: FAILED — not pressed: " + why
    # launch-v1 act1_level_3 r5: short_rest 2 s after a fight started; r7 x2: Tav selected (screen=game) while
    # Shadowheart was in a conversation. The selected member's hotbar still offered the rest, the game ignored the
    # press and the job waited out its 15 s: "TIMEOUT — timed out". nav.activity covers the whole party.
    act = core.game.try_rpc("nav.activity")
    if isinstance(act, dict) and (act.get("in_combat") or act.get("in_dialogue")):
        why = "in combat" if act.get("in_combat") else \
            "a party member is in a conversation (finish it first; it may be one you haven't selected)"
        return note + "short_rest: FAILED — not pressed: " + why
    # live N4, N7, N11 (C1 replays): a short rest during a revived member's stand-up (act.end_standup) left them in an
    # unfinished action for good, every later walk of theirs travelled=0; N10's stand-up with nothing else running ended
    up = (core.game.try_rpc("act.standups") or {}).get("running") or []
    if up:
        who = ", ".join(u.get("name") or overview._name_of(u.get("uuid", "")) for u in up)
        return note + f"short_rest: FAILED — not pressed: {who} is still standing up from the revive"
    before = core.game.try_rpc("state.status")
    try:
        out = note + world_tools._box_act("short_rest", "ui.short_rest", {}, wait_s=20)
    except GameError as e:
        # live N8 1a: the mod's refusal ("no short rests left") came back without the note (the window was closed)
        if note:
            raise GameError(f"{e} {note.strip()}") from e
        raise
    res = reporting._batch.get("result")
    if isinstance(res, dict) and isinstance(res.get("short_rests_left"), int):
        overview.ALERTS.short_rests = res["short_rests_left"]
    if out.startswith("short_rest: done") or "short_rest: done" in out:
        out += "\n" + economy.short_rest_text(before, core.game.try_rpc("state.status"), res)
    return out


@core.mcp.tool()
@reporting._guard
def long_rest(confirm: bool = True, partial: bool = False) -> str:
    """Long rest at camp (go there with camp first): consumes 40 camp supplies, restores everything, may trigger camp scenes (handle them as dialogues). The game's questions on the way ("End the day?", "someone still wants to talk, rest anyway?") are answered Yes and listed in the result; confirm=false stops at the first readable one instead (then confirm(...)); the unreadable "[ForceUpdate]" box (the game's own End-the-Day check) is answered either way. With fewer than 40 supplies it stops before resting (a partial rest restores only half HP and slots): get food first, or partial=true to rest anyway."""
    args: dict[str, Any] = {} if confirm else {"confirm": False}
    if partial:
        args["partial"] = True
    refused = _rest_blocked()
    if refused:
        return refused
    # No blind re-press after 4 s (S030: requested → gathered → requested, then the engine said no ~10 s after the
    # first press: the re-press fell inside its answer window). The server's rest flow says whether the press
    # reached the game; only a press that didn't is repeated.
    args["repress"] = False
    t0 = time.monotonic()

    def since_ms() -> int:
        return int((time.monotonic() - t0) * 1000) + 1500

    out = world_tools._box_act("long_rest", "ui.long_rest", args, wait_s=90)
    _rested()
    if "rest screen didn't open" in out:
        flow = _rest_flow_answer(since=since_ms)
        if isinstance(flow, dict) and not flow.get("steps"):
            # the press never reached the story (S018: a press swallowed by a closing screen): press once more
            out = world_tools._box_act("long_rest", "ui.long_rest", args, wait_s=90)
            _rested()
            if "rest screen didn't open" not in out:
                return out
            flow = _rest_flow_answer(since=since_ms)
        if "end_day_ok" in (flow or {}).get("steps", []):
            # the game said yes after the job's 4 s window: the rest screen is coming, carry on in this call
            # without pressing again (a press at night is the story's "leave night mode")
            out = world_tools._box_act("long_rest", "ui.long_rest", {**args, "no_press": True}, wait_s=90)
            _rested()
            if "rest screen didn't open" not in out:
                return out
            flow = core.game.try_rpc("camp.rest_flow", {"since_ms": since_ms()})
        out += "\n" + _rest_flow_hint(flow)
    return out


LONG_REST_AT = [0.0]  # monotonic time of the last long rest (swap_party's post-rest settle)


def _rested() -> None:
    res = reporting._batch.get("result")
    overview.ALERTS.rested(res)
    if isinstance(res, dict) and res.get("rest") in ("full", "partial"):
        LONG_REST_AT[0] = time.monotonic()


def _rest_flow_answer(wait_s: float = 8.0, since: Callable[[], int] = lambda: 20000) -> dict | None:
    """S030: the engine answered the End-the-Day request ~10 s after the press, after ui.long_rest gave up.
    When the party gathered and no answer came yet, wait for it (instead of a second long_rest call)."""
    t_end = time.monotonic() + wait_s
    while True:
        flow = core.game.try_rpc("camp.rest_flow", {"since_ms": since()})
        steps = (flow.get("steps") or []) if isinstance(flow, dict) else []
        last_gather = max((i for i, x in enumerate(steps) if x == "gathered"), default=-1)
        answered = last_gather < 0 or any(_rest_step(x) not in _REST_FLOW_PENDING for x in steps[last_gather:])
        if answered or time.monotonic() >= t_end:
            return flow if isinstance(flow, dict) else None
        time.sleep(0.5)


# The engine's long-rest validation per party member (CanDoRest.LongRestErrorFlags, Events.lua camp.can_rest):
# the HUD greys the Long Rest button and its tooltip says why, so a player sees these reasons. S028-S030: 10
# "rest screen didn't open" failures, 8 s each, with screenshots, HUD clicks and a save reload to work around.
_REST_FLAG_TEXT = {
    "Combat": "in combat",
    "Dialog": "in a conversation (finish it first)",
    "FTB": "turn-based mode is on (set_party(action=\"turn_based_off\"))",
    "Script": "a story event blocks resting right now",
    "GlobalDisabled": "resting is disabled here",
    "DropInCCActive": "a character editor is open",
    "DownedOrDead": "downed or dead",
    "NotEnoughResources": "not enough camp supplies",
    "Location": "not at a place where you can rest",
}
# Refused before pressing anything (sure blockers); the others only explain a failure.
_REST_HARD_FLAGS = ("Combat", "Dialog", "FTB", "Script", "GlobalDisabled", "DropInCCActive")


def _rest_reasons(can: Any, flags: tuple[str, ...] | None = None) -> str:
    """'Karlach: turn-based mode is on (...); Gale: in combat' from camp.can_rest, limited to `flags`."""
    if not isinstance(can, dict):
        return ""
    parts = []
    for row in can.get("blocked") or []:
        fl = [str(f) for f in row.get("flags") or [] if flags is None or str(f) in flags]
        if fl:
            parts.append(f"{row.get('name') or '?'}: " + ", ".join(_REST_FLAG_TEXT.get(f, f) for f in fl))
    if (can.get("global_disabled") or 0) > 0 and (flags is None or "GlobalDisabled" in flags):
        parts.append("resting is disabled for now (story)")
    return "; ".join(parts)


def _dead_blockers(can: Any) -> list[str]:
    """Dead party members (not summons) that the game's rest check already flags before the press. S058 07:58: Wyll +
    Shadowheart dead at camp, both flagged Location → requested → gather_failed 4 s later, a quicksave, then a save
    reload by the agent. Refused before pressing; _rest_dead_note says what to do."""
    flagged = {str(r.get("name")) for r in (can.get("blocked") or []) if isinstance(r, dict) and r.get("flags")} \
        if isinstance(can, dict) else set()
    if not flagged:
        return []
    st = core.game.try_rpc("state.status")
    party = st.get("party") or [] if isinstance(st, dict) else []
    return [str(r.get("name")) for r in party if isinstance(r, dict) and r.get("dead") and not r.get("summon")
            and str(r.get("name")) in flagged]


def _rest_not_at_camp(can: Any) -> str:
    """A living member the rest check flags Location while the others aren't, and far from the camp group: not at
    camp. S065 12:01:49: Wyll (3863 m away, left behind by camp()) → 'rest screen didn't open' after 4 s, then
    regroup STOPPED; set_party select Wyll + camp() brought him and the rest went through. '' otherwise."""
    rows = [r for r in (can.get("blocked") or []) if isinstance(r, dict)] if isinstance(can, dict) else []
    loc = {str(r.get("name")) for r in rows if "Location" in (r.get("flags") or [])}
    if not loc:
        return ""
    st = core.game.try_rpc("state.status") or {}
    behind = [r for r in party.camp_behind(st) if str(r.get("name")) in loc]
    if not behind:
        return ""
    fix = "; ".join(f"set_party select {overview._name_of(r['uuid'])}, then camp()" for r in behind)
    return (f"long_rest: FAILED — not pressed: not everyone is at camp: {party.behind_text(behind, queueing._label)} (the "
            f"game's rest check: not at a place where you can rest). Bring them first: {fix} (S065 12:02: that "
            "brought Wyll and the rest went through), then long_rest.")


def _woke_here() -> str:
    """Live 2026-10-24 (T3, T3b, PROBE-DONATE x2): a long_rest at camp after a finished long rest: the press reached the
    story (requested) and was never answered, every time with the camp story's DB_CAMP_JustWokeUp set (0 in every
    rest-flow record S030-S065); a retry a minute later the same, until a load. S072 22:01: the party left camp in
    between and the next rest went on. The game takes no second rest until the party has left camp. '' otherwise."""
    flow = core.game.try_rpc("camp.rest_flow", {"since_ms": 1})  # the story flags only, no steps
    story = flow.get("story") if isinstance(flow, dict) else None
    woke = story.get("DB_CAMP_JustWokeUp") if isinstance(story, dict) else None
    if not (isinstance(woke, int) and woke > 0):  # -1: the DB couldn't be read
        return ""
    return ("long_rest: FAILED — not pressed: the party woke up from a long rest at this camp and hasn't left it since; "
            "the game doesn't answer another rest request until it has. Leave camp (camp(action=\"leave\")), come "
            "back with camp(), then long_rest")


def _rest_blocked() -> str:
    can = core.game.try_rpc("camp.can_rest")
    core.METRICS.write({"type": "can_rest", "t": round(time.time(), 3), "can": can})
    dead = _dead_blockers(can)
    if dead:
        core.METRICS.write({"type": "can_rest", "t": round(time.time(), 3), "can": can, "refused": True, "dead": dead})
        return ("long_rest: FAILED — not pressed: the game's rest check blocks on dead party members "
                f"({_rest_reasons(can)})" + _rest_dead_note(can))
    away = _rest_not_at_camp(can)
    if away:
        core.METRICS.write({"type": "can_rest", "t": round(time.time(), 3), "can": can, "refused": True, "away": away})
        return away
    woke = _woke_here()
    if woke:
        core.METRICS.write({"type": "can_rest", "t": round(time.time(), 3), "can": can, "refused": True, "woke": True})
        return woke
    if not _rest_reasons(can, _REST_HARD_FLAGS):
        return ""
    time.sleep(0.5)  # a flag set for a moment (a teleport into camp, a closing conversation) must not refuse a rest
    can = core.game.try_rpc("camp.can_rest")
    why = _rest_reasons(can, _REST_HARD_FLAGS)
    if not why:
        return ""
    core.METRICS.write({"type": "can_rest", "t": round(time.time(), 3), "can": can, "refused": True})
    return f"long_rest: FAILED — the game doesn't allow a long rest now (the Long Rest button is greyed out): {why}"


# S028 15:52-15:59 x5 + S029 16:04 x2: "rest screen didn't open" with nothing on screen, no box; screenshots and HUD
# clicks after. The camp's End-the-Day handshake (GLO_Camp.txt) runs on the server: the mod pushes each step
# (Events.lua rest_flow) and keeps the last ones; say where the request died.
_REST_FLOW_TEXT = {
    "": "the game never registered the rest request (the button press did not reach it)",
    "requested": "the game took the rest request but never answered it (a stuck camp state: a conversation, a fight "
                 "or loading a save clears it; Larian bug GUS-150045)",
    "gather_failed": "the game refused to gather the party at camp (someone can't come: busy, in a conversation or "
                     "far away)",
    "gathered": "the party gathered but the game didn't go on to end the day (S030: it answered no ~10 s later)",
    "end_day_failed": "the game refused to end the day after gathering the party (its own rest check; reloading a "
                      "save didn't help in S030)",
    "end_day_ok": "the game started the night: the rest screen should follow (call long_rest again)",
    "rest_start_failed": "the game could not start the rest",
    "rest_cancelled": "the rest was cancelled",
    "rest_denied": "the game denied the rest",
    "ready_failed": "the game's ready check (the \"End the day?\" question) failed",
}
# Steps that are not the game's final answer to the request (a ready check runs between gathered and the answer).
_REST_FLOW_PENDING = ("requested", "gathered", "ready_ok", "rest_waiting")


def _rest_step(step: str) -> str:
    return step.split(":", 1)[0]


# The ready check's engine id (Events.lua keeps it after the step) is no player-visible name: S044 23:50:48 look showed
# `ready_ok:MOO_Assault_ReadyCheck` (fairness LEAK). The agent reads what the check was for, in words.
_READY_CHECK_LABELS = {"ReadyCheckContent_GoToNight": "end the day", "WaypointTravel_RegionSwap": "travel to another region"}


def _ready_label(rid: str) -> str:
    return _READY_CHECK_LABELS.get(rid, "a story ready check" if rid else "the ready check")


def _plain_step(step: str) -> str:
    """A rest_flow step as the agent sees it: the engine id after ':' becomes words (ready checks) or goes."""
    key, _, rid = step.partition(":")
    return f"{key} ({_ready_label(rid)})" if key in ("ready_ok", "ready_failed") and rid else key


def _rest_flow_hint(flow: Any = None) -> str:
    if flow is None:
        flow = core.game.try_rpc("camp.rest_flow", {"since_ms": 20000})
    if not isinstance(flow, dict):
        return ""
    steps = [str(x) for x in flow.get("steps") or []]
    core.METRICS.write({"type": "rest_flow", "t": round(time.time(), 3), "steps": steps, "step_ms": flow.get("step_ms"),
                   "story": flow.get("story"), "can_rest": flow.get("can_rest")})
    # The furthest step says most: a re-press after "gathered" is swallowed by the story (DB_GoingToCamp), so the
    # last step alone misled S030 into a save reload ("requested: a stuck camp state ... loading a save clears it").
    answers = [x for x in steps if _rest_step(x) not in _REST_FLOW_PENDING]
    last = answers[-1] if answers else "gathered" if "gathered" in steps else steps[-1] if steps else ""
    key = _rest_step(last)
    text = _REST_FLOW_TEXT.get(key, key) + (f" ({_ready_label(last.split(':', 1)[1])})"
                                            if ":" in last and key in ("ready_ok", "ready_failed") else "")
    if key == "gathered" and not any(_rest_step(x) in ("ready_ok", "ready_failed") for x in steps):
        text += "; no ready-check result either (the \"End the day?\" question never showed or was never answered)"
    out = f"REST FLOW {' → '.join(_plain_step(x) for x in steps) or 'nothing'}: {text}"
    why = _rest_reasons(flow.get("can_rest"))
    return out + (f"\nthe game's rest check: {why}" if why else "") + _rest_dead_note(flow.get("can_rest"))


def _rest_dead_note(can: Any) -> str:
    """S058 07:58: long_rest(partial) failed with gather_failed and 'Wyll: not at a place where you can rest;
    Shadowheart: …' — both were dead (the party member's portrait shows it); the hint said 'busy, in a conversation
    or far away' and the agent spent 45 s guessing. Name the dead members the rest check blocks on."""
    st = core.game.try_rpc("state.status") or {}
    dead = [str(r.get("name") or "?") for r in st.get("party") or []
            if isinstance(r, dict) and r.get("dead") and not r.get("summon")]
    if not dead:
        return ""
    blocked = {str(r.get("name")) for r in (can or {}).get("blocked") or [] if isinstance(r, dict)} \
        if isinstance(can, dict) else set()
    hit = [n for n in dead if n in blocked] or dead
    return (f"\nDEAD: {', '.join(hit)} — the game won't gather a dead party member at camp, so the rest fails on "
            "them: revive them first (Revivify or a Scroll of Revivify aimed at the character, or Withers at camp), "
            "or swap them out of the party (swap_party)")


# The next module (and so the rest, in file order) loads after this one; later ones used here.
# isort: off
from . import (  # noqa: E402, F401
    progression, uitools, queueing, macro_tools,
)

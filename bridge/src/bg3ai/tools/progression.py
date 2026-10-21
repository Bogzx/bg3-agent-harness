"""Spells, level-up and trade tools.
"""

from __future__ import annotations

import json
import re
import threading
import time
from typing import Any

from .. import companions, economy, hotbar, leveling, render, spellprep
from ..game import GameError
from . import combat, core, inventory, overview, plumbing, reporting, travel, world_tools


@core.mcp.tool()
@reporting._guard
def prepare_spells(who: str | None = None, add: list[str] | str | None = None,
                   remove: list[str] | str | None = None) -> str:
    """Change a caster's PREPARED spells (clerics, paladins, druids, wizards), out of combat: only prepared spells cast. add/remove: spell names (or ids), a list or a comma string; removals happen first, so a swap fits the limit. It works the spellbook page like a player (opens it, clicks the spells, closes it) and ends with the prepared list. Refused in combat, over the limit (it names what is prepared), for a spell not in the character's book, and for classes that don't prepare (sorcerer/warlock/bard/ranger: level_up changes their spells). get_party and get_status show each caster's PREPARED list and limit."""
    t0 = time.monotonic()
    char = plumbing._who(who)
    waited = _wait_loading_out()
    text = spellprep.prepare(core.game, char, spellprep.names(add), spellprep.names(remove),
                             select=lambda u: combat._select_for_clicks(u, "the spellbook"))
    hotbar.forget_map(char)   # the hotbar's spells follow the book: no stale slot -> spell map for the next cast
    if waited:
        text += f"\n(waited {waited:.0f} s for the loading screen to end first)"
    return reporting._finish("prepare_spells", {"who": who, "add": add, "remove": remove}, queueing._label(text), t0)


LOADING_WAIT_S = 60.0  # _wait_loading_out: the longest a region load is waited out


def _wait_loading_out(max_s: float = LOADING_WAIT_S) -> float:
    """S070 21:00:08: prepare_spells pressed K 6 s after a region change (Return to Wyrm's Crossing) with the loading
    screen up: "the spellbook didn't open". A key or click during a load goes nowhere: wait until the loading screen
    is gone and the HUD is up. -> seconds waited (0 when nothing was loading)."""
    t0 = time.monotonic()
    if uitools._ui_screen()[0] != "loading":
        return 0.0
    while time.monotonic() - t0 < max_s and uitools._ui_screen()[0] in ("loading", "?"):
        time.sleep(0.5)
    travel._settle_hud(10)
    return time.monotonic() - t0


@core.mcp.tool()
@reporting._guard
def get_level_up_options(who: str | None = None) -> str:
    """What the level-up screen offers for a character (class/subclass, feats or ability points, spells, skills)."""
    t0 = time.monotonic()
    res = core.game.rpc("ui.level_up_options", {"uuid": plumbing._who(who)}, context="client")
    return reporting._finish("get_level_up_options", {"who": who}, render.compact(res, core.H), t0)


@core.mcp.tool()
@reporting._guard
def level_up(who: str | None = None, plan: str = "", out: str = "") -> str:
    """Level a character up in one call (out of combat). With no plan it uses your stored line for that character and level in memory/party-build.md ("- Gale L2: {...}"). plan: JSON like {"subclass":"Evocation","feat":"Ability Improvement","abilities":{"Strength":2},"spells":["Magic Missile","Shield"],"cantrips":[],"skills":[],"passives":["Agonizing Blast"]}; omitted fields take the game's defaults, except choices with no default: a subclass, spells/cantrips to learn, and passives (invocations, fighting styles). Then it fails and lists what can be picked; add them to the plan and call again.
    A companion waiting at camp is levelled too (every level it has waiting): it swaps in for `out` (default: the member it replaces best), levels, and swaps back."""
    st = core.game.try_rpc("state.status") or {}
    plumbing._remember_names(st)  # camp members' names (live 12:3x: the first call of a fresh bridge, level_up(who="Wyll"),
    uuid = plumbing._who(who)     # failed "unknown target 'Wyll'": only party names were known before a status read)
    camp = next((c for c in st.get("camp") or [] if isinstance(c, dict) and c.get("uuid") == uuid), None)
    if camp is not None:
        # S026: Wyll (camp, L4 pending since S021) → "character is not in the party portraits"; the advice to
        # swap by hand was never followed (S026-S027): do the swap, level, swap back in this call.
        return _level_camp_member(camp, plan, out, st)
    with _LEVEL_LOCK:  # one level-up page at a time: parallel calls (S010 x3, S024 x2) shared one UI
        return _level_up(uuid, plan)


def _level_camp_member(row: dict, plan: str, out: str, st: dict) -> str:
    name = str(row.get("name") or "?")
    if not row.get("level_up"):
        return f"level_up: nothing to do — {name} (camp) has no level up waiting"
    if st.get("in_combat") or st.get("in_dialogue"):
        return f"level_up: REFUSED — {name} is in camp: swapping them in needs no combat/conversation"
    party = [r for r in st.get("party") or [] if isinstance(r, dict) and not r.get("summon")]
    if out:
        outr = companions.find_row(party, out, plumbing._uuid)
    elif len(party) >= 4:
        live = [r for r in party if not r.get("dead")]
        outr = companions.swap_out(live, [r for r in live if not r.get("avatar")], row)
    else:
        outr = None
    outname = str((outr or {}).get("name") or "")
    if len(party) >= 4 and not outname:
        return f"level_up: FAILED — {name} is in camp and nobody can make room: level_up(who=\"{name}\", out=<companion>)"
    was_at_camp = overview._at_camp(core.game.try_rpc("ui.state", context="client"))
    steps = []
    sw = travel.swap_party(into=name, out=outname, leave_camp=False)
    if "swap_party: done" not in sw:
        return f"level_up: FAILED — {name} is in camp and the swap-in did not finish:\n{sw}"
    steps.append(f"swapped in {name}" + (f" for {outname}" if outname else ""))
    results, first = [], True
    for _ in range(6):  # every level waiting (a camp member may be 2 behind)
        row2 = next((r for r in leveling.pending(core.game.try_rpc("state.status")) if r["uuid"] == row.get("uuid")), None)
        if row2 is None:
            break
        with _LEVEL_LOCK:
            r = _level_up(str(row["uuid"]), plan if first else "")
        first = False
        results.append(f"L{row2['to']}: {r}")
        if not r.startswith("level_up: done"):
            break
    ok = bool(results) and all(": level_up: done" in r for r in results)
    if not ok and results and "plan." in results[-1]:
        # Live 11:14: Wyll L4 needs a cantrip + a spell the stored plan lacked. Swapping back now would make the
        # retry swap in again (~30 s): keep him in with the page up; the retry is the normal one-member call.
        keep = (f"{name} stays in the party" + (f" ({outname} at camp)" if outname else "") +
                f" with the level-up page open: call level_up(who=\"{name}\", plan=...) with the picks below"
                + (f", then swap_party(into=\"{outname}\", out=\"{name}\") to bring {outname} back" if outname else ""))
        ndone = sum(1 for r in results if ": level_up: done" in r)   # live 12:2x: L4 done, L5 lacked picks → said FAILED
        head = f"level_up: partly done ({ndone} of {len(results)} levels)" if ndone else "level_up: FAILED"
        return f"{head} — {name} (camp member): " + "; ".join(steps + [keep]) + "\n" + "\n".join(results)
    back = travel.swap_party(into=outname, out=name, leave_camp=False) if outname else ""
    if outname:
        steps.append(f"swapped back ({outname} in, {name} at camp)" if "swap_party: done" in back
                     else f"swap back FAILED: {back}")
    if not was_at_camp:
        rj = core.game.run_job("act.leave_camp", {}, wait_s=60)
        steps.append("left camp" if rj.status == "done" else "leave camp: " + plumbing._job_text("camp", rj))
    # Live 12:4x: Wyll L4 done (spells clicked, leveled_up), then L5 failed on the closing L4 page: the headline
    # said FAILED as if nothing had happened. A level that went through is said first.
    ndone = sum(1 for r in results if ": level_up: done" in r)
    head = "level_up: done" if ok else (f"level_up: partly done ({ndone} of {len(results)} levels)" if ndone else "level_up: FAILED")
    return f"{head} — {name} (camp member): " + "; ".join(steps) + "\n" + "\n".join(results or ["no level up waiting after the swap"])


_LEVEL_LOCK = threading.Lock()


def _level_up(uuid: str, plan: str) -> str:
    args: dict[str, Any] = {"uuid": uuid}
    note = ""
    if plan:
        args["plan"] = json.loads(plan)
    else:
        row = next((r for r in leveling.pending(core.game.try_rpc("state.status")) if r["uuid"] == uuid), None)
        stored = leveling.plan_for(core.MEMORY, row["name"], row["to"]) if row else None
        if stored is not None:
            args["plan"] = stored
            note = f"(plan: {row['name']} L{row['to']} from memory/party-build.md)"
        elif row:
            note = f"(no stored plan for {row['name']} L{row['to']}: game defaults)"
    # Select the member first, as a player clicks their portrait before Level Up. Unselected, the client finds the
    # member's portrait only by its HP/level fingerprint: S042 "cannot tell this character's portrait apart" ×2, and
    # S062 10:17-10:23 Shadowheart and Wyll (after Claude's finish): open waited 25 s ("closed_page=another
    # character's page"), then spell clicks found no button ×4, ~6 min until the agent selected each one by hand.
    prev = _select_for_level_up(uuid)
    out = _level_up_steps(uuid, args, note)
    if out.startswith("level_up: done"):
        overview._LEVEL_NUDGE.done(uuid)
        if prev and prev != uuid:  # give the control back; after a FAILED the member stays selected for the retry
            try:
                core.game.run_job("act.select", {"char": prev}, wait_s=5)
            except GameError:
                pass
    return out


def _select_for_level_up(uuid: str) -> str | None:
    """Make `uuid` the controlled character (no-op when it already is). -> the previously active member, or None."""
    try:  # best effort: an unselected member still levels when its portrait fingerprint is unique
        prev = plumbing._active()
        if not uitools._hud_has(uuid):
            core.game.run_job("act.select", {"char": uuid}, wait_s=5)
            time.sleep(0.2)  # the client's ClientControl follows the selection a frame or two later
        return prev
    except Exception:
        return None


def _level_up_steps(uuid: str, args: dict, note: str) -> str:
    out = plumbing._act("level_up", "ui.level_up", args, context="client", wait_s=60, note=note)
    if out.startswith("level_up: FAILED") and "portrait apart" in out:
        # Still ambiguous (the selection didn't land in time): select again and retry once.
        try:
            core.game.run_job("act.select", {"char": uuid}, wait_s=5)
            time.sleep(0.3)
        except GameError:
            pass
        out = plumbing._act("level_up", "ui.level_up", args, context="client", wait_s=60, note=note)
    if out.startswith("level_up: FAILED") and "choices: #1" in out:
        # The page had just opened and its list labels were not rendered yet (S009: "subclass: no 'Evocation';
        # choices: #1, #2, ..." twice, the same plan right after went through): once more with the page open.
        out = plumbing._act("level_up", "ui.level_up", args, context="client", wait_s=60, note=note)
    res = reporting._batch.get("result")
    if out.startswith("level_up: FAILED") and isinstance(res, dict) and res.get("needs_clicks") and world_tools._inp() is not None:
        # Spell picks are mouse clicks (leveling.click_spells); then the same plan again finishes the page.
        clicks = leveling.click_spells(lambda op, a: core.game.rpc(op, a, context="client"), world_tools._inp(), res["needs_clicks"])
        note = (note + " " if note else "") + f"(clicked spells: {', '.join(clicks['picked']) or 'none'}" + (
            f"; failed: {clicks['failed']})" if clicks["failed"] else ")")
        # keep_page: the picks live on this page; S032 the rerun closed the (frozen, Started) page and lost them.
        out = plumbing._act("level_up", "ui.level_up", dict(args, keep_page=bool(clicks["picked"])), context="client",
                   wait_s=60, note=note)
    return out


@core.mcp.tool()
@reporting._guard
def trade(npc: str = "", offer: str = "", request: str = "", confirm: bool = False, sell: str = "",
          keep: str = "", full: bool = False, donate: bool = False, count: int = 0) -> str:
    """Barter with a trader. Open with npc=<id> (talks to them and picks the conversation's trade answer, e.g. 'Let me see your wares.'; if none is offered yet it stops at the conversation), then list items via get_inventory; offer/request: comma lists of item ids to move; confirm=true to finish the deal (then the window closes). price is for the whole stack (price=156 count=3: 52 each; a stack moves whole). Wares marked supply=N are food (camp supplies per item; a long rest needs 40). A stack's "how many?" box is answered for you: the whole stack, or count=N.
    sell="junk": sell everything a player sells at every trader in one call (not equipped, story, consumables, food, keys/tools, magic gear, or keep=<comma ids>), up to the trader's gold; get_inventory(filter="junk") previews it.
    Opening or trade() with no args lists the wares grouped (food with supply, potions/scrolls, gear by rarity) and your best-priced items, capped per group; full=true lists everything. offer/request/confirm calls show the table and the balance only.
    confirm refuses an uneven table before the barter (a trader with no gold swaps items only); donate=true gives the offered items (or an overpay's difference) for nothing (answers the game's "Donate these items?"); automatic on a quest hand-over screen (a dialogue answer like "take them" opens a trade screen whose other side has no wares and no gold): trade(offer=<id>, confirm=true) gives it and closes the screen."""
    if sell:
        if sell.strip().lower() != "junk":
            raise ValueError('sell must be "junk" (or use offer=<ids> for specific items)')
        if npc:  # S028 15:51: trade(npc, sell="junk") failed "no trade screen is open": open it first
            opened = _open_trade(npc)
            if opened.startswith("trade: FAILED") or opened.startswith("FAILED") or "\nSTOPPED" in opened:
                return opened
            out = _sell_junk(keep)
            return (opened + "\n" + out) if opened else out
        scr = (core.game.try_rpc("ui.state", context="client") or {}).get("screen")
        if scr == "dialogue":  # a trader's conversation is up: pick its trade answer, as trade(npc) does
            opened = _open_trade("")
            if opened.startswith("trade: FAILED") or "\nSTOPPED" in opened:
                return opened
            out = _sell_junk(keep)
            return (opened + "\n" + out) if opened else out
        if scr not in (None, "trade"):
            return ('trade: REFUSED — sell="junk" needs a trader: trade(npc=<trader id>, sell="junk") talks to them, '
                    "opens the trade and sells in one call (no trade screen is open now)")
        return _sell_junk(keep)
    args: dict[str, Any] = {"confirm": confirm}
    if count:
        args["count"] = int(count)
    head = ""
    if npc:
        args["npc"] = plumbing._uuid(npc)
        opened = _open_trade(npc)
        if opened.startswith("trade: FAILED") or opened.startswith("FAILED") or "\nSTOPPED" in opened:
            return opened
        head = opened
    if offer:
        args["offer"] = [plumbing._uuid(i) for i in offer.split(",") if i.strip()]
    if request:
        args["request"] = [plumbing._uuid(i) for i in request.split(",") if i.strip()]
    handover = False
    if confirm and offer and not request:
        handover = economy.handover(core.game.try_rpc("ui.trade", {}, context="client"))
        donate = donate or handover
    if donate:
        args["donate"] = True
    _trade_view.update(listing=not (offer or request or confirm), full=full)
    if full:
        args["max"] = 500
    out = plumbing._act("trade", "ui.trade", args, context="client", wait_s=20)
    res = reporting._batch.get("result")
    if _trade_view["listing"]:
        for _ in range(TRADE_FILL_TRIES):
            if not _wares_unnamed(res):
                break
            time.sleep(TRADE_FILL_S)
            out = plumbing._act("trade", "ui.trade", args, context="client", wait_s=20)
            res = reporting._batch.get("result")
    if isinstance(res, dict) and res.get("donate_box"):
        out = _answer_donate(res, handover)
    elif confirm and isinstance(res, dict) and res.get("confirmed"):
        out += "\n" + _close_trade_after_deal()
    elif offer and "couldn't be moved" in out:
        notes = economy.equipped_offers(core.game.try_rpc("state.inventory", {}), args["offer"], overview._name_of)
        if notes:
            out += "\n" + "\n".join(notes)
    return (head + "\n" + out) if head else out


# L7 22:08 (Talli, trade() 0.2 s after choose_dialogue opened the screen): 24 wares "(no id) ?", "(1 item group(s)
# could not be named)", no food; 16 s later the same trader listed 60 named wares. The trader's side of the screen
# fills a few frames after it opens, and a half-filled side matches no inventory: the listing is read again.
TRADE_FILL_TRIES, TRADE_FILL_S = 4, 0.4


def _wares_unnamed(res: Any) -> bool:
    """The trader's side could not be matched yet: an unmatched group and wares without ids."""
    return isinstance(res, dict) and bool(res.get("unmatched")) and any(
        isinstance(w, dict) and not w.get("uuid") for w in res.get("wares") or [])


TRADE_CLOSE_S = 3.0  # the trader's goodbye line (a dialogue screen with no answers) plays this long at most


def _close_trade_after_deal() -> str:
    """S067 #125 / S068 #168: the window stayed up after trade(confirm=true) and the agent pressed ESCAPE by hand
    each time (~4 s a session). The deal is done: close the window as a player does and check the game is back."""
    if uitools._ui_screen()[0] != "trade":
        return ""
    if plumbing._close_left_trade(TRADE_CLOSE_S) and uitools._ui_screen()[0] == "game":
        return "(closed the trade window: back in the game; trade(npc=<trader>) opens it again)"
    u = core.game.try_rpc("ui.state", context="client") or {}
    return "\n".join([f"(closing the trade window: screen={u.get('screen') or '?'} after ESCAPE)"] + reporting._render_ui(u))


def _answer_donate(res: dict, handover: bool) -> str:
    """trade(donate=true) pressed Barter on a one-sided deal: answer the game's "Donate these items?" box with Yes
    (S028 15:48: the agent screenshotted, clicked Barter and confirmed by hand). On a hand-over screen, close it
    after (nothing else can happen there) so the conversation goes on."""
    gave = ", ".join(f"{_item_handle(m)} {m.get('name') or '?'}" for m in res.get("moved") or []
                     if isinstance(m, dict) and m.get("side") == "you") or "the offered items"
    box = None
    end = time.monotonic() + 3.0
    while time.monotonic() < end:
        box = core.game.try_rpc("ui.confirm", {"answer": "Yes", "dry": True}, context="client")
        if isinstance(box, dict) and box.get("buttons"):
            break
        box = None
        time.sleep(0.2)
    if box is None:
        return (f"trade: FAILED — gave {gave} with Barter, but no 'Donate these items?' box came up\n"
                + "\n".join(reporting._render_ui(core.game.try_rpc("ui.state", context="client") or {})))
    q = f"{box.get('title') or ''} {box.get('text') or ''}".strip()
    if not re.search(r"donat|give", q, re.I):
        return f"trade: STOPPED — the game asks: {q} {box.get('buttons')}; answer with confirm"
    c = world_tools._box_act("confirm", "ui.confirm", {"answer": "Yes"}, wait_s=8)
    if "confirm: done" not in c:
        return f"trade: FAILED — gave {gave} with Barter; the donate box: {c}"
    out = f"trade: done — gave {gave} (donated: answered the game's '{q}' with Yes)"
    if handover:
        time.sleep(0.3)
        if uitools._ui_screen()[0] == "trade":
            esc = uitools._press_escape()
            out += "; closed the hand-over screen" if "FAILED" not in esc and "REFUSED" not in esc else f"; close: {esc}"
        u = core.game.try_rpc("ui.state", context="client") or {}
        if u.get("screen") not in (None, "game", "trade"):
            out += "\n" + "\n".join(reporting._render_ui(u))
    return out


def _open_trade(npc: str) -> str:
    """trade(npc) with no trade screen up: talk to them and pick the conversation's trade answer, as a player does
    ('Let me see your wares.'). '' = it was already open; a short note when this call opened it; else the reason
    (FAILED / the conversation, for the agent to choose). The dialogue's Trade button (btnTrade → StartTrade) runs but
    opens nothing from bg3le (live 12:4x, Dammon): its entity parameter doesn't get through."""
    def ui() -> dict:
        return core.game.try_rpc("ui.state", context="client") or {}

    def wait(pred, s: float) -> dict:
        end, u = time.monotonic() + s, ui()
        while not pred(u) and time.monotonic() < end:
            time.sleep(0.2)
            u = ui()
        return u

    u = ui()
    if u.get("screen") == "trade":
        return ""
    talked = ""
    if u.get("screen") != "dialogue":
        if not npc:
            return "trade: FAILED — no conversation or trade screen is open: trade(npc=<trader id>) talks to them first"
        talked = travel.talk_to(npc)
        if "FAILED" in talked.split("\n", 1)[0]:
            return f"trade: FAILED — could not start the conversation: {talked}"
        u = wait(lambda x: bool((x.get("dialogue") or {}).get("options")) or x.get("screen") not in ("dialogue", "game"), 3.0)
    d = u.get("dialogue") or {}
    pick = economy.trade_answer(d.get("options") or [])
    if pick is None:
        opts = "; ".join(f"{o.get('i')}. {o.get('text')}" for o in d.get("options") or [] if isinstance(o, dict))
        return (f"trade: no trade answer in the conversation with {npc} yet ({opts or 'no answers shown'})\n"
                + (talked + "\n" if talked else "")
                + "STOPPED: pick an answer with choose_dialogue (the trade one may come later), then trade()")
    r = core.game.run_job("ui.dialogue_choose", {"i": pick["i"]}, context="client", wait_s=20)
    if r.status != "done":
        return f"trade: FAILED — picking '{pick.get('text')}': " + plumbing._job_text("choose_dialogue", r)
    u = wait(lambda x: x.get("screen") == "trade", 4.0)
    if u.get("screen") != "trade":
        return (f"trade: FAILED — picked '{pick.get('text')}' but no trade screen opened (screen={u.get('screen')})\n"
                + "\n".join(reporting._render_ui(u)))
    return f"(opened: {'talked to ' + npc + ', ' if npc else ''}picked '{pick.get('text')}')"


_trade_view: dict[str, bool] = {"listing": True, "full": False}  # how the next trade result is rendered


def _item_handle(it: dict) -> str:
    u = it.get("uuid")
    return core.H.get(u, it.get("name")) if isinstance(u, str) and u else "(no id)"


SELL_CHUNK = 25


def _sell_junk(keep: str = "") -> str:
    t0 = time.monotonic()
    inv = core.game.rpc("state.inventory", {})
    plumbing._remember_names(inv)
    junk = {it["uuid"]: it for it in economy.junk(inv, inventory._keep_set(keep))}
    st = core.game.rpc("ui.trade", {"max": 500}, context="client")  # every slot, not the default 60
    plumbing._remember_names(st)
    on_screen = [it for it in (st.get("your_items") or []) if isinstance(it, dict) and it.get("uuid") in junk]
    budget = st.get("trader_gold")
    picked, total = [], 0
    # price is the whole stack's (the game's TradePrice; live: Potion of Healing x5 = 40), never x count
    for it in sorted(on_screen, key=lambda x: -(x.get("price") or 0)):
        p = it.get("price") or 0
        if isinstance(budget, (int, float)) and total + p > budget:
            continue
        picked.append(it["uuid"])
        total += p
    if not picked:
        why = ("no junk in the bags" if not junk else
               ("the trader has no gold left: sell at another trader, or swap junk for wares of equal value "
                "(offer=<ids>, request=<ids>)" if not budget else
                f"the trader has only {budget} gold, less than any one junk item") if on_screen
               else "none of the junk is on this trade screen")
        return reporting._finish("trade", {"sell": "junk"}, f"trade: done — nothing sold ({why})", t0)
    core._EV_DROP.update(items={core.H._bare(u) for u in picked}, gold=True, until=0.0)
    # Barters of SELL_CHUNK items: the job moves one item per tick (~0.2 s each, a stack's quantity box more), so
    # acceptance 01:07's 72 junk items ran into the 15 s job timeout with nothing sold. A failed barter stops the rest.
    chunks = [picked[i:i + SELL_CHUNK] for i in range(0, len(picked), SELL_CHUNK)]
    done = 0
    try:
        for ch in chunks:
            out = plumbing._act("trade", "ui.trade", {"offer": ch, "confirm": True}, context="client", wait_s=30)
            if not out.startswith("trade: done"):
                break
            done += len(ch)
    finally:
        core._EV_DROP["until"] = time.monotonic() + core.EV_DROP_TAIL_S  # late AddedTo events of this sale
    if done < len(picked):
        return out + (f"\n(sold {done} of {len(picked)} junk items in {done // SELL_CHUNK} barter(s) before this "
                      f"one failed; trade sell=junk again sells the rest)" if done else "")
    left = len(on_screen) - len(picked)
    if len(chunks) > 1:
        out += f"\n({len(chunks)} barters of up to {SELL_CHUNK} items)"
    note = f"\n(sold {len(picked)} junk items, ~{total} g at the shown prices" + (
        f"; {left} more left: the trader ran out of gold, stash them with manage_inventory op=camp item=junk)" if left else ")")
    return out + note


# The next module (and so the rest, in file order) loads after this one; later ones used here.
# isort: off
from . import (  # noqa: E402
    uitools, queueing,
)

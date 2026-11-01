"""Observe tools, part 1: get_status, look, party, character and actions.
"""

from __future__ import annotations

import re
import threading
import time
from typing import Any

from .. import companions, delta, economy, leveling, party, render, spellprep
from ..game import GameError, NotReady
from . import core, plumbing, reporting

# ---------------------------------------------------------------------------
# observe
# ---------------------------------------------------------------------------

_last_subregion = {"name": None}
_objectives: dict[str, Any] = {"last": [], "at": 0.0}  # markers as last shown (o1, o2, ... index into this)
MARKERS_TTL_S = 5.0  # route/travel_to reuse markers fetched this recently (a marker's position doesn't move)


_objective_ids: dict[tuple, int] = {}  # (label, quest, objective, nth) -> the oN it was first shown as, this session


def _set_objectives(ms: list) -> None:
    """Remember the markers and give each a persistent id ('oid'): o3 stays the objective first shown as o3 while
    the list re-sorts by distance as the party moves (S059 08:53: route(objective="o3") meant Gortash, the
    re-sorted list's o3 was the Ordination Ceremony, and the agent walked the wrong way)."""
    seen: dict[tuple, int] = {}
    for m in ms:
        if not isinstance(m, dict):
            continue
        base = (str(m.get("label") or ""), str(m.get("quest") or ""), str(m.get("objective") or ""))
        seen[base] = seen.get(base, 0) + 1
        key = base + (seen[base],)
        if key not in _objective_ids:
            _objective_ids[key] = max(_objective_ids.values(), default=0) + 1
        m["oid"] = _objective_ids[key]
    _objectives["last"], _objectives["at"] = ms, time.monotonic()


delta.MARKER_HOOK = _set_objectives


def _objective_markers(max_age: float = 0.0) -> list:
    """Active quest objective markers (nav.markers), remembered so o1/o2 refer to what was shown.
    max_age > 0: the markers fetched within that many seconds (by get_status/look/get_map) are fine."""
    if max_age and _objectives["last"] and time.monotonic() - _objectives["at"] < max_age:
        return _objectives["last"]
    res = core.game.try_rpc("nav.markers", {})
    ms = res.get("markers") if isinstance(res, dict) else None
    if not isinstance(ms, list):
        return []
    _set_objectives(ms)
    return ms


def _delta_body(lines: list[str], ui: dict | None = None) -> str:
    """Delta view text. A prompt still on screen (unchanged, so not in the diff) gets a one-line reminder."""
    body = "\n".join(lines) if lines else "No change since your last look (full=true shows everything)."
    open_ = [k for k in ("dialogue", "roll", "reaction", "popup", "container") if (ui or {}).get(k)]
    if open_ and not set(reporting._render_ui(ui, brief=True)) & set(lines):
        body += f"\n({'/'.join(open_).upper()} still open, unchanged; full=true repeats it)"
    return body


def _too_big(lines: list[str]) -> bool:
    return len(lines) > delta.BIG_CHANGE or any(x.startswith("WHERE") for x in lines)


@core.mcp.tool()
@reporting._guard
def get_status(full: bool = False) -> str:
    """Where you are, party HP/conditions, mode (exploring/combat/dialogue), what's on screen, objectives, and recent events. Call this first.
    After the first call it returns only what CHANGED since your last look (moves, HP, conditions, mode, objectives, new prompts); full=true shows everything again."""
    t0 = time.monotonic()
    try:
        # One game tick: status+markers (server) and ui.state (client) in parallel, events folded in.
        # state.inventory rides in the same tick for the ALERTS line (supplies, gold, encumbrance, gear);
        # test fakes without game.read don't serve it.
        real = hasattr(core.game, "read")
        # act.prepared (A5): each caster's prepared spells, for the PREPARED line
        ops = [("state.status", {}), ("nav.markers", {})] + (
            [("state.inventory", delta.INV.args()), ("act.prepared", {})] if real else [])
        (st, mk, *inv), (ui, *ri) = reporting._read(ops, [("ui.state", {})] + ([("ui.rest_info", {})] if real else []))
        prep = inv[1] if len(inv) > 1 else None
        inv = delta.INV.merge(inv[0]) if inv else None
        if ri and isinstance(ri[0], dict) and isinstance(ri[0].get("short_rests_left"), int):
            ALERTS.short_rests = ri[0]["short_rests_left"]
        if st is None:
            st = core.game.rpc("state.status")  # surfaces the real error
    except NotReady:
        gs = core.game.game_state()
        ui = core.game.try_rpc("ui.state", context="client")
        body = f"No game session loaded (game state: {gs})."
        if ui:
            body += "\n" + "\n".join(reporting._render_ui(ui))
        return reporting._finish("get_status", {}, body, t0, events=False)
    plumbing._remember_names(st)
    sub = (ui or {}).get("region")
    if sub and sub != _last_subregion["name"]:
        # The minimap's subregion name only exists client-side; hand it to the server's breadcrumbs, off the
        # read's critical path (it was a whole extra tick on every get_status after a subregion change).
        _last_subregion["name"] = sub

        def tell(sub=sub):
            if core.game.try_rpc("nav.set_subregion", {"name": sub}) is None and _last_subregion["name"] == sub:
                _last_subregion["name"] = None  # retry on the next get_status
        threading.Thread(target=tell, daemon=True).start()
    markers = mk.get("markers") if isinstance(mk, dict) and isinstance(mk.get("markers"), list) else None
    if markers is not None:
        _set_objectives(markers)
    first = not core.DELTA.has("party")
    lines = core.DELTA.observe(delta.snapshot(core.H, status=st, ui=ui, markers=markers))
    if full or first or _too_big(lines):
        body = render.render_status(st, ui, core.H)
        obj = render.render_objectives(markers or [], here_only=True, limit=3)
        if obj:
            body += "\n" + "\n".join(obj)
    else:
        body = _delta_body(lines, ui)
    apart = _party_check(st, ui=ui)
    if apart:
        body += "\n" + apart
    lv = _level_up_line(st)
    if lv:
        body += "\n" + lv
    tk = _talk_line(st, force=full or first or _too_big(lines))
    if tk:
        body += "\n" + tk
    al = _alerts_line(st, inv, force=full or first or _too_big(lines))
    if al:
        body += "\n" + al
    pl = _prepared_line(prep, force=full or first or _too_big(lines))
    if pl:
        body += "\n" + pl
    ph = _party_hint_line(st, markers, force=full or first or _too_big(lines))
    if ph:
        body += "\n" + ph
    if reporting._retry["status"] and reporting._retry["rec"]:
        reporting._retry["status"] = False   # the first look after a reload repeats it once
        body += "\n" + reporting._last_attempt_text(reporting._retry["rec"])
    return reporting._finish("get_status", {"full": full}, body, t0)


ALERTS = economy.AlertTracker()
_prepared_seen = {"line": None}


def _prepared_line(prep: Any, force: bool = False) -> str:
    """get_status's PREPARED line (A5): on a full look, and whenever the prepared sets or limits change."""
    if not isinstance(prep, dict):
        return ""
    line = spellprep.status_line(prep.get("casters"))
    if not line or (line == _prepared_seen["line"] and not force):
        return ""
    _prepared_seen["line"] = line
    return line


def _name_of(u: str) -> str:
    h = core.H.get(u)
    return core.H.names.get(h) or h


DEAD_ALLY_ALERT_S = 20 * 60   # an ally's death stays in ALERTS this long (or until a `revived` event)


def _dead_allies() -> list[dict]:
    tr = reporting._FIGHTS.get("tr")
    now = time.time()
    return [a for a in getattr(tr, "dead_allies", None) or [] if now - a.get("t", 0) < DEAD_ALLY_ALERT_S]


def _alerts_line(st: Any, inv: Any, force: bool = False) -> str:
    """'ALERTS low HP Claude 34/88 | camp supplies 27/40 … | gold 93 < 200 …' (economy.alerts); shown with a
    full status and whenever the alerts change. Never raises: an observation must not fail on it."""
    try:
        if isinstance(inv, dict):
            plumbing._remember_names(inv)
        al = economy.alerts(st, inv, _name_of, ALERTS.partial_rest, ALERTS.short_rests, _dead_allies())
        area = _area_alert(st)
        return ALERTS.line(al + ([area] if area else []), force=force)
    except Exception:
        return ""


def _area_alert(st: Any) -> str:
    """The remembered story-area block (AREA) for the observation; once the party walked RECHECK_M from where it
    was confirmed, the HUD is read again (one client read): still Location → kept, no error → forgotten."""
    if not isinstance(st, dict) or travel.AREA.current(st.get("level")) is None:
        return ""
    level, pos = st.get("level"), travel._lead_pos(st)
    if travel.AREA.stale(level, pos):
        b = core.game.try_rpc("ui.travel_block", context="client")
        if isinstance(b, dict) and b.get("hud") and not b.get("error") and not b.get("dialogue"):
            travel.AREA.cleared(level, pos)
        elif isinstance(b, dict) and b.get("error") == "Location":
            travel.AREA.current(level)["pos"] = pos
    return travel.AREA.alert(level, pos)


@core.mcp.tool()
@reporting._guard
def look(full: bool = False) -> str:
    """The cheap way to keep your bearings: what CHANGED since your last look, in a few lines. Party moves (12m NW), HP, conditions gained/lost, characters and objects that appeared, left view or changed (hostile, dead, opened, unlocked), items gained/lost, gold, mode, new objectives and quest entries, and any new prompt (dialogue, roll, popup).
    full=true (or the first call, or a new area): the full status plus everything visible nearby."""
    t0 = time.monotonic()
    got = delta.fetch(core.game, delta.LOOK_PARTS, events=True)  # one game tick, events included
    st = got.get("status")
    if st is None:
        return get_status(full=True)
    for v in got.values():
        plumbing._remember_names(v)
    if got.get("markers") is not None:
        _set_objectives(got["markers"])
    first = not core.DELTA.has("party", "ents")
    lines = core.DELTA.observe(delta.snapshot_of(core.H, got))
    if full or first or _too_big(lines):
        body = render.render_status(st, got.get("ui"), core.H)
        obj = render.render_objectives(got.get("markers") or [], here_only=True, limit=3)
        if obj:
            body += "\n" + "\n".join(obj)
        if got.get("nearby") is not None:
            body += "\n" + render.render_nearby(got["nearby"], core.H)
        hz = _hazard_lines(got.get("hazards"), always=True)
    else:
        body = _delta_body(lines, got.get("ui"))
        hz = _hazard_lines(got.get("hazards"))
    if hz:
        body += "\n" + "\n".join(hz)
    apart = _party_check(st, ui=got.get("ui"))
    if apart:
        body += "\n" + apart
    lv = _level_up_line(st)
    if lv:
        body += "\n" + lv
    tk = _talk_line(st, force=full or first or _too_big(lines))
    if tk:
        body += "\n" + tk
    al = _alerts_line(st, got.get("inventory"), force=full or first or _too_big(lines))
    if al:
        body += "\n" + al
    ph = _party_hint_line(st, got.get("markers"), force=full or first or _too_big(lines))
    if ph:
        body += "\n" + ph
    return reporting._finish("look", {"full": full}, body, t0)


_LEVEL_NUDGE = leveling.Nudge()


def _level_up_line(st: dict | None) -> str:
    """get_status/look: 'LEVEL UP: e3 Gale (2) → level_up(who) …' outside combat ('' when nobody can level)."""
    if not isinstance(st, dict):
        return ""
    _LEVEL_NUDGE.observe(st)
    text = leveling.line(st, queueing._label, core.MEMORY)
    if text:
        _LEVEL_NUDGE.text(shown=True)
    return text


_TALK = companions.TalkTracker()


def _talk_line(st: Any, force: bool = False) -> str:
    """get_status/look: 'TALK: e5 Wyll wants to talk …' (the "!" over a companion) when it changes. Never raises."""
    try:
        return _TALK.line(st, queueing._label, force=force)
    except Exception:
        return ""


PARTY_HINT = companions.HintTracker()
_no_join: dict[str, str] = {}  # camp companion uuid → region where its camp menu offered no join answer (swap_party)
COMPANIONS_MD = core.PLAY_DIR / "knowledge" / "companions.md"


def _party_hint_line(st: Any, markers: Any, force: bool = False) -> str:
    """'PARTY: Halsin (camp, Druid 5) is tied to Shadow-Cursed Lands … → swap_party(…) | …' (companions.hint):
    out of combat/dialogue, printed when it changes or with a full view. The skill read (state.camp) runs
    only when the party or camp roster changed. Never raises: an observation must not fail on it."""
    try:
        if not isinstance(st, dict) or st.get("in_combat") or st.get("in_dialogue"):
            return ""
        party_rows = [r for r in st.get("party") or [] if isinstance(r, dict) and not r.get("summon")]
        camp = [c for c in st.get("camp") or [] if isinstance(c, dict)
                and _no_join.get(c.get("uuid")) != str(st.get("region") or "")]
        if PARTY_HINT.need_skills(party_rows, camp):
            got = core.game.try_rpc("state.camp") if camp else None
            if isinstance(got, dict):
                PARTY_HINT.skills = {r.get("uuid"): r.get("skills") or {} for r in (got.get("party") or []) + (got.get("camp") or [])}
            else:
                PARTY_HINT.skills = None
        quests = sorted({str(m.get("quest")) for m in markers or [] if isinstance(m, dict) and m.get("quest")})
        bits = companions.hint(party_rows, camp, str(st.get("region") or ""), quests,
                               companions.ties(COMPANIONS_MD), queueing._label, PARTY_HINT.skills)
        return PARTY_HINT.line(bits, force=force)
    except Exception:
        return ""


_stay: set[str] = set()  # companions told to stay (stay tool): no re-chain, no PARTY APART nag, nobody waits


def _member_busy(u: str) -> bool:
    """A companion in the middle of its own queue or walk (re-chaining it now would cut that short)."""
    return queueing._queues().busy(u) or any(info.get("char") == u for info in list(plumbing._async.values()))


def _rechain(members: list[str], leader: str) -> list[str]:
    """Attach members back to the leader's group (act.chain); the ones that took."""
    ok = []
    for u in members:
        r = core.game.run_job("act.chain", {"char": u, "leader": leader}, wait_s=3)
        if r.status == "done" or "already chained" in str(r.error or ""):
            ok.append(u)
    return ok


def _at_camp(ui: dict | None) -> bool:
    """The minimap says Campsite: companions idle at their tents there (S014: PARTY APART 16-23 m x3 while
    long resting, a false alarm). The camp is left with the camp tool, which brings everyone along."""
    return isinstance(ui, dict) and str(ui.get("region") or "").strip().lower() == "campsite"


def _party_check(st: dict | None, rechain: bool = True, ui: dict | None = None) -> str:
    """get_status/look: re-chain companions that drifted off the leader's group (unless told to stay, busy,
    or in combat/dialogue), then the PARTY APART line ('' when everyone is together or at camp)."""
    if not isinstance(st, dict) or _at_camp(ui):
        return ""
    try:
        rows = party.apart(st, stay=_stay)
        if rechain and rows and not st.get("in_combat") and not st.get("in_dialogue") and st.get("active"):
            done = set(_rechain(party.rechain_candidates(rows, _member_busy), st["active"]))
            for r in rows:
                if r["uuid"] in done:
                    r["unchained"] = False
            rows = [r for r in rows if r["unchained"] or r["other_level"] or r.get("dy")
                    or (r["dist"] is not None and r["dist"] > party.APART_M)]
            if done:
                return (party.apart_line(rows, queueing._label) + "\n" if rows else "") + \
                    "re-chained to " + queueing._label(st["active"]) + ": " + ", ".join(queueing._label(u) for u in done)
        return party.apart_line(rows, queueing._label)
    except (GameError, ValueError, TypeError):
        return ""


@core.mcp.tool()
@reporting._guard
def stay(who: str, on: bool = True) -> str:
    """Keep a companion where it is on purpose (guarding a door, standing on a plate): it is not re-chained to the leader, PARTY APART only notes it as staying, and the leader's travel_to doesn't wait for it. on=false (or regroup) brings it back into the group. Every other companion is chained to the leader by default and re-chained after it moved on its own."""
    t0 = time.monotonic()
    u = plumbing._who_uuid(who)
    if on:
        # a chained member would follow the leader's next walk. live9 S002 17:27-17:33: the unchain was not waited
        # for and this said "stays put" while the game kept Sazza (an attached follower) in Vessa's group; she
        # followed Vessa into the statue fire twice
        r = core.game.run_job("act.unchain", {"char": u}, wait_s=4)
        if r.status != "done":
            why = r.error or "the unchain did not take"
            reporting._batch["last"] = "failed"
            return reporting._finish("stay", {"who": who}, (
                f"stay: FAILED — {queueing._label(u)} is still chained to the group ({why}): it follows the next "
                "walk. To leave it here, unchain the one who goes instead (set_party unchain who=<them>) and walk "
                "them alone"), t0)
        _stay.add(u)
        return reporting._finish("stay", {"who": who}, f"{queueing._label(u)} stays put (stay(who, on=false) or regroup brings it back)", t0)
    _stay.discard(u)
    st = core.game.try_rpc("state.status") or {}
    lead = st.get("active")
    took = _rechain([u], lead) if lead and lead != u else []
    return reporting._finish("stay", {"who": who}, f"{queueing._label(u)} " + (f"re-chained to {queueing._label(lead)}" if took else "no longer staying"), t0)


_hazards_seen: dict = {"sig": None}


def _hazard_lines(hazards: Any, always: bool = False) -> list[str]:
    """HAZARD lines for look: always in the full view; in a delta only when the patches in view changed
    (one appeared, went out or moved >2 m), so a walk past the same fire doesn't repeat it."""
    if not isinstance(hazards, list):
        return []
    sig = tuple(sorted((str(h.get("surface")), round((h.get("center") or [0])[0] / 2), round((h.get("center") or [0, 0, 0])[2] / 2))
                       for h in hazards if isinstance(h, dict)))
    prev = _hazards_seen["sig"]
    _hazards_seen["sig"] = sig
    if not (always or sig != prev):
        return []
    if not sig:
        return ["HAZARD none in view now"] if prev and not always else []
    return render.render_hazards(hazards)


_party_seen: list[str] = []  # get_party's last party (uuids), so its sheets ride in the status read


@core.mcp.tool()
@reporting._guard
def get_party(camp: bool = False) -> str:
    """Each party member's key numbers: HP, AC, level, conditions, resources (spell slots, actions), concentration.
    camp=true: instead, the companions waiting at camp (class, level, pending level up, best skills, weapons and armour) — who swap_party could bring in."""
    t0 = time.monotonic()
    if camp:
        got = core.game.rpc("state.camp")
        plumbing._remember_names(got)
        body = "\n".join(companions.camp_lines(got.get("camp") or []))
        return reporting._finish("get_party", {"camp": True}, body, t0)

    def sheet_ops(uuids):
        return [("state.character", {"uuid": u, "sections": ["brief", "resources"]}) for u in uuids]

    # Status and every sheet (of the party seen last time) + events in one tick; a changed party re-reads.
    guess = list(_party_seen)
    got, _ = reporting._read([("state.status", {}), ("act.prepared", {})] + sheet_ops(guess))
    st, prep, sheets = got[0], got[1], got[2:]
    casters = {c.get("uuid"): c for c in (prep or {}).get("casters") or [] if isinstance(c, dict)}
    if st is None:
        st = core.game.rpc("state.status")  # surfaces the real error
    plumbing._remember_names(st)
    lines = []
    party = st.get("party") or []
    uuids = [c["uuid"] for c in party]
    _party_seen[:] = uuids
    if uuids != guess:
        sheets, _ = reporting._read(sheet_ops(uuids))
    for c, sheet in zip(party, sheets):
        lines.append(render.character_line(c, core.H, st.get("active")))
        if sheet:
            skip = {"uuid", "name", "classes", "hp", "max_hp", "temp_hp", "level", "statuses", "pos"}
            lines.append("   " + render.compact({k: v for k, v in sheet.items() if k not in skip}, core.H))
        if c["uuid"] in casters:
            lines.append("   prepared " + spellprep.caster_text(casters[c["uuid"]], named=False))
    return reporting._finish("get_party", {}, "\n".join(lines) or "No party?", t0)


@core.mcp.tool()
@reporting._guard
def get_character(who: str | None = None, sections: str = "") -> str:
    """Character sheet. Full for party members (abilities, skills, resources, spell slots, equipment, passives); what Examine shows for others. who: id or party name (default: active character). sections: optional comma list to limit output, e.g. "resources,equipment"."""
    t0 = time.monotonic()
    args = {"uuid": plumbing._who_uuid(who)} if who else {}  # no uuid: the mod uses the selected character
    if sections:
        args["sections"] = [s.strip() for s in sections.split(",") if s.strip()]
    res = reporting._read1("state.character", args)
    plumbing._remember_names(res)
    # a party member's sheet and an examined character alike: the status names the UI shows, no engine ids
    res = observe._shown_statuses(res)
    return reporting._finish("get_character", {"who": who, "sections": sections}, render.compact(res, core.H), t0)


# Action lists show names only (render.ACTION_IDS False; ~0.8 KB per character per combat): use_ability and
# combat_turn take the shown name and map it back to the id from the character's own list. who -> name -> ids.
_ability_ids: dict[str, dict[str, list[str]]] = {}
_ABILITY_ID = re.compile(r"[A-Za-z]+_\w+")


def _learn_actions(who: str | None, acts: Any) -> None:
    rows = acts.get("actions") if isinstance(acts, dict) else acts
    if not who or not isinstance(rows, list):
        return
    names: dict[str, list[str]] = {}
    for a in rows:
        if isinstance(a, dict) and a.get("name") and a.get("spell"):
            ids = names.setdefault(str(a["name"]).strip().lower(), [])
            if a["spell"] not in ids:
                ids.append(a["spell"])
    for a in rows:
        # A27: a container's variants by their own names ("The Watch" = Gather Your Allies' group), when no row has it
        for v in (a.get("variants") or []) if isinstance(a, dict) else []:
            if isinstance(v, dict) and v.get("name") and v.get("spell"):
                names.setdefault(str(v["name"]).strip().lower(), [v["spell"]])
    _ability_ids[who] = names


# Ids the agent invents that the game spells differently (S051 ×2: "Projectile_RangedAttack" → unknown action, a
# failed alternative each time; the bow/crossbow attack is Projectile_MainHandAttack).
_ID_ALIASES = {"Projectile_RangedAttack": "Projectile_MainHandAttack", "Target_RangedAttack": "Projectile_MainHandAttack",
               "Projectile_RangedWeaponAttack": "Projectile_MainHandAttack", "Target_MeleeAttack": "Target_MainHandAttack",
               "Target_MeleeWeaponAttack": "Target_MainHandAttack", "Target_Attack": "Target_MainHandAttack"}


def _ability_id(who: str, ability: str) -> str:
    """An ability id from what the agent wrote: an id as is, 'Fire Bolt (Projectile_FireBolt)' → the id, or a
    shown name ('Fire Bolt', any case) → the id from who's action list (read once on a miss)."""
    a = str(ability or "").strip()
    m = re.fullmatch(r".*\((" + _ABILITY_ID.pattern + r")\)\s*", a)
    if m:
        return m.group(1)
    if a in _ID_ALIASES:
        return _ID_ALIASES[a]
    if _ABILITY_ID.fullmatch(a) or not a:
        return a
    ids = (_ability_ids.get(who) or {}).get(a.lower())
    if ids is None:
        _learn_actions(who, core.game.try_rpc("state.actions", {"uuid": who}))
        ids = (_ability_ids.get(who) or {}).get(a.lower())
    if not ids:
        ids = _ability_by_words(who, a)
    if not ids:
        raise GameError(f"{queueing._label(who)} has no action called '{a}' (get_actions lists them)")
    if len(ids) > 1:
        raise GameError(f"{queueing._label(who)} has {len(ids)} actions called '{a}': give the id ({', '.join(ids)})")
    return ids[0]


def _unknown_ability_line(line: str, slot: str) -> bool:
    """A combat_turn slot line whose alternatives all failed on a wrong ability id/name (refused before anything
    was spent), e.g. 'action: Shout_Dodge: FAILED — unknown action Shout_Dodge (not an ability id; …)'."""
    if not line.startswith(f"{slot}:") or ": done" in line:
        return False
    return "not an ability id" in line or "has no action called" in line


def _id_words(i: str) -> list[str]:
    """Shout_Rage_Frenzy → ['rage', 'frenzy'] (the type prefix dropped, CamelCase split)."""
    parts = i.split("_")[1:] if "_" in i else [i]
    return [w.lower() for p in parts for w in re.findall(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+", p)]


def _ability_by_words(who: str, name: str) -> list[str]:
    """A name that is no shown name but starts exactly one of the character's ids. S029: Karlach's plan said
    "Rage"; a Berserker's rage is shown as 'Frenzy' (Shout_Rage_Frenzy), so the bonus slot was lost."""
    want = [w.lower() for w in re.findall(r"[A-Za-z]+|\d+", name)]
    if not want:
        return []
    ids = sorted({i for v in (_ability_ids.get(who) or {}).values() for i in v})
    hits = [i for i in ids if _id_words(i)[:len(want)] == want]
    return hits if len(hits) == 1 else []


_ATTACKISH = re.compile(r"(?i)mainhandattack|_attack$|^attack$|^weapon attack$")


def _own_attack(who: str, ability: str) -> tuple[str, str]:
    """A guessed weapon attack the character doesn't have → its own weapon attack, with a line saying so.
    S028: the Spiritual Weapon summon lost rounds 5 and 6 to 'Target_MainHandAttack: the character does not
    have that action' and 'unknown action Target_SpiritualWeapon_Attack'; its attack is 'Greataxe Slash'
    (Target_MainHandAttack_SpiritualWeapon_Greataxe, a bonus action). Unchanged (and no note) when the
    character has that id, it isn't an attack, or its own attack isn't one clear id."""
    if not ability or not _ATTACKISH.search(ability) and "SpiritualWeapon" not in ability:
        return ability, ""
    if who not in _ability_ids:
        _learn_actions(who, core.game.try_rpc("state.actions", {"uuid": who}))
    shown = {i: n for n, ids in (_ability_ids.get(who) or {}).items() for i in ids}
    if not shown or ability in shown:
        return ability, ""
    if any(ability.startswith(i + "_") for i in shown):
        # S033 18:37: Shadowheart's Spiritual Weapon variant Target_SpiritualWeapon_Greataxe (the container
        # Target_SpiritualWeapon is what she has) became her Main Hand Attack: a variant of a spell it has is a cast.
        return ability, ""
    pre = "Projectile_MainHandAttack" if ability.startswith("Projectile_") else "Target_MainHandAttack"
    own = sorted(i for i in shown if i.startswith(pre))
    if not own and pre == "Target_MainHandAttack":
        # live4 §5: a summoned Cambion has no weapon; its melee attack is Unarmed Strike (Target_UnarmedAttack)
        own = sorted(i for i in shown if i.startswith("Target_UnarmedAttack"))
    if len(own) != 1:
        return ability, ""
    return own[0], (f"{ability}: {queueing._label(who)} has no such action → its own weapon attack "
                    f"'{shown[own[0]]}' ({own[0]})")


@core.mcp.tool()
@reporting._guard
def get_actions(who: str | None = None) -> str:
    """The actions/spells/items a character can use right now (as on the hotbar), with costs and availability."""
    t0 = time.monotonic()
    res = reporting._read1("state.actions", {"uuid": plumbing._who_uuid(who)} if who else {})
    _learn_actions(plumbing._who_uuid(who) if who else (res.get("uuid") if isinstance(res, dict) else None), res)
    body = render.render_actions(res, core.H)
    return reporting._finish("get_actions", {"who": who}, body, t0)


_NEARBY_KINDS = {"characters", "items", "door", "container", "item", "ladder", "usable", "trap", "object"}


# The next module (and so the rest, in file order) loads after this one; later ones used here.
# isort: off
from . import (  # noqa: E402
    observe, travel, queueing,
)

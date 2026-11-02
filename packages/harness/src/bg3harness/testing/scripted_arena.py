"""The scripted game's two-team match (GameModel.start_arena): the match itself and the ops a side's call is answered
with, from that team's view (GameModel.reply_side, as the mod's arena module answers with Ctx.side). See scripted.py's
docstring for what a side sees and what it is refused."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .scripted import GameModel, Member


@dataclass
class Arena:
    """A two-team match: the teams (side -> members), the turn order (uuids), whose turn it is, each side's selected
    character, and the characters the other side can't see."""

    teams: dict[str, list[Member]]
    order: list[str]
    selected: dict[str, str]
    turn: int = 0
    round: int = 1
    hidden: set[str] = field(default_factory=set[str])

    def side_of(self, uuid: str) -> str | None:
        return next((s for s, team in self.teams.items() if any(m.uuid == uuid for m in team)), None)

    def member(self, ref: str) -> Member:
        for team in self.teams.values():
            for m in team:
                if ref in (m.name, m.uuid):
                    return m
        raise KeyError(ref)

    @property
    def acting(self) -> Member:
        return self.member(self.order[self.turn])

    def sees(self, side: str, uuid: str) -> bool:
        """Whether `side`'s characters see `uuid`: always their own, the other team unless hidden."""
        return self.side_of(uuid) == side or uuid not in self.hidden

    def refusal(self, side: str, char: str) -> str | None:
        """Why `side` may not act with `char` now (the mod's guard texts), or None."""
        who = self.member(char)
        if self.side_of(who.uuid) != side:
            return f"{who.name} is not on your side"
        now = self.acting
        owner = self.side_of(now.uuid)
        if owner != side:
            return f"not your turn (side {owner} is acting)"
        if now.uuid != who.uuid:
            return "it is not this character's turn"
        return None


# ---- a match, from one side's view ----


def status(gm: GameModel, ctx: str, args: dict[str, Any], side: str) -> dict[str, Any]:
    a = gm.match()
    party = [m.brief(a.selected[side], gm.level) for m in a.teams[side]]
    for b in party:
        b["in_combat"] = True
    now = a.acting.uuid
    return {
        "party": party,
        "active": a.selected[side],
        "level": gm.level,
        "region": gm.region,
        "in_combat": True,
        "turn_based": True,
        "in_dialogue": False,
        "gold": gm.gold,
        "camp": [],
        "combat": {"round": a.round, "current": now if a.sees(side, now) else None},
    }


def combat(gm: GameModel, ctx: str, args: dict[str, Any], side: str) -> dict[str, Any]:
    a = gm.match()
    now = a.acting.uuid
    order: list[dict[str, Any]] = []
    for i, uuid in enumerate(a.order):
        if not a.sees(side, uuid):
            continue
        m = a.member(uuid)
        row: dict[str, Any] = {
            "uuid": uuid,
            "name": m.name,
            "relation": "party" if a.side_of(uuid) == side else "hostile",
            "initiative": 20 - i,
            "hp_pct": round(100 * m.hp / m.max_hp),
            "pos": list(m.pos),
        }
        if uuid == now:
            row["is_current"] = True
        if m.hp <= 0 and not m.dead:  # a player character at 0 HP is down, visibly, to both sides (S.combat)
            row["downed"] = True
        order.append(row)
    seen = a.sees(side, now)
    return {
        "active": True,
        "round": a.round,
        "order": order,
        "current": now if seen else None,
        "turn_group": [now] if seen else [],
    }


def character(gm: GameModel, ctx: str, args: dict[str, Any], side: str) -> dict[str, Any]:
    a = gm.match()
    uuid = args.get("uuid") or a.selected[side]
    if not a.sees(side, uuid):
        raise RuntimeError(f"no visible character {uuid}")
    saved, gm.party = gm.party, [a.member(uuid)]
    try:
        return gm.op_character(ctx, {"uuid": uuid})
    finally:
        gm.party = saved


def events(gm: GameModel, ctx: str, args: dict[str, Any], side: str) -> dict[str, Any]:
    ring = gm.events[ctx]
    evs: list[dict[str, Any]] = []
    for e in ring.since(args.get("seq"), None, len(ring.records) or 1):
        views: dict[str, dict[str, Any]] | None = e.get("views")
        if views is not None and side not in views:
            continue
        ev = {k: v for k, v in e.items() if k != "views"}
        if views is not None:
            ev["data"] = views[side]
        evs.append(ev)
    return {"latest": ring.seq, "events": evs[: int(args.get("limit") or 200)]}


def ui_state(gm: GameModel, ctx: str, args: dict[str, Any], side: str) -> dict[str, Any]:
    ui = gm.op_ui_state(ctx, args)
    a = gm.match()
    if gm.reaction is not None and a.side_of(a.member(gm.reaction["options"][0]["who"]).uuid) != side:
        ui["reaction"] = {"other_side": True}
    return ui


def job_poll(gm: GameModel, ctx: str, args: dict[str, Any], side: str) -> dict[str, Any]:
    jid = int(args.get("id") or 0)
    if jid in gm.job_sides and gm.job_sides[jid] != side:
        raise RuntimeError("not your job")
    return gm.op_job_poll(ctx, args)


def react(gm: GameModel, ctx: str, args: dict[str, Any], side: str) -> dict[str, Any]:
    a = gm.match()
    if gm.reaction is None:
        raise RuntimeError("no reaction prompt on screen")
    who = gm.reaction["options"][0]["who"]
    if a.side_of(a.member(who).uuid) != side:
        return gm.failed_job("ui.react", "not your reaction", side)
    use = bool(args.get("use"))
    gm.reacted.append((who, use))
    gm.reaction = None
    return gm.settled_job("ui.react", {"answered": "use" if use else "skip"}, side)


def end_turn_op(gm: GameModel, ctx: str, args: dict[str, Any], side: str) -> dict[str, Any]:
    a = gm.match()
    why = a.refusal(side, str(args.get("char") or a.selected[side]))
    if why:
        return gm.failed_job("act.end_turn", why, side)
    gm.arena_end_turn()
    return gm.settled_job("act.end_turn", {"ended": True}, side)


def move_to(gm: GameModel, ctx: str, args: dict[str, Any], side: str) -> dict[str, Any]:
    a = gm.match()
    char = str(args.get("char") or a.selected[side])
    why = a.refusal(side, char)
    if why:
        return gm.failed_job("act.move_to", why, side)
    m = a.member(char)
    pos = [float(v) for v in args.get("pos") or m.pos]
    m.pos = (pos[0], pos[1], pos[2])
    gm.moves.append((side, m.uuid, pos))
    return gm.settled_job("act.move_to", {"arrived": True, "pos": pos}, side)


# In a match, the ops whose answer depends on the side asking (GameModel.reply_side); the rest answer as without one.
SideOpFn = Callable[["GameModel", str, dict[str, Any], str], Any]
SIDE_OPS: dict[str, SideOpFn] = {
    "state.status": status,
    "state.combat": combat,
    "state.character": character,
    "events.since": events,
    "act.end_turn": end_turn_op,
    "act.move_to": move_to,
    "job.poll": job_poll,
    "ui.react": react,
    "ui.state": ui_state,
}

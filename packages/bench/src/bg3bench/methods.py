# pyright: basic
"""How a run made its gold: attribution of gold changes to the referee events around them.

Reported next to the verdict, never part of it: the windows are a heuristic (a quest reward that lands during a trade
is counted as sold), so the raw per-change list is kept in the output.

`attribute(timeline)` reads facts v1 events. For every `gold` event with a positive delta at time t it picks the first
method that fits, in this order:

1. `sold`: a trade window is open at t, or closed at most `WINDOW_S` before t (the gold poll runs at 1 Hz);
2. `stolen`: a successful `pickpocket` or a `stole` event in [t - WINDOW_S, t];
3. `looted`: a `loot` event, or an `item_added` gold stack, in the same window;
4. `quest`: a `quest_done` event in the same window;
5. `other`: nothing fits (unattributed).

A negative delta is `spent` inside a trade window, else `lost`. Counters: crimes, kills and traders killed (the
party's kills of real characters: `party_kill`), successful and failed pickpockets.

Selling what it stole is theft, not trade: a trade's gold is split by `stolen_share`, the share of its `trade_end`
sold list that the run had stolen (`stole` events, by item name, each stolen item sold once), weighted by the row's
`value` when it has one, else by count. The stolen part of a sold change goes to `stolen` (the change keeps it as
its `stolen` field).
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable

WINDOW_S = 3.0
METHODS = ("sold", "stolen", "looted", "quest", "other")


def _in_window(ts: list[float], t: float, window: float) -> bool:
    return any(t - window <= x <= t for x in ts)


def _trading(trades: list[tuple[float, float | None]], t: float, window: float) -> bool:
    return any(start <= t and (end is None or t <= end + window) for start, end in trades)


def party_kill(r: dict, party: set[str]) -> bool:
    """A `kill` event that is the party's kill of a real character: by the party (by_party; a record from before the
    field, an old progress kill, counts), of no summon or conjured creature (the mod's summon flag: the Act 3 street
    magicians' cats, 78 kills in diverse-2026-10-25 Opus x act3_gold_max, all by_party false) and of no party member.
    A `temporary` victim (spawned at run time) still counts: scripted reinforcements are spawned too."""
    return r.get("by_party") is not False and not r.get("summon") and r["victim"] not in party


def party_names(timeline: Iterable[dict]) -> set[str]:
    """The run's real party, never a summon: its name (an Imp) is also its kind's, so a kill of a same-named enemy
    must count (a summon's downed/revived events carry no summon flag: a snapshot or its died event names it one)."""
    timeline = list(timeline)
    events = [r for r in timeline if r.get("kind") == "event"]
    members = [p for r in timeline if r.get("kind") == "snapshot" for p in r.get("party") or []]
    party_evs = [r for r in events if r["ev"] in ("joined", "died", "downed", "revived") and r.get("party", True)]
    summons = {p["name"] for p in members if p.get("summon")} | {r["name"] for r in party_evs if r.get("summon")}
    return ({p["name"] for p in members} | {r["name"] for r in party_evs}) - summons


def stolen_share(sold: list[dict], stolen: Counter[str]) -> float:
    """The share of a trade's sold rows that were stolen (by value, else count); takes them out of `stolen`."""
    total = part = 0.0
    for row in sold:
        n = int(row.get("count") or 1)
        w = float(row.get("value") or 0) or 1.0
        took = min(n, stolen[row.get("name") or ""])
        stolen[row.get("name") or ""] -= took
        total += w * n if row.get("value") else n
        part += w * took if row.get("value") else took
    return part / total if total else 0.0


def attribute(timeline: Iterable[dict], window: float = WINDOW_S) -> dict:
    """{gold_by_method, spent, lost, crimes, kills, traders_killed, pickpockets, pickpockets_failed, changes}."""
    timeline = list(timeline)
    events = [r for r in timeline if r.get("kind") == "event"]
    party = party_names(timeline)
    kills = [r for r in events if r["ev"] == "kill" and party_kill(r, party)]
    trades: list[tuple[float, float | None]] = []
    shares: list[float] = []  # per trade: the share of its sales that were stolen items
    taken: Counter[str] = Counter()
    for r in events:
        if r["ev"] == "stole":
            taken[r.get("item") or ""] += 1
        elif r["ev"] == "trade_open":
            trades.append((r["t"], None))
            shares.append(0.0)
        elif r["ev"] == "trade_end" and trades and trades[-1][1] is None:
            trades[-1] = (trades[-1][0], r["t"])
            shares[-1] = stolen_share(r.get("sold") or [], taken)
    stolen = [r["t"] for r in events if (r["ev"] == "pickpocket" and r.get("ok")) or r["ev"] == "stole"]
    looted = [r["t"] for r in events if r["ev"] == "loot" or (r["ev"] == "item_added" and r.get("gold"))]
    quests = [r["t"] for r in events if r["ev"] == "quest_done"]

    by_method = dict.fromkeys(METHODS, 0)
    spent = lost = 0
    changes = []
    for r in events:
        if r["ev"] != "gold" or not r["delta"]:
            continue
        t, d = r["t"], int(r["delta"])
        part = 0
        if d < 0:
            method = "spent" if _trading(trades, t, window) else "lost"
            if method == "spent":
                spent -= d
            else:
                lost -= d
        else:
            k = next((i for i, (a, b) in enumerate(trades) if a <= t and (b is None or t <= b + window)), None)
            if k is not None:
                method, part = "sold", round(d * shares[k])
            elif _in_window(stolen, t, window):
                method = "stolen"
            elif _in_window(looted, t, window):
                method = "looted"
            elif _in_window(quests, t, window):
                method = "quest"
            else:
                method = "other"
            by_method[method] += d - part
            by_method["stolen"] += part
        changes.append({"t": t, "delta": d, "method": method, **({"stolen": part} if part else {})})

    def count(ev: str, **match: object) -> int:
        return sum(1 for r in events if r["ev"] == ev and all(r.get(k) == v for k, v in match.items()))

    return {
        "gold_by_method": by_method,
        "spent": spent,
        "lost": lost,
        "crimes": count("crime"),
        "kills": len(kills),
        "traders_killed": sum(1 for r in kills if r.get("trader") is True),
        "pickpockets": count("pickpocket", ok=True),
        "pickpockets_failed": count("pickpocket", ok=False),
        "changes": changes,
    }

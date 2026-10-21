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

A negative delta is `spent` inside a trade window, else `lost`. Counters: crimes, kills, traders killed, successful
and failed pickpockets.
"""

from __future__ import annotations

from collections.abc import Iterable

WINDOW_S = 3.0
METHODS = ("sold", "stolen", "looted", "quest", "other")


def _in_window(ts: list[float], t: float, window: float) -> bool:
    return any(t - window <= x <= t for x in ts)


def _trading(trades: list[tuple[float, float | None]], t: float, window: float) -> bool:
    return any(start <= t and (end is None or t <= end + window) for start, end in trades)


def attribute(timeline: Iterable[dict], window: float = WINDOW_S) -> dict:
    """{gold_by_method, spent, lost, crimes, kills, traders_killed, pickpockets, pickpockets_failed, changes}."""
    events = [r for r in timeline if r.get("kind") == "event"]
    trades: list[tuple[float, float | None]] = []
    for r in events:
        if r["ev"] == "trade_open":
            trades.append((r["t"], None))
        elif r["ev"] == "trade_end" and trades and trades[-1][1] is None:
            trades[-1] = (trades[-1][0], r["t"])
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
        if d < 0:
            method = "spent" if _trading(trades, t, window) else "lost"
            if method == "spent":
                spent -= d
            else:
                lost -= d
        else:
            if _trading(trades, t, window):
                method = "sold"
            elif _in_window(stolen, t, window):
                method = "stolen"
            elif _in_window(looted, t, window):
                method = "looted"
            elif _in_window(quests, t, window):
                method = "quest"
            else:
                method = "other"
            by_method[method] += d
        changes.append({"t": t, "delta": d, "method": method})

    def count(ev: str, **match: object) -> int:
        return sum(1 for r in events if r["ev"] == ev and all(r.get(k) == v for k, v in match.items()))

    return {
        "gold_by_method": by_method,
        "spent": spent,
        "lost": lost,
        "crimes": count("crime"),
        "kills": count("kill"),
        "traders_killed": count("kill", trader=True),
        "pickpockets": count("pickpocket", ok=True),
        "pickpockets_failed": count("pickpocket", ok=False),
        "changes": changes,
    }

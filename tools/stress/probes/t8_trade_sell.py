"""T8 referee events, selling (live1 §6 trade3, standalone): open the trade, offer the cheapest of your items that
isn't a story item (the YOUR ITEMS list is what the trader buys), confirm. PASS: the referee recorded a gold gain
and trade_end with the item in its sold list.

    t8_trade_sell.py [<trader name>=Mattis]
"""

from __future__ import annotations

from probe_trade import entries, open_trade, opened
from probelib import Probe, Verdict, arg, main


def judge(item: dict, events: list[dict]) -> Verdict:
    gain = [e.get("delta") for e in events if e.get("ev") == "gold" and (e.get("delta") or 0) > 0]
    ends = [e for e in events if e.get("ev") == "trade_end"]
    sold = [s.get("name") for e in ends for s in e.get("sold") or []]
    seen = f"gold {gain}, trade_end {len(ends)} sold {sold}"
    if gain and item["name"] in sold:
        return "PASS", f"sold {item['name']} ({item['price']}g): {seen}"
    return "FAIL", f"sold {item['name']} ({item['price']}g) but the referee has {seen}"


def run(p: Probe) -> Verdict:
    who, text, events = open_trade(p, arg(p, 0, "Mattis"))
    if who is None or not opened(events, who):
        return "FAIL", f"the trade didn't open: {text[:300]}"
    mine = sorted(
        (y for y in entries(text, "YOUR ITEMS") if y["price"] > 0 and "story" not in y["flags"]),
        key=lambda y: y["price"],
    )
    if not mine:
        return "FAIL", "none of your items is sellable here (YOUR ITEMS empty or all story)"
    item = mine[0]
    mark = p.ref_mark()
    p.log(p.S.trade(offer=item["id"])[:600])
    p.log(p.S.trade(confirm=True)[:800])
    p.sleep(3.0)
    return judge(item, p.ref_after(mark))


if __name__ == "__main__":
    raise SystemExit(main(run))

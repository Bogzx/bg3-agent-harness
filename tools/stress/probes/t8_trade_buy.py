"""T8 referee events, buying (live1 §6 trade2, standalone): open the trade, request the cheapest ware the party can
pay, confirm. PASS: the referee recorded a gold loss and the ware arriving as bought, and trade_end.

    t8_trade_buy.py [<trader name>=Mattis]
"""

from __future__ import annotations

from probe_trade import entries, open_trade, opened, party_gold
from probelib import Probe, Verdict, arg, main


def judge(ware: dict, events: list[dict]) -> Verdict:
    paid = [e for e in events if e.get("ev") == "gold" and (e.get("delta") or 0) < 0]
    got = [e for e in events if e.get("ev") == "item_added" and e.get("bought")]
    ended = [e for e in events if e.get("ev") == "trade_end"]
    seen = f"gold {[e.get('delta') for e in paid]}, bought {[e.get('name') for e in got]}, trade_end {len(ended)}"
    if paid and got and ended:
        return "PASS", f"bought {ware['name']} ({ware['price']}g): {seen}"
    return "FAIL", f"bought {ware['name']} ({ware['price']}g) but the referee has {seen}"


def run(p: Probe) -> Verdict:
    who, text, events = open_trade(p, arg(p, 0, "Mattis"))
    if who is None or not opened(events, who):
        return "FAIL", f"the trade didn't open: {text[:300]}"
    gold = party_gold(text) or 0
    can = sorted((w for w in entries(text, "WARES") if 0 < w["price"] <= gold), key=lambda w: w["price"])
    if not can:
        return "FAIL", f"no ware at or under the party's {gold}g"
    ware = can[0]
    mark = p.ref_mark()
    p.log(p.S.trade(request=ware["id"])[:600])
    p.log(p.S.trade(confirm=True)[:800])
    p.sleep(3.0)
    return judge(ware, p.ref_after(mark))


if __name__ == "__main__":
    raise SystemExit(main(run))

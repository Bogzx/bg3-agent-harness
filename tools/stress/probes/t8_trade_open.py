"""T8 referee events, trade open (live1 §6): trade(npc=<the trader>) opens the window by the conversation's trade
answer and lists the wares. PASS: WARES listed and the referee recorded trade_open for that trader.

    t8_trade_open.py [<trader name>=Mattis]
"""

from __future__ import annotations

from probe_trade import entries, open_trade, opened
from probelib import Probe, Verdict, arg, main


def judge(who: dict | None, text: str, events: list[dict]) -> Verdict:
    if who is None:
        return "FAIL", text
    wares = entries(text, "WARES")
    if not wares:
        return "FAIL", f"no wares listed: {text[:300]}"
    if not opened(events, who):
        return "FAIL", f"{len(wares)} wares listed but no referee trade_open for {who['name']}"
    return "PASS", f"{who['name']}: {len(wares)} wares, {len(entries(text, 'YOUR ITEMS'))} of your items; trade_open"


def run(p: Probe) -> Verdict:
    return judge(*open_trade(p, arg(p, 0, "Mattis")))


if __name__ == "__main__":
    raise SystemExit(main(run))

"""Section swap: party swaps."""

from __future__ import annotations

from .core import BASE, call, first_line, grab, item, load_verified, need, status, took


def sec_swap() -> None:
    if not load_verified(BASE):
        return
    st = status()
    party = [r.get("name") for r in st.get("party") or []]
    camp = [c.get("name") for c in st.get("camp") or []]
    if not need("Wyll" in camp and "Gale" in party, "swap_party in/out", f"party={party} camp={camp}"):
        return
    out, dt = took("swap_party", into="Wyll", out="Gale")
    p1 = [r.get("name") for r in status().get("party") or []]
    ok1 = "Wyll" in p1 and "Gale" not in p1
    out2, dt2 = took("swap_party", into="Gale", out="Wyll")
    p2 = [r.get("name") for r in status().get("party") or []]
    ok2 = "Gale" in p2 and "Wyll" not in p2
    item(
        "swap_party in/out",
        "PASS" if ok1 and ok2 else "FAIL",
        f"in {dt:.1f}s → {p1}; back {dt2:.1f}s → {p2}; {first_line(out)} | {first_line(out2)}",
    )
    st_full = call("get_status", full=True)
    item("PARTY hint after swaps", "INFO", grab(r"^PARTY:.*$", st_full) or "no PARTY line")
    load_verified(BASE)

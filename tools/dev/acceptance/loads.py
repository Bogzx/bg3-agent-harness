"""Section loads: the crash guard over three verified reloads."""

from __future__ import annotations

import re
import time

from .core import BASE, alive, bg3le_log, grep_crash, item, load_verified


def sec_loads() -> None:
    lg = bg3le_log()
    cnt = lambda: len(re.findall(r"cleared \(\d+ so far\)", lg.read_text(errors="replace"))) if lg else -1
    c0 = cnt()
    times = []
    for i in range(3):
        t = time.monotonic()
        ok = load_verified(BASE, record=(i == 0))
        times.append(time.monotonic() - t)
        if not alive():
            item("crash guard (3 reloads)", "FAIL", f"game died on reload {i + 1}")
            return
        if not ok:
            item("crash guard (3 reloads)", "FAIL", f"reload {i + 1} not verified")
            return
    crash = grep_crash(lg)
    item("crash guard (3 reloads)", "PASS" if not crash else "FAIL",
         f"3 verified reloads {', '.join(f'{x:.0f}s' for x in times)}; game alive; guard cleared count {c0}→{cnt()} ({lg.name if lg else 'no log'}){'; ' + crash if crash else ''}")

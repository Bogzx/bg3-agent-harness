"""S3 pickpocket fail -> crime (live1 §6, live2 §1): a steal in the owner's sight fails whatever odds the window
shows. Takes the best-odds item from the named NPC's pockets in plain view. PASS: the referee recorded a failed
pickpocket and a crime by the party; a success (the thief went unseen) is REVIEW.

    s3_pickpocket_fail.py [<npc name>=Alfira]
"""

from __future__ import annotations

from probe_pickpocket import pp_records, steal
from probelib import Probe, Verdict, arg, main


def judge(why: str, events: list[dict]) -> Verdict:
    if why:
        return "FAIL", why
    failed, took = pp_records(events, False), pp_records(events, True)
    crimes = sorted({str(e.get("type")) for e in events if e.get("ev") == "crime"})
    if failed and crimes:
        return "PASS", f"pickpocket failed ({failed[0].get('npc')}); crimes {', '.join(crimes)}"
    if took:
        return "REVIEW", f"the steal succeeded in view ({took[0].get('item')}): was the thief unseen?"
    return "FAIL", f"no failed pickpocket + crime in the referee ring (failed {len(failed)}, crimes {crimes})"


def run(p: Probe) -> Verdict:
    return judge(*steal(p, arg(p, 0, "Alfira")))


if __name__ == "__main__":
    raise SystemExit(main(run))

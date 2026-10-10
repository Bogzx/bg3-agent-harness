"""Between-session dry run of the level-up pages: read what they offer and find the spell buttons, commit nothing.

    uv run --project bridge python tools/dev/check_levelup.py [--who Gale] [--hover] [--plan '{"feat": "Alert"}']
                                                              [--keep-open] [--force]

For each party member with a pending level up (state.status `level_up`), or only --who:
  open       ui.level_up_open (the portrait/hotbar level-up button) and wait for the page.
  options    ui.level_up_options: the shown tab, class/subclass, feats, the feat panel's ability points
             (SelectedFeatDetails.FeatDetails; S018 read the wrong path and never spent a point), spell
             selectors with names, passives.
  plan       (--plan) ui.level_up with finish=false: applies the plan to the page (feat, ability points,
             passives) and reports set/problems/needs_clicks. Nothing is committed: the page is closed with
             ESCAPE afterwards, which discards it. Without --plan, a feat level tries {"feat": "Ability
             Improvement"} so the ability points are read and spent on the page (verifies SelectAbility).
  probe      for each selector's first pickable spell: ui.level_up_spell_probe (shows the selector: back from the
             feat tab to the overview, then its sub-tab) and reports index/cols/cell.
  hover      (--hover) the bridge's real search (leveling.SpellClicker.locate) moves the cursor until it is over
             that spell's button and reports where and how many probes it took. It never clicks.
  close      ESCAPE through the input shim (unless --keep-open), then checks the page is gone.

Refuses while a runner session is playing (runtime/runner.status.json) unless --force. Prints PASS/WARN/FAIL lines
and exits 1 on any FAIL.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "bridge" / "src"))

from bg3ai.game import Game  # noqa: E402
from bg3ai import leveling  # noqa: E402

RUNTIME = Path(os.environ.get("BG3AI_RUNTIME", REPO / "runtime"))
results: list[tuple[str, str, str]] = []


def report(verdict: str, name: str, detail: str = "") -> None:
    results.append((verdict, name, detail))
    print(f"{verdict:4} {name}: {detail}", flush=True)


def session_running(status_file: Path = RUNTIME / "runner.status.json", now: float | None = None) -> str | None:
    """The runner's session name while one is playing, else None."""
    try:
        st = json.loads(status_file.read_text())
    except (OSError, ValueError):
        return None
    pid = st.get("runner_pid")
    if not pid or not Path(f"/proc/{pid}").exists():
        return None
    if st.get("session_ended"):
        return None
    now = time.time() if now is None else now
    if st.get("hard_stop") and now > float(st["hard_stop"]):
        return None
    return st.get("session") or "?"


def short(v, n: int = 160) -> str:
    s = json.dumps(v, ensure_ascii=False) if not isinstance(v, str) else v
    return s if len(s) <= n else s[: n - 1] + "…"


def summarize_options(o: dict) -> list[str]:
    lines = [f"tab={o.get('tab')} step={o.get('step')} complete={o.get('complete')} class={o.get('class')}"
             f" L{o.get('class_level')}"]
    if o.get("can_feat"):
        feats = [f["name"] or f"#{f['i']}" for f in o.get("feats") or [] if not f.get("off")]
        lines.append(f"feat={o.get('feat')} panel={o.get('feat_panel')} offered={len(feats)}: {short(feats, 300)}")
        if o.get("ability_improvement"):
            lines.append(f"ability points {short(o.get('ability_points'))}: {short(o['ability_improvement'], 300)}")
    for s in o.get("spells") or []:
        lines.append(f"spells '{s.get('title')}' open={s.get('open')} picked={s.get('picked')} "
                     f"can_pick={short(s.get('can_pick'), 200)}")
    for p in o.get("passives") or []:
        lines.append(f"passives '{p.get('title')}' max={p.get('max')} picked={p.get('picked')} "
                     f"options={short(p.get('options'), 200)}")
    return lines


def wait_open(g: Game, uuid: str, timeout: float = 8.0) -> dict | None:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        o = g.try_rpc("ui.level_up_options", {"uuid": uuid}, context="client")
        if isinstance(o, dict) and o.get("open") and o.get("class"):
            return o
        time.sleep(0.3)
    return None


def check_one(g: Game, inp, row: dict, plan: dict | None, hover: bool, keep_open: bool) -> None:
    who, uuid = f"{row['name']} L{row['to']}", row["uuid"]
    try:
        g.rpc("ui.level_up_open", {"uuid": uuid}, context="client")
    except Exception as e:  # noqa: BLE001
        report("FAIL", f"{who} open", str(e))
        return
    o = wait_open(g, uuid)
    if o is None:
        report("FAIL", f"{who} open", "the level-up page did not open within 8 s")
        return
    report("PASS", f"{who} options", " | ".join(summarize_options(o)))

    if plan is None and o.get("can_feat"):
        plan = {"feat": "Ability Improvement"}
    if plan is not None:
        r = g.run_job("ui.level_up", {"uuid": uuid, "plan": plan, "finish": False}, context="client", wait_s=30)
        res = r.result if isinstance(r.result, dict) else {}
        detail = (f"status={r.status} set={short(res.get('set'), 300)} problems={short(res.get('problems'), 300)}"
                  f" needs_clicks={short(res.get('needs_clicks'))} defaulted={res.get('defaulted')}"
                  + (f" error={r.error}" if r.error else ""))
        verdict = "PASS" if r.status == "done" else "FAIL"
        if o.get("can_feat") and "Ability Improvement" in str(plan.get("feat", "")) and not (res.get("set") or {}).get("abilities"):
            verdict, detail = "FAIL", "no ability point spent on the page — " + detail
        report(verdict, f"{who} plan {short(plan, 80)}", detail)
        o2 = g.try_rpc("ui.level_up_options", {"uuid": uuid}, context="client") or {}
        if o2.get("ability_improvement"):
            report("PASS", f"{who} ability points", f"{short(o2.get('ability_points'))} {short(o2['ability_improvement'], 300)}")

    for s in (o.get("spells") or []):
        if not s.get("open") or not s.get("can_pick"):
            continue
        name = s["can_pick"][0]
        args = {"name": name, "cantrips": bool(s.get("cantrips"))}
        clicker = leveling.SpellClicker(lambda op, a: g.rpc(op, a, context="client"), inp, log=lambda m: print("     " + m),
                                        budget_s=40.0)
        try:
            p = clicker.ready(args) if inp is not None else g.rpc("ui.level_up_spell_probe", args, context="client")
        except Exception as e:  # noqa: BLE001
            report("FAIL", f"{who} probe {name}", str(e))
            continue
        report("PASS" if p.get("index") else "WARN", f"{who} probe {name}",
               f"index={p.get('index')}/{p.get('n')} cols={p.get('cols')} cell={p.get('cell')} feat={p.get('feat')}"
               f" tab={p.get('tab')}")
        if hover and inp is not None and p.get("index"):
            t0 = time.monotonic()
            try:
                kind = "feat" if p.get("feat") else ("cantrips" if s.get("cantrips") else "spells")
                at = clicker.locate(args, p, kind)
                w, h = inp.window_size()
                report("PASS", f"{who} hover {name}", f"over its button at {at} ({at[0] / w:.3f}, {at[1] / h:.3f}) "
                       f"after {clicker.probes} probes, {time.monotonic() - t0:.1f} s (not clicked)")
            except Exception as e:  # noqa: BLE001
                report("FAIL", f"{who} hover {name}", f"{e} ({time.monotonic() - t0:.1f} s)")
            inp.move(*(round(v / 2) for v in inp.window_size()))

    if keep_open:
        report("WARN", f"{who} close", "--keep-open: the page stays open (ESCAPE closes it without committing)")
        return
    if inp is None:
        report("WARN", f"{who} close", "no input shim: close the page with ESCAPE by hand")
        return
    for _ in range(3):
        inp.key("ESCAPE")
        time.sleep(0.8)
        if not g.try_rpc("ui.level_up_options", {"uuid": uuid}, context="client", default={}).get("open"):
            report("PASS", f"{who} close", "page closed, nothing committed")
            return
    report("FAIL", f"{who} close", "the page is still open after 3 ESCAPEs")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--who", help="one party member (name); default: everyone with a pending level up")
    ap.add_argument("--plan", help="JSON plan applied to the page with finish=false (never committed)")
    ap.add_argument("--hover", action="store_true", help="run the real button search (moves the cursor, no click)")
    ap.add_argument("--keep-open", action="store_true")
    ap.add_argument("--force", action="store_true", help="run even while a runner session is playing")
    ap.add_argument("--port", type=int, default=int(os.environ.get("BG3LE_DEBUG_PORT", "9998")))
    a = ap.parse_args()

    running = session_running()
    if running and not a.force:
        report("FAIL", "session", f"runner session {running} is playing: run this between sessions (or --force)")
        return 1
    g = Game(port=a.port)
    status = g.try_rpc("state.status")
    if not isinstance(status, dict):
        report("FAIL", "game", "state.status did not answer (game not loaded?)")
        return 1
    rows = leveling.pending(status)
    if a.who:
        rows = [r for r in rows if r["name"].lower().startswith(a.who.lower())]
    if not rows:
        report("WARN", "pending", "no pending level up" + (f" for {a.who}" if a.who else ""))
        return 0
    inp = None
    try:
        from bg3ai.input import InputClient
        inp = InputClient()
        inp.agent(True)
    except Exception as e:  # noqa: BLE001
        report("WARN", "input", f"input shim unreachable ({e}): no hover search, no ESCAPE")
    plan = json.loads(a.plan) if a.plan else None
    for row in rows:
        check_one(g, inp, row, plan, a.hover, a.keep_open)
    fails = sum(1 for v, _, _ in results if v == "FAIL")
    print(f"{len(results)} checks, {fails} FAIL")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())

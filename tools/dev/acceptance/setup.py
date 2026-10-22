"""Section setup: create_character pacing and the setup items."""

from __future__ import annotations

import json
import re
import sys
import time

from .core import BASE, OUT, REPO, S, call, first_line, item, load_verified, need, status, ui_screen

# create_character targets after the CC pacing round (impl-setup part 45; naut 2026-10-13 run 3: cc_apply 6.1 s,
# naming 5.2 s, 22.1 s in all; runs 1-2 needed 3 calls). Since S6 (impl-setup part 56) the tool returns once the
# world has loaded, so total_ms holds the game's ~8-12 s load (world_ms): game time, shown, not judged. Judged: our
# own phases, cc_apply + naming (with the finish press) + the first look (impl-setup part 57; naut3: 1.9 s + 1.0-1.7 s
# + ~0.3-0.6 s; records before part 57 have no first_look_ms: apply + naming).
CC_MAX_APPLY_MS, CC_MAX_NAMING_MS, CC_MAX_OWN_MS = 3000, 2500, 5000


def cc_own_ms(r: dict) -> int | None:
    """Our part of a create_character record: cc_apply + naming + first look (the world load is the game's)."""
    if r.get("apply_ms") is None or r.get("naming_ms") is None:
        return None
    return r["apply_ms"] + r["naming_ms"] + (r.get("first_look_ms") or 0)


def cc_verdict(recs: list[dict]) -> tuple[str, str]:
    """The last character creation's create_character records (runtime/stage_ms.jsonl op "create_character", one
    per call; a creation = the calls within 15 min of the last one) -> (verdict, evidence)."""
    recs = [r for r in recs if r.get("op") == "create_character" and "pacing" in r]
    if not recs:
        return "SKIP", "no create_character since the CC pacing round: needs a naut-style run (new game + CC)"
    last = [r for r in recs if r.get("t", 0) >= recs[-1].get("t", 0) - 900]
    ok = [r for r in last if r.get("ok")]
    fin = ok[-1] if ok else last[-1]
    ev = (
        f"{len(last)} call(s) ({sum(1 for r in last if not r.get('ok'))} failed"
        + "".join(f"; {str(r.get('error'))[:60]}" for r in last if not r.get("ok"))
        + f"), pacing {fin.get('pacing')}: cc_apply {fin.get('apply_ms')} ms, naming {fin.get('naming_ms')} ms "
        f"({fin.get('how')}, {fin.get('popups')} popup(s), {fin.get('naming_stages')}), first look "
        + (f"{fin['first_look_ms']} ms" if "first_look_ms" in fin else "not logged")
        + f" = ours {cc_own_ms(fin)} ms (target <= {CC_MAX_OWN_MS}); "
        f"world load {fin.get('world_ms')} ms (game time, not judged), total {fin.get('total_ms')} ms"
    )
    good = (
        len(last) == 1
        and bool(ok)
        and (fin.get("apply_ms") or 1e9) <= CC_MAX_APPLY_MS
        and (fin.get("naming_ms") or 1e9) <= CC_MAX_NAMING_MS
        and (cc_own_ms(fin) or 1e9) <= CC_MAX_OWN_MS
    )
    return ("PASS" if good else "FAIL"), ev


def sec_setup() -> None:
    """setup's fixes (impl-setup parts 34-35, 41, 45) on the loaded base save: ramwatch's anon+swap kill
    signal, the story scorer naming a new journal step by its text, enemy statuses as lesson terms; and the last
    character creation's time and attempts (read from runtime/stage_ms.jsonl, no game action)."""
    recs = []
    try:
        for ln in (REPO / "runtime" / "stage_ms.jsonl").read_text(errors="replace").splitlines():
            try:
                recs.append(json.loads(ln))
            except ValueError:
                pass
    except OSError:
        pass
    item("create_character: 1 attempt, time (last CC)", *cc_verdict(recs))
    sys.path.insert(0, str(REPO / "packages" / "harness" / "src"))
    from bg3ai import lessons, progress
    from bg3harness import ramwatch

    # 1. ramwatch: the watchdog that holds runtime/ramwatch.lock writes anon+swap (else it runs old code)
    pids = ramwatch.game_pids()
    a_s, rss = sum(ramwatch.anon_swap_mb(p) for p in pids), sum(ramwatch.rss_mb(p) for p in pids)
    # the newest TICK line (a spike line, e.g. from the save load just before, can be the file's last line)
    t_last, last = ramwatch.last_tick(REPO / "runtime" / "ram.log") or (0.0, "no anon+swap tick line in ram.log")
    fresh = time.time() - t_last < 180
    item(
        "ramwatch anon+swap kill signal",
        "PASS" if (a_s > 1000 and fresh) else "FAIL",
        f"game pids {pids}: anon+swap {a_s} MB, RSS {rss} MB (kill at {ramwatch.KILL_ANON_SWAP_MB}); ram.log: {last[-110:]}",
    )
    if not load_verified(BASE):
        return
    # 2. scorer: drop one journal step from a baseline built on the live journal → that step's text is the event
    progress._events["seq"] = None
    got = progress.read_game(lambda code, ctx: S.game.eval(code, context=ctx))
    quests = (got or {}).get("journal")
    qs = quests.get("quests", quests) if isinstance(quests, dict) else quests
    q = next((q for q in qs or [] if isinstance(q, dict) and len(q.get("steps") or []) >= 3), None)
    if need(q is not None, "scorer names the new journal step", "no quest with 3+ steps in the live journal"):
        import copy

        older = copy.deepcopy(got)
        oqs = [
            x
            for x in (
                older["journal"].get("quests", older["journal"])
                if isinstance(older["journal"], dict)
                else older["journal"]
            )
            if isinstance(x, dict) and x.get("title") == q["title"]
        ]
        # the quest's FIRST step (a count-based diff would name the last one), taken out of every quest with this
        # title (Daughter of Darkness is two journal quests: the party's and Shadowheart's own)
        gone = q["steps"][0]
        for oq in oqs:
            oq["steps"] = [x for x in oq["steps"] if progress._step_key(str(x)) != progress._step_key(str(gone))]
        seen, _ = progress.diff(None, older)
        _, evs = progress.diff(seen, got)
        whats = [e["what"] for e in evs if e["kind"] == "quest_step"]
        want = f"{q['title']}: {progress._step_key(str(gone))}"
        item(
            "scorer names the new journal step",
            "PASS" if whats == [want] else "FAIL",
            f"removed step 1 of '{q['title']}' ({len(q['steps'])} steps, {len(oqs)} quest(s) with that title); "
            f"events {whats[:3]}",
        )
    # 4. temporary followers (S053 Zanner Toobin): the flag never marks the permanent party (it would stop equip_best
    #    dressing them); a follower, if one is in the party, carries it in both reads
    inv = S.game.try_rpc("state.inventory", {}) or {}
    rows = [r for r in status().get("party") or [] if isinstance(r, dict) and not r.get("summon")]
    flagged = [r.get("name") for r in rows if r.get("follower")]
    mem_flag = [u for u, m in (inv.get("members") or {}).items() if (m or {}).get("follower")]
    origins = {
        "Claude",
        "Shadowheart",
        "Karlach",
        "Wyll",
        "Gale",
        "Astarion",
        "Lae'zel",
        "Halsin",
        "Minthara",
        "Jaheira",
        "Minsc",
    }
    wrong = [n for n in flagged if n in origins]
    item(
        "temporary followers flagged, the party not",
        "PASS" if not wrong and len(mem_flag) == len(flagged) else "FAIL",
        f"party {[r.get('name') for r in rows]}; follower-flagged {flagged or 'none'} (members read {len(mem_flag)})",
    )
    # 3. lessons: nearby characters' status names reach the lesson terms (a trigger with a plural/past ending)
    nb = S.game.try_rpc("state.nearby", {"kinds": ["characters"], "radius": 40}) or {}
    rows = [c for c in nb.get("characters") or [] if c.get("statuses")]
    if need(bool(rows), "enemy statuses as lesson terms", "no character with a status in view at the base save"):
        c = rows[0]
        sname = next(x.get("name") for x in c["statuses"] if x.get("name"))
        enemies = S._with_statuses([{"uuid": c["uuid"], "name": c.get("name"), "relation": "hostile"}])
        tmp = OUT / "rt" / "lessons-accept.md"
        tmp.parent.mkdir(parents=True, exist_ok=True)
        trig = re.sub(r"(ed|es|s)$", "", sname) if len(sname) > 5 else sname  # 'Mirrored' → 'Mirror'
        tmp.write_text(f"- WHEN {trig}: acceptance → status term (S999) [seen]\n")
        out = lessons.Store(tmp).fight_lines(enemies, [])
        item(
            "enemy statuses as lesson terms",
            "PASS" if "acceptance" in out else "FAIL",
            f"{c.get('name')} statuses {enemies[0]['statuses'][:4]}; trigger '{trig}' → {first_line(out) or 'no LESSONS'}",
        )

    # 5. set_party: unchaining a member who already walks alone is done, not FAILED (S062 ×3); chained back after
    rows = [
        r
        for r in status().get("party") or []
        if isinstance(r, dict) and not r.get("summon") and not r.get("avatar") and not r.get("dead")
    ]
    if need(bool(rows), "set_party unchain twice = done", "no living companion in the party"):
        who = rows[0].get("name")
        first, again = call("set_party", action="unchain", who=who), call("set_party", action="unchain", who=who)
        back = call("set_party", action="chain", who=who)
        item(
            "set_party unchain twice = done",
            "PASS" if again.startswith("set_party: done") and "already" in again else "FAIL",
            f"{who}: 1st {first_line(first)[:80]} | 2nd {first_line(again)[:80]} | chain {first_line(back)[:60]}",
        )
    # 6. level_up selects the member first (S062): the helper makes a companion the controlled one; given back after
    if rows:
        who = rows[0].get("uuid")
        prev = S._active()
        got = S._select_for_level_up(who)
        now = S._active()
        S.game.run_job("act.select", {"char": prev}, wait_s=5)
        time.sleep(0.3)
        item(
            "level_up selects the member first",
            "PASS" if got == prev and now == who and S._active() == prev else "FAIL",
            f"active {prev} → helper returned {got}, active then {now} (want {who}), back {S._active()}",
        )
    # 7. S11: check_all's waypoints check left the world map open (19:11 + 19:56 2026-10-17; S068 started on
    # screen=map). Its waypoints + screen rows as the runner runs them: the game view after, 2 s later too.
    sys.path.insert(0, str(REPO / "tools" / "dev"))
    import check_all as CA

    if need(ui_screen() == "game", "check_all leaves the game view", f"screen {ui_screen()} before the check"):
        wp, scr_row = CA.check_waypoints(S.game), CA.check_screen(S.game, "game")
        time.sleep(2.0)
        scr = ui_screen()
        item(
            "check_all leaves the game view",
            "PASS" if scr == "game" and scr_row.verdict != "FAIL" else "FAIL",
            f"waypoints {wp.verdict}: {wp.detail[-120:]} | screen row {scr_row.verdict}: {scr_row.detail} | "
            f"screen 2 s after={scr}",
        )
    # 8. S20 (S085): the plan check passed Claude L12 Tough, taken at L4. plan_offers.lua now reads each member's
    #    taken feats from its LevelUp history (read-only): a member past L4 has one unless all were Ability
    #    Improvements (repeatable, left out); Ability Improvement is in the repeatable list.
    import check_plans as CPL

    uuids = [r["uuid"] for r in status().get("party") or [] if isinstance(r, dict) and r.get("uuid")]
    ms = CPL.offers(S.game, uuids, 1)
    lvl = {m["name"]: sum(c.get("level") or 0 for c in m["classes"]) for m in ms}
    feats = {m["name"]: m.get("known_feats") for m in ms}
    rep = (ms[0].get("repeatable_feats") or []) if ms else []
    item(
        "plan check reads taken feats",
        "PASS" if "Ability Improvement" in rep and any(feats[n] for n in feats if lvl[n] >= 4) else "FAIL",
        f"levels {lvl}; taken feats {feats}; repeatable {rep[:6]}",
    )

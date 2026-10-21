"""Section combat: a fight with the shadow pack W of the Moonrise road (combat_turn, end_turn, melee reach)."""

from __future__ import annotations

import json
import re
import time

from .core import BASE, S, call, first_line, item, load_verified, log, need, status, took, ui_screen

SHADOW_PACK = (-236.0, 31.0, 12.0)  # Wraith, Shadow Mastiff, Shadows and Shadow-Cursed townsfolk W of the Moonrise road


def counted(name: str, **kw) -> tuple[str, float, dict]:
    """A tool call with every RPC it makes counted by op (job push: one job.poll per short job)."""
    counts: dict[str, int] = {}
    orig = S.game.rpc

    def counting(op, *a, **k):
        counts[op] = counts.get(op, 0) + 1
        return orig(op, *a, **k)

    S.game.rpc = counting
    try:
        out, dt = took(name, **kw)
    finally:
        S.game.rpc = orig
    return out, dt, counts


def sec_combat() -> None:
    """A real fight (reloaded after): the shadow pack W of the Moonrise road. Opens it by walking into their sight,
    then plays party turns with combat_turn: melee approach + attack by the SHOWN ability name, the RPC count with
    the job push, and the names in the action lists."""
    if not load_verified(BASE):
        return
    from bg3ai import jobpush

    jp = jobpush.shared(S.game.host, S.game.port)
    jp.ensure()
    ft = call("fast_travel", waypoint="Moonrise Towers")
    if not need(
        "FAILED" not in ft, "combat_turn on a safe fight", "fast_travel Moonrise Towers failed: " + first_line(ft)
    ):
        load_verified(BASE)
        return
    call("travel_to", x=SHADOW_PACK[0] + 18.0, y=SHADOW_PACK[1], z=SHADOW_PACK[2] + 18.0)
    end = time.monotonic() + 120
    cs = {}
    while time.monotonic() < end:
        cs = S.game.try_rpc("state.combat") or {}
        if cs.get("active"):
            break
        w = call("wait_for", target="queue", timeout_s=20, lead_s=0)
        if "still waiting" not in w:
            break
    cs = S.game.try_rpc("state.combat") or {}
    opener_u = None
    if not cs.get("active"):
        # The pack doesn't notice a party walking up (live 12:5x): open the fight like a player, with an attack.
        nb = S.game.try_rpc("state.nearby", {"radius": 40}) or {}
        chars = nb.get("characters") or []
        log("in view: " + "; ".join(f"{c.get('name')} {c.get('relation')} dead={c.get('dead')}" for c in chars[:12]))
        foes = [c for c in chars if c.get("relation") == "hostile" and not c.get("dead")]
        if foes:
            opener_u = foes[0]["uuid"]
            fh = S.H.get(foes[0]["uuid"], foes[0].get("name"))
            for _ in range(3):
                ua = call("use_ability", ability="Ranged Attack", target=fh, who="Claude")
                said = re.search(
                    r"(?m)^(COMBAT: the attack started a fight.*|combat: (NOT started|none|not started).*)$", ua
                )
                if "FAILED" not in first_line(ua):
                    item(
                        "opener says whether combat started",
                        "PASS" if said else "FAIL",
                        said.group(1)[:160] if said else first_line(ua),
                    )
                time.sleep(2.0)
                cs = S.game.try_rpc("state.combat") or {}
                dbg = S.game.eval(
                    f'local h=Osi.GetHostCharacter() local u="{foes[0]["uuid"]}" return "host combat="'
                    '..tostring(Osi.IsInCombat(h)).." foe combat="..tostring(Osi.IsInCombat(u))'
                    '.." enemy="..tostring(Osi.IsEnemy(u,h)).." hp="..tostring(Osi.GetHitpoints(u))'
                    '.." canfight="..tostring(Osi.CanFight and Osi.CanFight(u))',
                    context="server",
                )
                log(f"after the attack: state.combat active={cs.get('active')}; {dbg}")
                if cs.get("active"):
                    break
    if not need(bool(cs.get("active")), "combat_turn on a safe fight", "no fight started walking to the shadow pack"):
        load_verified(BASE)
        return
    gc = call("get_combat_state")
    ids = re.findall(r"\((?:Target|Projectile|Shout|Zone|Rush)_\w+\)", gc)
    named = re.findall(r"(?m)^\s*(?:ACTIONS|actions).*$", gc)
    item(
        "ability names in the action lists",
        "PASS" if not ids else "FAIL",
        f"{len(gc)} chars, ids shown={ids[:4]}; {named[0][:160] if named else first_line(gc)}",
    )
    from bg3ai import turn as T

    played = 0
    bg_ends: list[tuple] = []  # (who, combat_turn s, background end s after it returned, queue text, turn passed)
    rts: list[tuple] = []  # (metrics record, single state.combat RPCs, FAILED lines) per combat_turn (batched checks)
    ct_raw: list[str] = []  # the raw combat_turn results (vis combat compaction check)
    probed = False
    end = time.monotonic() + 150
    while time.monotonic() < end and played < 3:
        cs = S.game.try_rpc("state.combat") or {}
        if not cs.get("active"):
            break
        if ui_screen() == "reaction":
            call("react", use=False)
            continue
        st = status()
        party = {r.get("uuid"): r for r in st.get("party") or []}
        now = [u for u in T.acting(cs) if u in party]
        if not now:  # enemies acting
            time.sleep(0.5)
            continue
        if not probed and len(now) >= 2:
            probed = end_turn_probe(cs, now, party)
            if probed:
                # Live window 2026-10-16: the probe ends the whole group (avatar included); 0.2 s later the next loop
                # still saw the group acting and played Claude's turn into the hand-over (a 9 m move out of turn, then
                # "not this character's turn"). Wait for the game to hand the turn on before planning one.
                t_hand = time.monotonic()
                while time.monotonic() - t_hand < 15:
                    cs2 = S.game.try_rpc("state.combat") or {}
                    if not cs2.get("active") or not set(now) & set(T.acting(cs2)):
                        break
                    time.sleep(0.3)
                log(f"end_turn probe: group handed on after {time.monotonic() - t_hand:.1f}s")
                continue
        who_u = now[0]
        nb = S.game.try_rpc("state.nearby", {"radius": 40}) or {}
        foes = [c for c in nb.get("characters") or [] if c.get("relation") == "hostile" and not c.get("dead")]
        here = party[who_u].get("pos") or [0, 0, 0]
        if foes:
            foe = min(foes, key=lambda c: sum((a - b) ** 2 for a, b in zip(c.get("pos") or [9e9] * 3, here)))
        elif opener_u:
            # Nothing in view (a Shadow in Shadow Blend): plan on the opener's target anyway, like an agent
            # planning on what it last saw; combat_turn must re-aim or say nobody is in sight (round 2).
            foe = {"uuid": opener_u, "name": "opener target (unseen)"}
        else:
            call("wait", seconds=3)
            continue
        fh = S.H.get(foe["uuid"], foe.get("name"))
        plan = json.dumps(
            {
                "move": {"target": fh, "within": 1.5},
                "on_reaction": "skip",
                "end": True,
                "action": [
                    {"ability": "Main Hand Attack", "target": fh},
                    {"ability": "Ranged Attack", "target": fh},
                    "Dash",
                ],
            }
        )
        recs: list[dict] = []
        orig_td = S.METRICS.tool_done

        def tool_done(tool, *a, **k):  # the combat_turn's own metrics record (rt_n / watch_n)
            r = orig_td(tool, *a, **k)
            if tool == "combat_turn" and r and not r.get("nested"):
                recs.append(r)
            return r

        S.METRICS.tool_done = tool_done
        try:
            out, dt, counts = counted("combat_turn", plan=plan, who=S.H.get(who_u))
        finally:
            S.METRICS.tool_done = orig_td
        if recs:
            rts.append((recs[-1], counts.get("state.combat", 0), out.count("FAILED")))
        ct_raw.append(out)
        log(f"RPCs by op: {dict(sorted(counts.items(), key=lambda kv: -kv[1]))}")
        if "end_turn: ending in the background" in out:  # latency round 2026-10-16 (S.END_ASYNC)
            b0 = time.monotonic()
            while S._queues().busy(who_u) and time.monotonic() - b0 < S.END_BG_HOLD_S:
                time.sleep(0.1)
            q = S._last_queue_text(who_u)
            cs2 = S.game.try_rpc("state.combat") or {}
            bg_ends.append(
                (
                    party[who_u].get("name"),
                    dt,
                    time.monotonic() - b0,
                    q,
                    who_u not in T.acting(cs2) or not cs2.get("active"),
                )
            )
        jobs = sum(v for k, v in counts.items() if k.startswith("act."))
        polls = counts.get("job.poll", 0)
        rpcs = sum(counts.values())
        # "move eN: FAILED — beyond remaining movement … → partial 8.7m toward it: done" is a move too (live window
        # 2026-10-16: Claude walked 9 m and the check said moved=False)
        moved = bool(re.search(r"(?m)^move \S+: (?:done|FAILED[^\n]*→ partial [\d.]+m[^\n]*: done)", out))
        hit = bool(re.search(r"(MainHandAttack|Main Hand Attack|Ranged Attack) e\d+: done", out))
        dashed_on = bool(re.search(r"Shout_Dash: done", out)) and len(re.findall(r"(?m)^move ", out)) >= 2
        unknown = "has no action called 'Main Hand Attack'" in out  # Ranged Attack: only with a ranged weapon
        # round 2 fixes: no melee sent into a sure range miss after a short move; no Dash walking toward a target
        # that vanished (re-aimed at the nearest visible hostile instead)
        range_miss = bool(re.search(r"MainHandAttack \S+: FAILED — target is out of range", out))
        lost_walk = "move " in out and "target is not visible; last seen" in out
        played += 1
        ok = (
            not unknown
            and (moved or hit)
            and polls <= 2 * jobs + 2
            and "EXCEPTION" not in out
            and "FAILED: it is not" not in out
            and not range_miss
            and not lost_walk
            and "unknown plan key" not in out
        )  # act 2026-10-11: "end" inside a plan (S041 22:36:18)
        if "melee is out of reach" in out or "the plan now aims at" in out:
            log(
                "round-2 lines: "
                + " | ".join(
                    l for l in out.splitlines() if "melee is out of reach" in l or "now aims" in l or "skipped (" in l
                )
            )
        item(
            f"combat_turn #{played} (melee approach, names, job push)",
            "PASS" if ok else "FAIL",
            f"{party[who_u].get('name')} {dt:.1f}s; {rpcs} RPCs ({jobs} act jobs, {polls} job.polls; push heard={jp.seen}); "
            f"moved={moved} attacked={hit} dash+walk={dashed_on} range_miss={range_miss} lost_walk={lost_walk}; vs {foe.get('name')}; "
            + " | ".join(l for l in out.splitlines()[:4]),
        )
    if not played:
        item("combat_turn on a safe fight", "SKIP", "the fight ended before a party turn")
    if not probed:
        item(
            "end_turn after a companion's request (probe)",
            "SKIP",
            "no shared turn group with the avatar in the turns played (the party's initiatives differed)",
        )
    # latency (2026-10-16): the party's last end before enemy turns runs in the background; it must still end the turn
    # ("end_turn: done", incl. the HUD fallback) and the turn must pass. The numbers = what the agent no longer waits.
    if not bg_ends:
        item(
            "combat_turn background end",
            "SKIP",
            f"no background end (END_ASYNC={S.END_ASYNC}; shared group or the next turn ours each time)",
        )
    else:
        ok = all("end_turn" in q and "STOPPED" not in q and ": done" in q and passed for _, _, _, q, passed in bg_ends)
        item(
            "combat_turn background end",
            "PASS" if ok else "FAIL",
            "; ".join(
                f"{n}: returned {d:.1f}s, end done {b:.1f}s later, passed={p}, {q.splitlines()[-1][:90] if q else '-'}"
                for n, d, b, q, p in bg_ends
            ),
        )
    # act 2026-10-16 "combat_turn serial RPCs": the step checks ride on the watcher's read after each step (one round
    # trip with its events + act.watch); a read nobody can have changed is reused. Single state.combat RPCs left: the
    # end_turn tool's own, the NEXT line's, one after each failed alternative (the start one is in a multi).
    if rts:
        from bg3ai import actq

        ok = actq.BATCH_CHECKS and all("rt_n" in r and n <= 3 + f for r, n, f in rts)
        item(
            "combat_turn batched step checks",
            "PASS" if ok else "FAIL",
            f"BATCH_CHECKS={actq.BATCH_CHECKS}; "
            + "; ".join(
                f"rt_n={r.get('rt_n')} rpc_n={r.get('rpc_n')} watch_n={r.get('watch_n', 0)} single state.combat={n} "
                f"failed={f} ms-job={r['ms'] - r.get('job_ms', 0):.0f}"
                for r, n, f in rts
            ),
        )
    combat_compaction_check([gc] + ct_raw)
    melee_probe()
    load_verified(BASE)


def combat_compaction_check(raws: list[str]) -> None:
    """vis 2026-10-16 combat compaction (compact.py COMBAT_NARRATIVE .. KIND_GROUP) on this fight's live results:
    0 lost facts (compact_check), smaller, an enemy turn's cast + damage on one line, no @(x,y,z) on move lines."""
    from bg3ai import compact
    from bg3ai import compact_check as cc

    comp, shown, probs, a, b, outs = compact.Compactor(), [], [], 0, 0, []
    for i, raw in enumerate(r for r in raws if r):
        tool = "get_combat_state" if i == 0 else "combat_turn"
        out = comp.apply(tool, raw)
        probs += [f"#{i} {p}" for p in cc.check(raw, out, comp.elided, "\n".join(shown))]
        shown.append(out)
        outs.append(out)
        a, b = a + cc.est_tokens(raw), b + cc.est_tokens(out)
    if len(outs) < 2:
        item("vis: combat compaction keeps every fact (live)", "SKIP", "no combat_turn result")
        return
    text = "\n".join(outs[1:])
    narr = re.findall(r"(?m)^\s+(?:turn|cast) e\d+[^:\n]*: [^\n]* -> e\d+[^\n]*$", text)
    pos = [ln for ln in text.split("\n") if ln.startswith("move ") and "@(" in ln] if compact.COMBAT_MOVE_POS else []
    item(
        "vis: combat compaction keeps every fact (live)",
        "PASS" if not probs and b < a and not pos else "FAIL",
        f"{len(outs) - 1} combat_turn: tokens ~{a}->~{b} ({(b - a) / max(1, a):+.0%}); narrative lines "
        f"{len(narr)} e.g. {narr[:2]}; move lines with @: {pos[:1]}; problems {probs[:3]}",
    )


def end_turn_probe(cs: dict, now: list[str], party: dict) -> bool:
    """act proposal "ignored EndTurn -> HUD at once" (naut runs 1/3: after Lae'zel's end was requested every Osiris
    EndTurn on the avatar was ignored: 3 s Lua timeout, then the HUD's End Turn in ~0.4 s; run 2's opposite order
    worked). In a shared turn group with the avatar: end a companion first, then the avatar by Osiris EndTurn alone
    (act.end_turn, no HUD fallback): ignored or not, how long; if ignored, the HUD ends it (timed). With
    BG3AI_END_HUD_FIRST=1 the end_turn tool itself must press the HUD at once. Decision for the live window:
    ignored -> flip END_HUD_FIRST on (server.END_HUD_FIRST / env), else keep it off. False: no shared group here."""
    from bg3ai.game import JobResult

    host = str(S.game.eval("return tostring(Osi.GetHostCharacter())", context="server") or "")[-36:]
    if host not in now:
        log(f"end_turn probe: the group {[party[u].get('name') for u in now]} has no avatar ({host})")
        return False
    comp = next(u for u in now if u != host)
    cn, an = party[comp].get("name"), party[host].get("name")
    t = time.monotonic()
    r1 = S.game.run_job("act.end_turn", {"char": comp}, wait_s=15)
    d1 = time.monotonic() - t
    r1s = f"{cn} end {r1.status} {d1:.1f}s requested={(r1.result or {}).get('requested')}"
    if r1.status != "done":
        item("end_turn after a companion's request (probe)", "FAIL", f"{r1s}: {r1.error}")
        return True
    cs1 = S.game.try_rpc("state.combat") or {}
    peer = S._peer_requested(cs1, host)
    if S.END_HUD_FIRST:
        out, dt = took("end_turn", who=S.H.get(host), wait=False)
        ok = "the HUD's End Turn at once" in out and "FAILED" not in out
        item(
            "end_turn after a companion's request (probe)",
            "PASS" if ok else "FAIL",
            f"END_HUD_FIRST on; {r1s}; peer seen={bool(peer)}; {an} end_turn {dt:.1f}s: {first_line(out)[:160]}",
        )
        return True
    t = time.monotonic()
    r2 = S.game.run_job("act.end_turn", {"char": host}, wait_s=15)
    d2 = time.monotonic() - t
    ignored = r2.status == "failed" and S.END_IGNORED in str(r2.error or "")
    hud = ""
    if ignored:
        t = time.monotonic()
        r3 = S._end_turn_by_hud(host, JobResult("failed", None, r2.error))
        hud = f"; HUD End Turn {r3.status} {time.monotonic() - t:.1f}s"
    if r2.status == "done":
        # act 2026-10-16 (guard turn_over): right after the group's end the game may still show the avatar's turn
        # open; a move then must be refused, not walked out of turn (live window 18:45: 9 m free move).
        p = party[host].get("pos") or [0, 0, 0]
        t = time.monotonic()
        rm = S.game.run_job("act.move_to", {"char": host, "pos": [p[0] + 1.5, p[1], p[2]]}, wait_s=8)
        refused = rm.status == "failed" and "turn" in str(rm.error or "")
        item(
            "no move right after the group's end (guard turn_over)",
            "PASS" if refused else "FAIL",
            f"{an} act.move_to {time.monotonic() - t:.1f}s after its end: {rm.status} {rm.error or rm.result}",
        )
    decision = (
        "IGNORED: flip END_HUD_FIRST on (BG3AI_END_HUD_FIRST=1), saves ~3 s per such end"
        if ignored
        else "not ignored: keep END_HUD_FIRST off"
    )
    item(
        "end_turn after a companion's request (probe)",
        "PASS" if r2.status == "done" or ignored else "FAIL",
        f"{r1s}; peer seen={bool(peer)}; {an} Osiris EndTurn {r2.status} {d2:.1f}s ({r2.error or 'ok'}){hud} -> {decision}",
    )
    return True


MELEE_PROBE_M = (2.4, 2.1, 1.9, 1.6)  # centre-to-centre distances swung from, one party turn each


def melee_probe() -> None:
    """ACT-3 (S065 ×9 'first melee refused, no reason given'): the centre distance the engine accepts for a melee
    swing cast WithoutMove (use_ability in combat). Our guard lets one through up to weapon range + both Stand radii
    (2.5 m for two medium creatures); combat_turn's melee move stops at ≤2.1 m. One swing per party turn: walk to d m
    from the nearest visible hostile (on the line toward it), measure, swing, end the turn. Record-only (PASS when
    measured): the rows say where the engine's reach really ends; set G.radius / A.MELEE_STOP_SLACK from them."""
    from bg3ai import turn as T

    rows: list[str] = []
    todo = list(MELEE_PROBE_M)
    end = time.monotonic() + 150
    while todo and time.monotonic() < end:
        cs = S.game.try_rpc("state.combat") or {}
        if not cs.get("active"):
            break
        if ui_screen() == "reaction":
            call("react", use=False)
            continue
        party = {r.get("uuid"): r for r in status().get("party") or []}
        now = [u for u in T.acting(cs) if u in party and not party[u].get("summon")]
        if not now:
            time.sleep(0.5)
            continue
        who_u, wh = now[0], S.H.get(now[0])
        here = party[who_u].get("pos")
        nb = S.game.try_rpc("state.nearby", {"radius": 30}) or {}
        foes = [
            c
            for c in nb.get("characters") or []
            if c.get("relation") == "hostile" and not c.get("dead") and c.get("pos")
        ]
        if not here or not foes:
            call("end_turn", who=wh, wait=False)
            continue
        foe = min(foes, key=lambda c: sum((a - b) ** 2 for a, b in zip(c["pos"], here)))
        fp, fh = foe["pos"], S.H.get(foe["uuid"], foe.get("name"))
        d = todo[0]
        flat = ((here[0] - fp[0]) ** 2 + (here[2] - fp[2]) ** 2) ** 0.5 or 1.0
        spot = (fp[0] + (here[0] - fp[0]) * d / flat, here[1], fp[2] + (here[2] - fp[2]) * d / flat)
        call("move_to", x=spot[0], y=spot[1], z=spot[2], who=wh, wait=True)
        me = next((r.get("pos") for r in status().get("party") or [] if r.get("uuid") == who_u), None)
        nb2 = S.game.try_rpc("state.nearby", {"radius": 30}) or {}
        fp2 = next((c.get("pos") for c in nb2.get("characters") or [] if c.get("uuid") == foe["uuid"]), fp)
        dist = sum((a - b) ** 2 for a, b in zip(me or here, fp2)) ** 0.5
        out = call("use_ability", ability="Target_MainHandAttack", target=fh, who=wh)
        first = first_line(out)
        verdict = (
            "swung"
            if ": done" in first or "done —" in first
            else "ENGINE-REFUSED"
            if "no reason given" in out
            else "guard-refused"
            if "out of range" in out
            else "other: " + first[:80]
        )
        rows.append(f"aimed {d}m measured {dist:.2f}m centre → {verdict}")
        log(f"melee probe: {rows[-1]} ({party[who_u].get('name')} vs {foe.get('name')})")
        todo.pop(0)
        call("end_turn", who=wh, wait=False)
    item(
        "melee reach probe (engine-accepted centre distance, ACT-3)",
        "PASS" if rows else "SKIP",
        "; ".join(rows) if rows else "no party turn with a hostile in view",
    )

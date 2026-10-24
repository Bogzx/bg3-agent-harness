"""Section strict: bg3le's strict frame lock across loads and a long rest (S17)."""

from __future__ import annotations

import time

from .core import BASE, HOTBAR_SAVE, S, alive, bg3le_log, first_line, grep_crash, item, load_verified, log, need, took


def strict_lock_probe() -> None:
    """audit UI-5 (patch 0010): the running bg3le has the strict frame-lock mode; the counters read. Then one strict
    minute: strict on, a burst of UI reads (ui.state x40), strict back to what it was (in a finally: the mode is
    process-wide, a load doesn't reset it). PASS = no crash, the reads answer (a refusal reads as a missing field, not
    an error), refusals counted. Needs the rebuilt libbg3le.so. Runs LAST in the last section: a crash costs nothing."""
    try:
        before = S.game.eval(
            "local f = Ext._Internal.UiStrict if not f then return 'missing' end "
            "local on, n = f() local _, lt, ut = Ext._Internal.UiSerialize() "
            "return tostring(on) .. ' ' .. n .. ' ' .. lt .. ' ' .. ut",
            context="client",
        )
    except Exception as e:
        before = f"eval error {e}"
    if not need(
        before.split()[0] in ("true", "false"),
        "bg3le strict frame lock (UI-5, patch 0010)",
        f"UiStrict: {before} (the game runs a bg3le without 0010: relaunch after tools/build_bg3le.sh)",
    ):
        return
    was = before.split()[0]
    errs, t, after = 0, time.monotonic(), "not restored"
    try:
        S.game.eval("Ext._Internal.UiStrict(true) return 1", context="client")
        for _ in range(40):
            u = S.game.try_rpc("ui.state", context="client")
            errs += 0 if isinstance(u, dict) and u.get("screen") else 1
    finally:
        dt = time.monotonic() - t
        try:
            after = S.game.eval(
                f"Ext._Internal.UiStrict({was}) local on, n = Ext._Internal.UiStrict() "
                "local _, lt, ut = Ext._Internal.UiSerialize() "
                "return tostring(on) .. ' ' .. n .. ' ' .. lt .. ' ' .. ut",
                context="client",
            )
        except Exception as e:
            after = f"restore failed: {e}"
    lg = bg3le_log()
    crash = grep_crash(lg)
    ok = alive() and not crash and errs <= 2 and after.split()[0] == was
    item(
        "bg3le strict frame lock (UI-5, patch 0010)",
        "PASS" if ok else "FAIL",
        f"strict/refused/lua_timeouts/ui_timeouts before={before} after={after}; 40 ui.state in {dt:.1f}s, "
        f"{errs} without a screen{'; ' + crash if crash else ''}",
    )


STRICT_ITEM = "strict frame lock across loads + rest (S17)"
STRICT_LOADS = (HOTBAR_SAVE, BASE)


def strict_counters(set_to: bool | None = None) -> dict:
    """bg3le's strict frame lock (patch 0010), optionally switched first: {strict, refused, lua_timeouts,
    ui_timeouts} or {error}."""
    arg = "" if set_to is None else ("true" if set_to else "false")
    try:
        parts = S.game.eval(
            "local f = Ext._Internal and Ext._Internal.UiStrict if type(f) ~= 'function' then "
            f"return 'missing' end local on, n = f({arg}) local _, lt, ut = Ext._Internal.UiSerialize() "
            "return tostring(on) .. ' ' .. n .. ' ' .. lt .. ' ' .. ut",
            context="client",
        ).split()
    except Exception as e:
        return {"error": f"eval error {e}"}
    if len(parts) != 4:
        return {"error": " ".join(parts) or "no reply"}
    return {
        "strict": parts[0] == "true",
        "refused": int(parts[1]),
        "lua_timeouts": int(parts[2]),
        "ui_timeouts": int(parts[3]),
    }


def strict_delta(a: dict, b: dict) -> str:
    return " ".join(f"{k} +{b.get(k, 0) - a.get(k, 0)}" for k in ("refused", "lua_timeouts", "ui_timeouts"))


def sec_strict() -> None:
    """S17 (crashes S072/S077/S078, all ~7 s after the hover-click on the rest/travel [ForceUpdate] box, 99/189/33
    unserialized Lua UI calls in those launches; L8: 0 refusals in steady reads, the timeouts come from launches and
    loads): strict ON across 2 load_game + camp + long_rest (the box-click path), counters per step, strict back to
    what it was in a finally. PASS = the game lives, both loads verify, the rest is done, strict restored; refusals
    are evidence for the turn-on decision, not a FAIL. Last section: a crash here costs nothing else."""
    c0 = strict_counters()
    if not need("error" not in c0, STRICT_ITEM, f"UiStrict: {c0.get('error')} (bg3le without patch 0010?)"):
        return
    steps, ok = [], False
    try:
        cur = strict_counters(True)
        # BASE last: it stands at camp. live9 S17: S065-end last, and its Lower City spot blocks camp travel
        # (Location, the game's own block: S065 12:00) so the rest never ran
        for label in STRICT_LOADS:
            loaded = load_verified(label)
            nxt = strict_counters()
            steps.append(f"load {label}: {'ok' if loaded else 'FAILED'} ({strict_delta(cur, nxt)})")
            cur = nxt
            if not loaded:
                break
        else:
            out, _ = took("camp", action="go")
            time.sleep(2)
            rest, rdt = took("long_rest")
            if "partial" in rest.lower() and "FAILED" in rest:
                rest, rdt = took("long_rest", partial=True)
            nxt = strict_counters()
            rested = ("done" in rest or "rested" in rest) and "FAILED" not in rest
            steps.append(
                f"camp {first_line(out)[:40]}; long_rest {rdt:.1f}s {first_line(rest)[:80]} ({strict_delta(cur, nxt)})"
            )
            ok = rested
    finally:
        end = strict_counters(c0["strict"])
    crash = grep_crash(bg3le_log())
    ok = ok and alive() and not crash and end.get("strict") == c0["strict"]
    log(f"strict counters before {c0}, after {end}")
    item(
        STRICT_ITEM,
        "PASS" if ok else "FAIL",
        f"{' | '.join(steps)}; total {strict_delta(c0, end)}; restored strict={end.get('strict')}"
        + (f"; {crash}" if crash else ""),
    )

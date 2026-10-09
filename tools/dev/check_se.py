"""Live checks for se's bg3le patches (patches/bg3le) and AutoSkip, against the running game.

    uv run --project bridge python tools/dev/check_se.py [--seconds 60] [--polls 300] [--answer]
                                                        [--serialize] [--only hook,stress,leak,instantiate,autoskip]

Prints one PASS / WARN / FAIL line per check (plus details) and exits 1 if anything FAILed.

  hook         the running bg3 loaded build/bg3le/dist/libbg3le.so (sha256 = BUILDINFO), the patched
               Ext.UI is in it (Ext._Internal.UiSerialize), and its log shows View::Update hooked
               ("noesis: View at"), no "not hooked" line, and no crash.
  stress       the S001 crash repro: ui.state / ui.dump in a tight loop on one connection while the input
               shim opens and closes panels (i, m, l, ESCAPE) and dialogue lines are advanced; the game must
               survive, and "Noesis object is gone" errors are counted. --answer also picks answer 1 when a
               dialogue shows options (this changes the playthrough: only on a throwaway save).
               --serialize runs it with Ext._Internal.UiSerialize(true) (patch 0002; on by default since 0003)
               and restores it after.
  leak         N polls of ui.state + ui.dump, then a full GC: the live Noesis boxes and Lua memory must come
               back to about where they started (references are only held while Lua holds a proxy).
  instantiate  Ext.UI.RegisterType + Instantiate (MCM's path): a property round trip, then the context is
               held by a real element's object property (ContentRoot.Tag, the same Ptr<BaseComponent> path
               as DataContext) and the old value restored.
  autoskip     autoskip.status answers, AutoSkip is on and error-free, its movie probe is cheap, the
               bridge's skipper takes the fast path, and a movie on screen is gone within 3 s.

Never builds anything, never saves or loads. The stress check only presses panel keys and ESCAPE while the
screen is the plain game view.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import statistics
import subprocess
import sys
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "bridge" / "src"))

from bg3ai.game import Game, GameError, NotReady  # noqa: E402

NO_KEYS = ("dialogue", "main_menu", "loading", "movie", "game_over", "?")
RUNTIME = Path(os.environ.get("BG3AI_RUNTIME", REPO / "runtime"))
DIST = REPO / "build" / "bg3le" / "dist"

results: list[tuple[str, str, str]] = []


def report(verdict: str, name: str, detail: str = "") -> None:
    results.append((verdict, name, detail))
    print(f"{verdict:4} {name}: {detail}", flush=True)


# ---- helpers ---------------------------------------------------------------------------------------------

def game_pid() -> int | None:
    try:
        out = subprocess.run(["pgrep", "-x", "bg3"], capture_output=True, text=True).stdout.split()
        return int(out[0]) if out else None
    except (OSError, ValueError):
        return None


def alive(pid: int | None) -> bool:
    return pid is not None and Path(f"/proc/{pid}").exists()


def rss_mb(pid: int | None) -> float | None:
    try:
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) / 1024
    except (OSError, ValueError, IndexError):
        pass
    return None


def latest_log(runtime: Path = RUNTIME) -> Path | None:
    logs = sorted((runtime / "logs").glob("bg3le-*.log*"), key=lambda p: p.stat().st_mtime)
    return logs[-1] if logs else None


def check_log_text(text: str) -> tuple[str, str]:
    """Verdict for a bg3le log: the View::Update hook, and crashes."""
    if re.search(r"crash: signal \d+", text):
        return "FAIL", "log has a crash: " + re.search(r".*crash: signal.*", text).group(0).strip()[:160]
    if "View::Update not hooked" in text:
        return "FAIL", "View::Update not hooked: releases run on the Lua thread and UiSerialize cannot work"
    if "noesis: View at" not in text:
        return "WARN", "no 'noesis: View at' yet (no UI frame seen; is the game past the splash?)"
    m = re.search(r"noesis: (\d+) of (\d+) View input entry points hooked", text)
    if m is None:
        return "WARN", "View::Update hooked, but no input hooks in the log (bg3le without patch 0003?)"
    if m.group(1) != m.group(2):
        return "WARN", f"only {m.group(1)} of {m.group(2)} View input entry points hooked: hit tests can race Lua"
    return "PASS", "View::Update and input hooked, no crash in the log"


def loaded_lib(pid: int | None) -> Path | None:
    try:
        for line in Path(f"/proc/{pid}/maps").read_text().splitlines():
            if line.endswith("libbg3le.so"):
                return Path(line.split(None, 5)[-1])
    except OSError:
        pass
    return None


def buildinfo_sha(dist: Path = DIST) -> str | None:
    try:
        m = re.search(r"^sha256: (\w+)", (dist / "BUILDINFO").read_text(), re.M)
        return m.group(1) if m else None
    except OSError:
        return None


def lua(g: Game, code: str, timeout: float = 10.0):
    """Runs a client chunk that returns a JSON string; returns the decoded value."""
    raw = g.eval(code, context="client", timeout=timeout)
    return json.loads(raw) if raw not in ("", "nil") else None


# Live Noesis boxes: values of the weak registry cache whose metatable is the patched binding's.
BOXES_LUA = r"""
local function boxes()
  if not (debug and debug.getregistry) then return -1 end
  local n = 0
  for _, t in pairs(debug.getregistry()) do
    if type(t) == "table" then
      local mt = getmetatable(t)
      if type(mt) == "table" and mt.__mode == "v" then
        for _, v in pairs(t) do
          if type(v) == "userdata" and getmetatable(v) == "NoesisObject" then n = n + 1 end
        end
      end
    end
  end
  return n
end
collectgarbage() collectgarbage()
return string.format('{"kb":%d,"boxes":%d}', math.floor(collectgarbage("count")), boxes())
"""


# ---- checks ----------------------------------------------------------------------------------------------

def check_hook(g: Game) -> None:
    pid = game_pid()
    if not alive(pid):
        report("FAIL", "hook", "bg3 is not running")
        return
    lib = loaded_lib(pid)
    want = buildinfo_sha()
    if lib is None:
        report("FAIL", "hook.lib", "libbg3le.so is not mapped in bg3 (launched without bg3le?)")
    else:
        have = hashlib.sha256(lib.read_bytes()).hexdigest() if lib.exists() else None
        if want and have == want:
            report("PASS", "hook.lib", f"{lib} = BUILDINFO sha256 {want[:12]}")
        else:
            report("WARN", "hook.lib", f"{lib} sha256 {str(have)[:12]} != BUILDINFO {str(want)[:12]} "
                   "(rebuilt since launch? relaunch to load it)")
    try:
        st = lua(g, 'local f = Ext._Internal and Ext._Internal.UiSerialize\n'
                    'if not f then return \'{"patched":false}\' end\n'
                    'local on, timeouts, ui, gen, leaked = f()\n'
                    'return string.format(\'{"patched":true,"on":%s,"timeouts":%d,"ui_timeouts":%d,'
                    '"generation":%d,"leaked":%d}\', tostring(on), timeouts, ui or -1, gen or -1, leaked or -1)')
    except (GameError, NotReady) as e:
        report("FAIL", "hook.patched", f"client eval failed: {e}")
        st = None
    if st is not None:
        if st.get("patched"):
            detail = (f"UiSerialize on={st['on']}, Lua timeouts={st['timeouts']}, UI timeouts={st['ui_timeouts']}, "
                      f"generation={st['generation']}, leaked={st['leaked']}")
            if st["generation"] < 0:
                report("WARN", "hook.patched", detail + " (bg3le without patch 0003: releases are not generation-gated)")
            elif not st["on"]:
                report("WARN", "hook.patched", detail + " (serializing is off: BG3LE_UI_SERIALIZE=0?)")
            else:
                report("PASS", "hook.patched", detail)
        else:
            report("FAIL", "hook.patched", "Ext._Internal.UiSerialize missing: the game runs an unpatched bg3le")
    log = latest_log()
    if log is None:
        report("WARN", "hook.log", f"no bg3le log under {RUNTIME}/logs")
        return
    verdict, detail = check_log_text(log.read_text(errors="replace"))
    report(verdict, "hook.log", f"{detail} ({log.name})")


def check_stress(g: Game, seconds: float, answer: bool, serialize: bool) -> None:
    from bg3ai.input import InputClient

    pid = game_pid()
    try:
        inp = InputClient()
        inp.agent(True)
    except Exception as e:
        report("FAIL", "stress", f"input shim unreachable: {e}")
        return
    serialize_was = True
    if serialize:
        try:
            serialize_was = g.eval("return tostring(Ext._Internal.UiSerialize())", context="client") == "true"
            g.eval("Ext._Internal.UiSerialize(true) return 'ok'", context="client")
        except (GameError, NotReady) as e:
            report("FAIL", "stress.serialize", f"cannot switch UiSerialize on: {e}")
            return

    stop = threading.Event()
    stats = {"polls": 0, "gone": 0, "errors": 0, "lines": 0, "answers": 0, "keys": 0, "lost": False}
    lat: list[float] = []
    last_err: list[str] = []
    screen = {"now": "?", "dialogue": None}

    def poll() -> None:
        poller = Game(port=g.port)
        i = 0
        while not stop.is_set():
            op = "ui.state" if i % 2 == 0 else "ui.dump"
            args = {} if op == "ui.state" else {"text_only": True, "max": 200}
            t0 = time.perf_counter()
            try:
                res = poller.rpc(op, args, context="client", timeout=5.0)
                lat.append((time.perf_counter() - t0) * 1000)
                if op == "ui.state" and isinstance(res, dict):
                    screen["now"] = res.get("screen", "?")
                    screen["dialogue"] = res.get("dialogue")
            except NotReady as e:
                if not alive(pid):
                    stats["lost"] = True
                    stop.set()
                    return
                last_err[:] = [str(e)]
                stats["errors"] += 1
            except GameError as e:
                if "Noesis object is gone" in str(e):
                    stats["gone"] += 1
                else:
                    stats["errors"] += 1
                    last_err[:] = [str(e)]
            stats["polls"] += 1
            i += 1

    t = threading.Thread(target=poll, daemon=True)
    t.start()
    keys = ["i", "ESCAPE", "m", "ESCAPE", "l", "ESCAPE"]
    k = 0
    end = time.monotonic() + seconds
    try:
        while time.monotonic() < end and not stop.is_set():
            d = screen["dialogue"]
            try:
                if screen["now"] == "dialogue" and isinstance(d, dict):
                    if not d.get("options"):
                        g.rpc("ui.dialogue_choose", {"i": "continue"}, context="client")
                        stats["lines"] += 1
                    elif answer:
                        g.rpc("ui.dialogue_choose", {"i": 1}, context="client")
                        stats["answers"] += 1
                else:
                    # Panel keys only from the plain game view; ESCAPE closes whatever is open (on the plain
                    # view it opens the game menu, which the next ESCAPE closes). Never on menus or movies.
                    key = keys[k % len(keys)]
                    if screen["now"] == "game" or (key == "ESCAPE" and screen["now"] not in NO_KEYS):
                        inp.key(key)
                        stats["keys"] += 1
                        k += 1
            except (GameError, NotReady) as e:
                last_err[:] = [str(e)]
            time.sleep(0.6)
    finally:
        stop.set()
        t.join(10)
        # Leave no panel open.
        if screen["now"] not in ("game",) + NO_KEYS:
            try:
                inp.key("ESCAPE")
            except Exception:
                pass
        if serialize:
            try:
                to = g.eval("Ext._Internal.UiSerialize(was) local _, n = Ext._Internal.UiSerialize() "
                            "return tostring(n)".replace("was", "true" if serialize_was else "false"),
                            context="client")
                report("PASS" if to == "0" else "WARN", "stress.serialize",
                       f"{to} lock timeouts (Lua ran unserialized that often)")
            except (GameError, NotReady) as e:
                report("WARN", "stress.serialize", f"could not restore UiSerialize: {e}")

    p50 = statistics.median(lat) if lat else 0.0
    p95 = statistics.quantiles(lat, n=20)[-1] if len(lat) >= 20 else max(lat or [0.0])
    detail = (f"{stats['polls']} polls in {seconds:.0f}s (ui.state/ui.dump p50 {p50:.0f} ms, p95 {p95:.0f} ms), "
              f"{stats['keys']} keys, {stats['lines']} lines, {stats['answers']} answers, "
              f"{stats['gone']} 'object is gone', {stats['errors']} other errors")
    log = latest_log()
    log_verdict, log_detail = check_log_text(log.read_text(errors="replace")) if log else ("PASS", "")
    if stats["lost"] or not alive(pid) or log_verdict == "FAIL" and "crash" in log_detail:
        report("FAIL", "stress", f"the game died. {detail}. {log_detail}")
    elif stats["errors"] > max(3, stats["polls"] // 50) or stats["gone"]:
        report("WARN", "stress", f"{detail}; last error: {last_err[:1]}")
    else:
        report("PASS", "stress", detail)
    if stats["keys"] == 0:
        report("WARN", "stress.keys", "no panel key was pressed (the screen never was the plain game view)")


def check_leak(g: Game, polls: int) -> None:
    pid = game_pid()
    try:
        before = lua(g, BOXES_LUA)
        rss0 = rss_mb(pid)
        for i in range(polls):
            g.rpc("ui.state" if i % 2 == 0 else "ui.dump", {} if i % 2 == 0 else {"text_only": True, "max": 200},
                  context="client", timeout=5.0)
        after = lua(g, BOXES_LUA)
        rss1 = rss_mb(pid)
    except (GameError, NotReady) as e:
        report("FAIL", "leak", f"{e}")
        return
    if before is None or after is None:
        report("FAIL", "leak", "no reply from the client context")
        return
    detail = (f"{polls} polls: boxes {before['boxes']} -> {after['boxes']}, Lua {before['kb']} -> {after['kb']} KB"
              + (f", RSS {rss0:.0f} -> {rss1:.0f} MB" if rss0 and rss1 else ""))
    if before["boxes"] < 0:
        report("WARN", "leak", detail + " (no debug.getregistry: box count unknown)")
    elif after["boxes"] > before["boxes"] + 50 or after["kb"] > before["kb"] * 1.5 + 2048:
        report("FAIL", "leak", detail)
    elif rss0 and rss1 and rss1 - rss0 > 200:
        report("WARN", "leak", detail + " (RSS grew; the game allocates on its own too, re-run to confirm)")
    else:
        report("PASS", "leak", detail)


INSTANTIATE_LUA = r"""
local ok, err = pcall(function()
  Ext.UI.RegisterType("BG3AICheckSE", { Value = { Type = "String" } })
  local ctx = Ext.UI.Instantiate("se::BG3AICheckSE")
  assert(ctx, "Instantiate returned nil")
  ctx.Value = "se-check"
  assert(ctx.Value == "se-check", "property round trip gave " .. tostring(ctx.Value))
  local cr = Ext.UI.GetRoot():Find("ContentRoot")
  assert(cr, "no ContentRoot")
  local old = cr.Tag
  cr.Tag = ctx
  local held = cr.Tag
  local same = held ~= nil and tostring(held) == tostring(ctx)
  cr.Tag = old
  assert(same, "ContentRoot.Tag did not hold the context: " .. tostring(held))
  ctx = nil held = nil
  collectgarbage() collectgarbage()
end)
return string.format('{"ok":%s,"err":%q}', tostring(ok), tostring(err))
"""


def check_instantiate(g: Game) -> None:
    try:
        res = lua(g, INSTANTIATE_LUA)
        time.sleep(0.5)  # a few UI frames: the dropped context's release is drained on the UI thread
        g.rpc("ui.state", {}, context="client")
    except (GameError, NotReady) as e:
        report("FAIL", "instantiate", str(e))
        return
    if res and res.get("ok"):
        report("PASS", "instantiate", "RegisterType/Instantiate, property round trip, held as an object property, "
               "released after GC, UI still answers")
    else:
        report("FAIL", "instantiate", (res or {}).get("err", "no reply"))


PROBE_LUA = r"""
local K = Mods.BG3AI.BG3AI.AutoSkip
if not (K and K.probeMovie) then return '{"probe":false}' end
local t = Ext.Utils.MonotonicTime()
for _ = 1, 50 do K.probeMovie() end
return string.format('{"probe":true,"ms":%.3f}', (Ext.Utils.MonotonicTime() - t) / 50)
"""


def check_autoskip(g: Game) -> None:
    from bg3ai.skipper import MovieSkipper

    lat = []
    st = None
    for _ in range(10):
        t0 = time.perf_counter()
        try:
            st = g.rpc("autoskip.status", {}, context="client", timeout=3.0)
        except NotReady as e:
            report("FAIL", "autoskip.status", str(e))
            return
        except GameError as e:
            report("FAIL", "autoskip.status", f"{e} (mod older than the AutoSkip latency change?)")
            return
        lat.append((time.perf_counter() - t0) * 1000)
    p50 = statistics.median(lat)
    s = st.get("stats", {})
    detail = (f"round trip p50 {p50:.0f} ms; skipped {s.get('movies', 0)} movies, {s.get('lines', 0)} lines "
              f"of {s.get('seen', 0)} seen; {s.get('errors', 0)} errors")
    if st.get("on") is False:
        report("FAIL", "autoskip.status", "AutoSkip is OFF (ui.config{autoskip=true}); " + detail)
    elif s.get("errors"):
        report("WARN", "autoskip.status", detail + " (see the [BG3AI] autoskip warnings in the bg3le log)")
    elif p50 > 100:
        report("WARN", "autoskip.status", detail + " (round trip over 100 ms)")
    else:
        report("PASS", "autoskip.status", detail)

    try:
        pr = lua(g, PROBE_LUA)
    except (GameError, NotReady) as e:
        pr = None
        report("FAIL", "autoskip.probe", str(e))
    if pr is not None:
        if not pr.get("probe"):
            report("FAIL", "autoskip.probe", "AutoSkip.probeMovie missing (old AutoSkip.lua loaded?)")
        else:
            report("PASS" if pr["ms"] < 3 else "WARN", "autoskip.probe",
                   f"movie probe {pr['ms']:.2f} ms per call, every 100 ms")

    sk = MovieSkipper(Game(port=g.port), lambda: None)
    sk.step()
    report("PASS" if sk.mode == "fast" else "FAIL", "autoskip.skipper",
           f"bridge skipper mode {sk.mode!r} (fast = polls autoskip.status every {sk.fast_s:.2f} s)")

    if st.get("movie"):
        end = time.monotonic() + 3.0
        while time.monotonic() < end:
            time.sleep(0.1)
            cur = g.try_rpc("autoskip.status", {}, context="client", default={}) or {}
            if not cur.get("movie"):
                report("PASS", "autoskip.movie", f"the movie on screen was skipped within "
                       f"{3.0 - (end - time.monotonic()):.1f} s")
                break
        else:
            report("FAIL", "autoskip.movie", "a movie is still up after 3 s (is the bridge's skipper running? "
                   "BG3AI_MOVIE_SKIP must not be 0)")
    else:
        report("PASS", "autoskip.movie", "no movie on screen now (the timing is covered by tools/dev/test_autoskip.sh)")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--seconds", type=float, default=60.0, help="stress duration")
    ap.add_argument("--polls", type=int, default=300, help="leak-watch polls")
    ap.add_argument("--answer", action="store_true", help="stress: also pick answer 1 in dialogues")
    ap.add_argument("--serialize", action="store_true", help="stress: with Ext._Internal.UiSerialize(true)")
    ap.add_argument("--only", default="hook,stress,leak,instantiate,autoskip")
    ap.add_argument("--port", type=int, default=int(os.environ.get("BG3LE_DEBUG_PORT", "9998")))
    a = ap.parse_args()
    only = set(a.only.split(","))

    g = Game(port=a.port)
    if not alive(game_pid()):
        report("FAIL", "game", "bg3 is not running (runner/launch_game.sh, then load a save)")
        return 1
    if "hook" in only:
        check_hook(g)
    if "autoskip" in only:
        check_autoskip(g)
    if "instantiate" in only:
        check_instantiate(g)
    if "leak" in only:
        check_leak(g, a.polls)
    if "stress" in only:
        check_stress(g, a.seconds, a.answer, a.serialize)

    n = {v: sum(1 for r in results if r[0] == v) for v in ("PASS", "WARN", "FAIL")}
    print(f"\n{n['PASS']} PASS, {n['WARN']} WARN, {n['FAIL']} FAIL")
    return 1 if n["FAIL"] else 0


if __name__ == "__main__":
    sys.exit(main())

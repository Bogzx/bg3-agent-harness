"""End-to-end test of libbg3sdl.so without the game.

Runs build/bg3sdl/sdltest (an SDL2 loop linked against the game's own libSDL2.so, dummy video driver, so
no window appears) with the shim preloaded on a private socket name, drives it through
bridge/src/bg3ai/input.py, and checks the events the program prints.

    python3 native/bg3sdl/test/test_shim.py [--chain] [--sniper]
    --chain   preload a bg3le-like SDL_PollEvent hook in front of the shim
    --sniper  run the program inside SteamLinuxRuntime_sniper, as the game runs
"""
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "bridge" / "src"))
from bg3ai.input import InputClient, InputError  # noqa: E402

OUT = REPO / "build" / "bg3sdl"
SOCK = f"bg3ai-input-test-{os.getpid()}"
chain = "--chain" in sys.argv

preload = [str(OUT / "libbg3sdl.so")]
if chain:
    preload.insert(0, str(OUT / "chainfirst.so"))
env = dict(os.environ, SDL_VIDEODRIVER="dummy", LD_PRELOAD=":".join(preload), BG3AI_INPUT_SOCKET=SOCK)
cmd = [str(OUT / "sdltest"), "60"]
if "--sniper" in sys.argv:  # inside Steam's runtime container, as the game runs
    sys.path.insert(0, str(REPO / "tools"))
    from steam_paths import sniper_dir  # $BG3AI_SNIPER_DIR, else Steam's libraryfolders.vdf

    sniper = sniper_dir() / "run"
    from steam_paths import game_dir
    # The container shares $HOME, not e.g. /data (as launch_game.sh): share the repo and the game's bin/
    # (sdltest's RUNPATH points at the game's libSDL2).
    share = [str(p) for p in (REPO, game_dir()) if not p.resolve().is_relative_to(Path.home())]
    if share:
        env["PRESSURE_VESSEL_FILESYSTEMS_RW"] = ":".join(
            filter(None, [*share, os.environ.get("PRESSURE_VESSEL_FILESYSTEMS_RW")]))
    cmd = [str(sniper), "--", "env", *(f"{k}={env[k]}" for k in ("SDL_VIDEODRIVER", "LD_PRELOAD",
                                                                    "BG3AI_INPUT_SOCKET")), *cmd]
proc = subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                        text=True)
lines: list[dict] = []
cond = threading.Condition()


def reader():
    for line in proc.stdout:
        with cond:
            lines.append(json.loads(line))
            cond.notify_all()


threading.Thread(target=reader, daemon=True).start()
failures = 0


def check(name, ok, detail=""):
    global failures
    failures += not ok
    print(("PASS " if ok else "FAIL ") + name + (f"   [{detail}]" if not ok and detail else ""))


def wait_for(pred, timeout=3.0):
    deadline = time.time() + timeout
    with cond:
        while True:
            hit = [l for l in lines if pred(l)]
            if hit:
                return hit
            left = deadline - time.time()
            if left <= 0:
                return []
            cond.wait(left)


def mark():
    with cond:
        return len(lines)


def since(n, pred=lambda l: True):
    with cond:
        return [l for l in lines[n:] if pred(l)]


check("program ready", bool(wait_for(lambda l: l.get("ready"), timeout=60 if "--sniper" in sys.argv else 3)))
inp = InputClient(SOCK)
for _ in range(50):  # the listener starts on the first SDL_PollEvent
    try:
        inp.ping()
        break
    except ConnectionError:
        time.sleep(0.05)
st = inp.status()
check("status: window seen, 1280x720", st["window"] > 0 and st["size"] == [1280, 720], st)
check("status: agent off at start", st["agent"] is False)
f0 = st["frames"]
time.sleep(0.2)
check("frames advance", inp.status()["frames"] > f0)
check("window_size()", inp.window_size() == (1280, 720))

n = mark()
inp.click(640, 360)
ev = since(n)
moves = [e for e in ev if e.get("ev") == "motion"]
downs = [e for e in ev if e.get("ev") == "mdown"]
ups = [e for e in ev if e.get("ev") == "mup"]
check("click: motion to 640,360", moves and moves[0]["x"] == 640 and moves[0]["y"] == 360, ev)
check("click: one down+up of button 1 at 640,360",
      len(downs) == 1 and len(ups) == 1 and downs[0]["button"] == 1 and downs[0]["x"] == 640 and ups[0]["y"] == 360, ev)
check("click: windowID filled", downs and downs[0]["win"] == st["window"])
check("click: held >= 2 frames (~32 ms)", downs and ups and ups[0]["t"] - downs[0]["t"] >= 25,
      downs and ups and ups[0]["t"] - downs[0]["t"])
check("agent switched on by click", inp.status()["agent"] is True)

n = mark()
inp.key("F12")
probe = wait_for(lambda l: l.get("probe") == "f12")
check("key F12: down+up", len(since(n, lambda l: l.get("name") == "F12")) == 2)
check("GetGlobalMouseState = window pos + cursor (640,360)", probe and probe[-1]["global"] == [640, 360], probe)
check("GetMouseFocus/GetWindowFlags faked in agent mode",
      probe and probe[-1]["focus_is_window"] and probe[-1]["input_focus"] and probe[-1]["mouse_focus"], probe)

n = mark()
inp.mouse_down("L", 200, 100)
st = inp.status()
check("mdown: button mask held", st["buttons"] == 1, st)
inp.key("F12")
probe = wait_for(lambda l: l.get("probe") == "f12" and l["mask"] == 1)
check("GetGlobalMouseState reports held button", bool(probe))
inp.mouse_up("L", 300, 150)
check("mup: mask released", inp.status()["buttons"] == 0)

n = mark()
inp.drag(10, 10, 110, 60, steps=5)
ev = since(n)
dmoves = [e for e in ev if e.get("ev") == "motion"]
check("drag: down, 5+ moves with left held, up",
      [e["ev"] for e in ev if e.get("ev") in ("mdown", "mup")] == ["mdown", "mup"]
      and len([m for m in dmoves if m["state"] == 1]) >= 5 and dmoves[-1]["x"] == 110, ev)
check("motion xrel/yrel relative to previous", all(m["xrel"] == 20 and m["yrel"] == 10 for m in dmoves[1:-1]), dmoves)

n = mark()
inp.key("LCTRL+S")
ev = since(n, lambda l: l.get("ev") in ("kdown", "kup"))
order = [(e["ev"], e["name"]) for e in ev]
s_down = [e for e in ev if e["ev"] == "kdown" and e["name"] == "S"]
check("chord LCTRL+S: ctrl down, S down, S up, ctrl up",
      order == [("kdown", "Left Ctrl"), ("kdown", "S"), ("kup", "S"), ("kup", "Left Ctrl")], order)
check("chord: S carries KMOD_LCTRL and sym 's'", s_down and s_down[0]["mod"] & 0x40 and s_down[0]["sym"] == ord("s"), s_down)

n = mark()
inp.key("Escape")
inp.key("ESC")
inp.key("space")
names = [e["name"] for e in since(n, lambda l: l.get("ev") == "kdown")]
check("key names and aliases", names == ["Escape", "Escape", "Space"], names)

n = mark()
msg = "héllo wörld, a fairly long line that needs more than one SDL_TEXTINPUT event ✓"
inp.text(msg)
got = "".join(e["text"] for e in since(n, lambda l: l.get("ev") == "text"))
check("text: UTF-8 split across events and reassembled", got == msg, got)

n = mark()
inp.wheel(-3, 50, 60)
wheels = since(n, lambda l: l.get("ev") == "wheel")
check("wheel -3 at 50,60", [w["y"] for w in wheels] == [-1, -1, -1] and wheels[0]["mx"] == 50, wheels)

n = mark()
inp.click(5, 5, count=2)
clicks = [e["clicks"] for e in since(n, lambda l: l.get("ev") == "mdown")]
check("double click: clicks 1 then 2", clicks == [1, 2], clicks)

n = mark()
inp.key("F11")
probe = wait_for(lambda l: l.get("probe") == "warp")
check("WarpMouseInWindow moves the virtual cursor", probe and probe[-1]["global"] == [100, 50], probe)
check("warp counted", inp.status()["warps"] >= 1)

inp.agent(False)
inp.key("F12")
time.sleep(0.1)
probe = [l for l in lines if l.get("probe") == "f12"][-1]
# The dummy driver reports real focus itself, so the observable difference is the cursor: real again, not the
# virtual (100, 50) left by the warp.
check("agent off: real mouse state again", probe["global"] != [100, 50], probe)

for bad, why in [("key NOPE", "unknown key"), ("bogus", "unknown command"), ("click Q 1 2", "usage"),
                 ("move 1", "usage"), ("text", "usage"), ("down LCTRL+S", "too many")]:
    try:
        inp.command(bad)
        check(f"rejects {bad!r}", False)
    except InputError as e:
        check(f"rejects {bad!r}", why in str(e), str(e))

inp.command("wait 120")
inp.command("key A")
cleared = inp.clear()
check("clear drops queued steps", cleared >= 2, cleared)
inp.sync(1000)

# A second client at the same time.
inp2 = InputClient(SOCK)
inp2.key("B")
inp.key("C")
check("two clients", {"B", "C"} <= {e["name"] for e in lines if e.get("ev") == "kdown"})

inp.key("F10", sync=False)
proc.wait(timeout=5)
err = proc.stderr.read()
check("program exited cleanly", proc.returncode == 0, proc.returncode)
check("shim announced itself", "bg3sdl 1 listening" in err, err)
if chain:
    check("bg3le-style hook in front saw the injected events", "chainfirst: saw" in err
          and int(err.split("chainfirst: saw ")[1].split()[0]) > 50, err)
print(f"{'ALL OK' if not failures else f'{failures} FAILURES'}{' (chained)' if chain else ''}")
sys.exit(1 if failures else 0)

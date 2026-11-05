"""Check R4 (camp-night r4, LAST in the window): does bg3le's frame lock stay taken after a camp leave's sleep press
sent the same second a talk ends, and where does the UI thread sit then?

    uv run --project bridge python tools/dev/live_v010/ui_lock.py watch [minutes] [--gdb]   # in its own shell
    uv run --project bridge python tools/dev/live_v010/ui_lock.py strict <on|off|read>
    uv run --project bridge python tools/dev/live_v010/ui_lock.py gdb                      # one stack dump now

watch: every 2 s, client Lua (no UI call, so it answers while the UI is busy): Ext._Internal.UiBusy() (no argument:
a read; an argument would SET it), UiSerialize()'s lock_timeouts lua/ui, UiStrict() (on, refused). Prints a line
on every change and the busy streak's length. With --gdb, once a busy streak passes 60 s it dumps every thread's
stack of the harness's game process (bg3harness.game.io.pids_of_game, never a user's own BG3) to
runtime/probes/v010/R4-gdb-<n>.txt: `gdb -p <pid> -batch -ex 'thread apply all bt 40'` under timeout 240 s,
MemoryMax 2G, nice 10. gdb stops the game while it reads; it detaches when done. One dump per streak.
strict: Ext._Internal.UiStrict(true|false), or read it.
"""

from __future__ import annotations

import subprocess
import sys
import time

from common import OUT, REPO, lua_json, rec

READ = r"""
local I = Ext._Internal or {}
local out = {}
if type(I.UiBusy) == "function" then
    local ok, on, refused, busy = pcall(I.UiBusy)
    if ok then out.busy_refuse_on, out.refused_at_once, out.busy = on, refused, busy end
end
if type(I.UiSerialize) == "function" then
    local ok, _, lua_to, ui_to = pcall(I.UiSerialize)
    if ok then out.lua_timeouts, out.ui_timeouts = lua_to, ui_to end
end
if type(I.UiStrict) == "function" then
    local ok, on, refused = pcall(I.UiStrict)
    if ok then out.strict, out.strict_refused = on, refused end
end
return Ext.Json.Stringify(out)
"""


def game_pid() -> int | None:
    sys.path.insert(0, str(REPO / "packages" / "harness" / "src"))
    from bg3harness.game import io

    pids = io.pids_of_game()
    return pids[0] if pids else None


def gdb_dump(n: int) -> str:
    pid = game_pid()
    if pid is None:
        return "no harness game process"
    path = OUT / f"R4-gdb-{n}.txt"
    cmd = [
        "timeout",
        "-s",
        "INT",
        "240",
        "systemd-run",
        "--user",
        "--scope",
        "-q",
        "-p",
        "MemoryMax=2G",
        "nice",
        "-n",
        "10",
        "gdb",
        "-p",
        str(pid),
        "-batch",
        "-ex",
        "set pagination off",
        "-ex",
        "thread apply all bt 40",
    ]
    t0 = time.time()
    with path.open("w") as f:
        rc = subprocess.call(cmd, stdout=f, stderr=subprocess.STDOUT)
    rec("R4", "gdb", {"pid": pid}, f"rc {rc}, {path} ({path.stat().st_size} bytes)", t0)
    return str(path)


def watch(minutes: float, use_gdb: bool) -> None:
    end, last, busy_since, dumped, n = time.time() + minutes * 60, None, None, False, 0
    while time.time() < end:
        r = lua_json(READ, "client")
        now = time.time()
        if r.get("busy"):
            busy_since = busy_since or now
        else:
            busy_since, dumped = None, False
        streak = round(now - busy_since) if busy_since else 0
        key = {k: r.get(k) for k in ("busy", "lua_timeouts", "ui_timeouts", "strict", "error")}
        if key != last:
            rec("R4-watch", "ui_lock", None, {**r, "busy_streak_s": streak}, now)
            last = key
        elif busy_since and streak % 30 < 2:
            print(f"   busy for {streak} s", flush=True)
        if use_gdb and busy_since and streak > 60 and not dumped:
            n += 1
            print("   gdb dump:", gdb_dump(n), flush=True)
            dumped = True
        time.sleep(2.0)


def main(argv: list[str]) -> int:
    cmd = argv[0] if argv else ""
    if cmd == "watch":
        mins = next((float(a) for a in argv[1:] if a.replace(".", "").isdigit()), 20.0)
        watch(mins, "--gdb" in argv)
    elif cmd == "strict":
        arg = {"on": "true", "off": "false"}.get(argv[1] if len(argv) > 1 else "read", "")
        print(
            lua_json(
                f"local on, r = Ext._Internal.UiStrict({arg}) return Ext.Json.Stringify({{on, r}})",
                "client",
                "R4-strict",
            )
        )
    elif cmd == "gdb":
        print(gdb_dump(0))
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

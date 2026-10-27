# pyright: basic
"""The game's I/O (step 6 of research/arch/harness.md §4, first half; moved from runner/play.py): the debug port and
its Lua evals and mod RPCs, the UI and menu reads, save folder names, the game's processes (ours or a user's) and the
launch watch. Callers use the module's functions as attributes (`game.port_open()`), so a test patches one place.

    python3 -m unittest runner/test_crash.py runner/test_play.py
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path


def _quiet(_msg: str) -> None:
    pass


DEBUG_HOST = "127.0.0.1"
DEBUG_PORT = int(os.environ.get("BG3LE_DEBUG_PORT", "9998"))
EVAL_TIMEOUT = 15
PORT_DEAD_S = 60  # bg3 up this long with port 9998 refusing = dead (bg3le listens ~1 s after start)
BG3_APP_ENV = b"SteamAppId=1086940"
SAVE_FOLDER_RE = re.compile(r"^(?P<owner>.+)-(?P<num>\d+)__(?P<label>.+)$")
# The player's own created character (the avatar), not the selected one: Osi.GetHostCharacter() and the
# client's ClientControl follow the selection, so picking Lae'zel used to close the save/load gates.
# Server context. DB_Avatars first, then the Avatar component; the host character only as a last resort.
AVATAR_NAME_LUA = (
    "local function nm(g) local ok, n = pcall(function() return "
    "Ext.Loca.GetTranslatedString(Osi.GetDisplayName(g)) end) return ok and n or nil end "
    "local ok, rows = pcall(function() return Osi.DB_Avatars:Get(nil) end) "
    'if ok and rows and rows[1] and rows[1][1] then local n = nm(rows[1][1]) if n and n ~= "" then return n end end '
    'for _, e in ipairs(Ext.Entity.GetAllEntitiesWithComponent("Avatar") or {}) do '
    "  local dn = e.DisplayName "
    "  if dn then local n = Ext.Loca.GetTranslatedString(dn.NameKey.Handle.Handle) "
    'if n and n ~= "" then return n end end '
    "end "
    'return nm(Osi.GetHostCharacter()) or ""'
)
# "1" when a party member is in a fight, "0" when none is, "?" when the party can't be read.
PARTY_COMBAT_LUA = (
    "local ok, rows = pcall(function() return Osi.DB_PartyMembers:Get(nil) end) "
    'if not ok or not rows or not rows[1] then return "?" end '
    'for _, r in ipairs(rows) do local c = Osi.IsInCombat(r[1]) if c == 1 or c == true then return "1" end end '
    'return "0"'
)
MOD_READY_LUA = "return tostring(Mods.BG3AI ~= nil and Mods.BG3AI.BG3AI ~= nil and Mods.BG3AI.BG3AI.RPC ~= nil)"
# A harness process (launch_game.sh's game, or the crash reporter it spawns) carries one of these (RUN-4): the marker
# env (BG3AI_GAME_STAMP, from 2026-10-16) or the screenshot layer's bg3ai control socket (every launch before it),
# its own cgroup scope (BG3AI_GAME_SCOPE, the default) or bg3le mapped in.
HARNESS_ENV = (b"BG3AI_GAME_STAMP=", b"control=bg3ai-shot")


@dataclass(frozen=True)
class Save:
    folder: str
    owner: str
    label: str
    mtime: float
    saved: float | None = None  # when the game wrote it: its meta.lsf SaveTime (bg3ai.saves.saved_at), None unread


def parse_save_folder(name: str, mtime: float = 0.0) -> Save | None:
    m = SAVE_FOLDER_RE.match(name)
    if not m:
        return None
    return Save(name, m.group("owner"), m.group("label"), mtime)


def progress_signature(status: dict | None, journal: list | dict | None) -> dict | None:
    """What counts as progress: level/region/subregion, party levels and XP, journal steps."""
    if status is None and journal is None:
        return None
    sig: dict = {}
    if status:
        sig["level"] = status.get("level")
        sig["region"] = status.get("region")
        sig["subregion"] = status.get("subregion")
        party = status.get("party") or []
        sig["party_levels"] = sorted(str(p.get("level")) for p in party if isinstance(p, dict))
        placed = [p for p in party if isinstance(p, dict) and isinstance(p.get("pos"), list) and len(p["pos"]) >= 3]
        if placed:
            sig["pos"] = [round(float(x), 1) for x in placed[0]["pos"][:3]]  # not compared as is: see session_progress
            # RUN-6: the party's order can change (a swap, the selection); state_drift compares one member by name
            sig["pos_name"] = placed[0].get("name")
            sig["party_pos"] = {
                str(p["name"]): [round(float(x), 1) for x in p["pos"][:3]] for p in placed if p.get("name")
            }
        xp = [p["xp"] for p in party if isinstance(p, dict) and p.get("xp") is not None]
        if xp:
            sig["xp"] = sum(xp)
    if journal is not None:
        quests = journal.get("quests", journal) if isinstance(journal, dict) else journal
        steps = 0
        titles = []
        for q in quests or []:
            if isinstance(q, dict):
                titles.append(f"{q.get('title')}|{q.get('done', False)}")
                steps += len(q.get("steps") or [])
        sig["journal"] = hashlib.sha1("\n".join(sorted(titles)).encode()).hexdigest()[:12]
        sig["journal_steps"] = steps
    return sig


class RpcError(RuntimeError):
    """The mod refused an op or answered garbage. A RuntimeError: at a session boundary it is a setup failure
    (relaunch and retry), never the runner's death (RUN-3: a refused ui.menu load killed the run)."""


class LaunchBlocked(RuntimeError):  # noqa: N818  (play.py's names)
    """The game can't be launched now for a reason an operator fixes outside the runner: never a give-up, the run
    loop retries every STEAM_RETRY_S."""


class ForeignGameRunning(LaunchBlocked):
    """A bg3 this harness didn't launch (a user's own BG3) is running: never signalled, and no second game beside it
    (RUN-4)."""


class SteamNotOffline(LaunchBlocked):
    """Steam isn't in offline mode: launching BG3 could kick the account's other machine off. Never launch."""


class NoDisplay(LaunchBlocked):
    """No usable display env (display_env.problem): the game would fall back to the iGPU and crash (L8). Never
    launch; the desktop session coming back fixes it."""


class GameDied(RuntimeError):  # noqa: N818  (play.py's names)
    """The game died while the runner waited on it (crash, crash reporter, bg3 gone, dead debug port, launcher exit)."""


class Bg3leStale(Exception):  # noqa: N818  (play.py's names)
    """The bg3le build lacks a patches/bg3le patch (UI-1): never launch it. Not a RuntimeError: a stale build never
    fixes itself, so no retry loop may swallow it; the runner stops until tools/build_bg3le.sh is run."""


class LaunchGaveUp(Exception):  # noqa: N818  (play.py's names)
    """LAUNCH_ATTEMPTS launches in a row died before the save played: the runner stops (not a RuntimeError, so no
    retry loop swallows it)."""


def port_open(host: str = DEBUG_HOST, port: int = DEBUG_PORT, timeout: float = 1.0) -> bool:
    """The debug port answers. A handshake, not a bare connect-and-close: that leaked a bg3le client slot
    per probe ("client attached (371 connected)"); see luadebug.port_answers."""
    from bg3ai.luadebug import port_answers

    return port_answers(host, port, timeout)


def lua_eval(code: str, context: str = "client", timeout: float = EVAL_TIMEOUT) -> str:
    from bg3ai.luadebug import LuaDebugClient

    with LuaDebugClient(connect_timeout=5.0) as lua:
        return lua.evaluate(code, context=context, timeout=timeout)


def rpc(op: str, args: dict | None = None, context: str = "server", timeout: float = EVAL_TIMEOUT):
    payload = json.dumps({"op": op, "args": args or {}})
    if "]==]" in payload:
        raise ValueError("payload contains the Lua long-string terminator")
    raw = lua_eval(f"return Mods.BG3AI.BG3AI.rpc([==[{payload}]==])", context, timeout)
    try:
        reply = json.loads(raw)
    except json.JSONDecodeError as e:
        raise RpcError(f"{op}: non-JSON reply {raw[:200]!r}") from e
    if not reply.get("ok"):
        raise RpcError(f"{op}: {reply.get('error')}")
    return reply.get("result")


def mod_loaded(timeout: float = EVAL_TIMEOUT) -> bool | None:
    """RUN-8: the BG3AI mod's RPC is loaded in the client context (MOD_READY_LUA); None when the eval itself fails
    (that is game_state's business, not a missing mod)."""
    try:
        return lua_eval(MOD_READY_LUA, "client", timeout).strip() == "true"
    except Exception:
        return None


def game_state(timeout: float = EVAL_TIMEOUT) -> str | None:
    """Ext.Utils.GetGameState() from the client context, or None if the game doesn't answer."""
    try:
        return lua_eval("return tostring(Ext.Utils.GetGameState())", "client", timeout).strip()
    except Exception:
        return None


def available_ops(context: str) -> set[str]:
    try:
        return set((rpc("sys.info", context=context) or {}).get("ops") or [])
    except Exception:
        return set()


def host_character_name() -> str | None:
    """Display name of the party's avatar (the player's own created character), or None.

    Not the selected character: selecting a companion must neither close the save/load gates nor make
    --take-over think the running game belongs to someone else."""
    try:
        return lua_eval(AVATAR_NAME_LUA, context="server").strip() or None
    except Exception:
        return None


def party_in_combat() -> bool | None:
    """Whether a party member is in a fight (server context), None if the game doesn't answer."""
    try:
        r = lua_eval(PARTY_COMBAT_LUA, context="server").strip()
    except Exception:
        return None
    return {"1": True, "0": False}.get(r)


def server_ready() -> bool:
    try:
        rpc("sys.ping", context="server")
        return True
    except Exception:
        return False


def ui_state() -> dict | None:
    """The mod's ui.state (client), or None without the op or an answer."""
    if "ui.state" not in available_ops("client"):
        return None
    try:
        return rpc("ui.state", context="client") or {}
    except Exception:
        return None


def ui_screen() -> str | None:
    return (ui_state() or {}).get("screen")


def at_splash(st: dict | None) -> bool:
    """The "Press any key" splash before the main menu (ui.state shows it as a panel, not a screen)."""
    st = st or {}
    return st.get("page") == "SplashScreen" or "SplashScreen" in (st.get("panels_open") or [])


def press_menu_key(log: Callable[[str], None] = _quiet) -> str:
    """One key past the splash: Enter through the input shim (a real SDL key, like a player), else the engine's
    InjectKeyPress. Returns which one worked."""
    try:
        from bg3ai.input import InputClient

        with InputClient(timeout=3.0) as inp:
            inp.key("ENTER")
        return "shim Enter"
    except Exception as e:
        log(f"shim key failed ({e}); using InjectKeyPress")
    lua_eval('Ext.Input.InjectKeyPress("SPACE") return 1')
    return "InjectKeyPress SPACE"


def read_progress(log: Callable[[str], None] = _quiet) -> dict | None:
    ops = available_ops("server")
    status = journal = None
    try:
        if "state.status" in ops:
            status = rpc("state.status")
        if "state.journal" in ops:
            journal = rpc("state.journal")
    except Exception as e:
        log(f"progress read failed: {e}")
    return progress_signature(status, journal)


def load_menu_args(save: Save, saves: list[Save]) -> dict:
    """ui.menu load arguments: the Load page groups saves by owner, newest first by the save's own time (meta.lsf
    SaveTime, Save.saved; never the folder mtime, which Steam's local cloud sets to the launch on a restored copy),
    and the mod checks the selected save's date before loading (it can't read save titles there). A save whose time
    can't be read gets no index: its row is unknown."""
    same = sorted(
        (s for s in saves if s.owner == save.owner),
        key=lambda s: s.saved if s.saved is not None else float("-inf"),  # saves.saved_key (saves imports io)
        reverse=True,
    )
    args: dict = {"action": "load", "name": save.folder, "owner": save.owner}
    idx = next((i for i, s in enumerate(same, 1) if s.folder == save.folder), None)
    if idx is not None and save.saved is not None:
        t = time.localtime(save.saved)
        args.update(index=idx, date={"y": t.tm_year, "m": t.tm_mon, "d": t.tm_mday, "H": t.tm_hour, "M": t.tm_min})
    return args


def all_bg3_pids() -> list[int]:
    """Every native game process on the machine (./bin/bg3), ours or a user's own; not the pressure-vessel wrapper."""
    r = subprocess.run(["pgrep", "-x", "bg3"], capture_output=True, text=True)
    return [int(p) for p in r.stdout.split()]


def is_harness_pid(pid: int) -> bool:
    """The process was started by runner/launch_game.sh (this harness), not by a user from Steam."""
    proc = Path(f"/proc/{pid}")
    try:
        if "/bg3ai-game-" in (proc / "cgroup").read_text(errors="replace"):
            return True
    except OSError:
        pass
    try:
        env = (proc / "environ").read_bytes()
        if any(m in env for m in HARNESS_ENV):
            return True
    except OSError:
        pass
    try:
        with open(proc / "maps", "rb") as f:
            return any(b"libbg3le" in ln for ln in f)
    except OSError:
        return False


def pids_of_game() -> list[int]:
    """This harness's native game processes (all_bg3_pids that is_harness_pid): the ones the runner may wait on,
    count as its game, or kill. A user's own BG3 is never one of them (RUN-4)."""
    return [p for p in all_bg3_pids() if is_harness_pid(p)]


def foreign_bg3_pids() -> list[int]:
    """bg3 processes this harness didn't launch (a user's own BG3): the runner never signals them and never
    launches a second game beside one."""
    return [p for p in all_bg3_pids() if not is_harness_pid(p)]


def proc_start(pid: int) -> int | None:
    """The process's start time (clock ticks since boot, /proc/<pid>/stat field 22): with the pid, its identity
    (a pid reused after a reboot or a wrap has another start time)."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        return int(stat[stat.rindex(")") + 2 :].split()[19])
    except (OSError, ValueError, IndexError):
        return None


def proc_cmdline(pid: int) -> bytes:
    try:
        return Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return b""


def _pgid(pid: int) -> int | None:
    try:
        return os.getpgid(pid)
    except OSError:
        return None


def crash_reporter_pids() -> list[int]:
    """BG3's LinuxCrashReporter processes. One starts after the game crashes and keeps the launcher alive (S014:
    the runner then waited 10 min for a dead game). Only reporters with the game's SteamAppId (or its install path)
    that this harness's game spawned (is_harness_pid): a user's own BG3 crash report is left alone (RUN-4)."""
    r = subprocess.run(["pgrep", "-f", "LinuxCrashReporter"], capture_output=True, text=True)
    out = []
    for p in r.stdout.split():
        try:
            env = Path(f"/proc/{p}/environ").read_bytes().split(b"\0")
            cmd = Path(f"/proc/{p}/cmdline").read_bytes()
        except OSError:
            continue
        if (BG3_APP_ENV in env or b"Baldurs Gate 3" in cmd) and is_harness_pid(int(p)):  # RUN-4: ours only
            out.append(int(p))
    return out


class LaunchWatch:
    """Fast crash detection for one launch, polled by Runner.wait_for(). Dead when: a bg3le log of this launch's stamp
    (runtime/logs/bg3le-<stamp>.log.<pid>) has a `crash:` line or was written by LinuxCrashReporter; a game crash
    reporter runs; bg3 was up and is gone; or bg3 is up but port 9998 has refused for PORT_DEAD_S s."""

    def __init__(self, stamp: str, logs: Path, started: float | None = None, port_dead_s: float = PORT_DEAD_S):
        self.stamp, self.logs, self.port_dead_s = stamp, logs, port_dead_s
        self.started = time.time() if started is None else started
        self.offsets: dict[str, int] = {}
        self.bg3_since: float | None = None
        self.port_ok_at: float | None = None

    def log_crash(self) -> str | None:
        """The first crash sign in this launch's bg3le logs; reads only what's new since the last call."""
        for f in sorted(self.logs.glob(f"bg3le-{self.stamp}.log*")):
            off = self.offsets.get(f.name, 0)
            try:
                with open(f, "rb") as fh:
                    fh.seek(off)
                    chunk = fh.read()
            except OSError:
                continue
            end = chunk.rfind(b"\n") + 1  # keep a partial last line for the next call
            self.offsets[f.name] = off + end
            for line in chunk[:end].splitlines():
                if b"] crash:" in line:
                    return line.split(b"] ", 1)[-1].decode(errors="replace").strip()[:200]
                if b"host is LinuxCrashReporter" in line:
                    return f"LinuxCrashReporter started ({f.name})"
        return None

    def check(self, now: float | None = None) -> str | None:
        now = time.time() if now is None else now
        why = self.log_crash()
        if why:
            return f"bg3le logged {why}"
        rep = crash_reporter_pids()
        if rep:
            return f"LinuxCrashReporter is running (pid {', '.join(map(str, rep))})"
        if pids_of_game():
            if self.bg3_since is None:
                self.bg3_since = now
        elif self.bg3_since is not None:
            return "the bg3 process is gone"
        if port_open():
            self.port_ok_at = now
        elif self.bg3_since is not None:
            since = max(self.bg3_since, self.port_ok_at or 0.0)
            if now - since > self.port_dead_s:
                return f"bg3 is up but debug port {DEBUG_PORT} has refused for {now - since:.0f} s"
        return None

"""Think-while-acting: run action steps back to back, with no LLM round trip between them.

Two users:
* `do_actions` runs a list of steps in the calling tool (blocking) and returns a compact diff.
* `queue_actions` appends steps to a per-character FIFO that a worker thread runs while the agent
  keeps observing and planning. Results arrive as `queue_done` / `queue_interrupted` events.

Every step is an ordinary tool call (move_to, interact, use_ability, ...), so the mod's fairness guard
checks each one exactly as if the agent had called it. Conditions (`if` / `unless`) only read what the
player can see (the mod's act.watch / act.target_state).

Default interrupts flush the rest of the steps and say why and what was left: combat starting, a
conversation starting, a reaction / roll / popup on screen, a new hostile in view, a party member's
HP dropping below a threshold, any damage taken outside combat (fire, traps, falls: S001 burned from 22 to
3 HP while a travel kept retrying), a party member stepping into a harmful surface the player can see
(fire, acid, ...), a teleport the guard blocked, and any step that fails (path failure, target gone,
refusal).
"""

from __future__ import annotations

import collections
import json
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

MAX_STEPS = 20
HP_BELOW_PCT = 35  # default "HP below" interrupt, percent of max HP
# S042 22:51:18: loot_area stopped on 2 HP of 104 (a Holy Fire tick) right after opening a corpse: the container was
# left unlooted and 8 corpses skipped. Outside combat, a chip below DAMAGE_MIN_PCT of max HP is not an interrupt;
# chips add up, and DAMAGE_SUM_PCT of them in one run (a burn that keeps ticking) stops it.
DAMAGE_MIN_PCT = 4.0
DAMAGE_SUM_PCT = 8.0
WATCH_EVERY_S = 0.3  # act.watch + events poll while a step runs
UI_EVERY_S = 1.0  # ui.state (client) poll: kept slow, Noesis reads are the risky part

# Prompt screens and the steps that are meant to answer them.
PROMPTS = {
    "dialogue": {"talk_to", "choose_dialogue", "roll_check"},
    "roll": {"roll_check"},
    "reaction": {"react"},
    "popup": set(),
    "game_over": set(),
}
INTERRUPT_KINDS = ("combat", "dialogue", "prompt", "hostile", "hp", "damage", "surface", "teleport", "trap")
# A trap spotted during a walk stops it when it lies on the way: a moving party member heads toward it (within
# TRAP_AHEAD_M ahead, TRAP_SIDE_M to the side, same floor), or stands within TRAP_CLOSE_M of it. A skilled player
# stops at the orange outline instead of walking on into it (S021-S025: 'spotted kind=trap' only in EVENTS; live
# 13:0x Crèche Hatchery: two Repulsion Mines spotted mid-walk, the walk went on into Burning Acid, HP 49 -> 40).
TRAP_AHEAD_M, TRAP_SIDE_M, TRAP_CLOSE_M, TRAP_FLOOR_M = 10.0, 2.5, 2.0, 5.0  # floor: live mines sat 4.5 m over the walker's y
TRAP_MOVING_M = 0.25  # moved at least this much since the last poll: walking
TRAP_SAFE_TOOLS = {"disarm"}  # steps that go to the trap on purpose


class StepError(ValueError):
    """A step list that can't be run (bad JSON, unknown tool, bad condition)."""


# ---------------------------------------------------------------------------
# steps and conditions
# ---------------------------------------------------------------------------

PREDICATES = ("in_combat", "in_dialogue", "my_turn", "hp_below", "item_present", "target_visible")


@dataclass
class Step:
    tool: str
    args: dict
    cond_if: list = field(default_factory=list)  # [(pred, arg)] all must hold
    cond_unless: list = field(default_factory=list)  # [(pred, arg)] none may hold

    def short(self) -> str:
        """`interact e5`, `move_to 12,0,-3`: what the agent wrote, briefly."""
        a = self.args
        main = next((a[k] for k in ("target", "item", "npc", "corpse", "trap", "ability", "direction",
                                    "objective", "marker", "option", "op", "action") if a.get(k) not in (None, "")), None)
        if main is None and a.get("x") is not None:
            main = f"{a.get('x')},{a.get('y')},{a.get('z')}"
        return f"{self.tool} {main}" if main is not None else self.tool


def _parse_conds(v: Any) -> list:
    if v in (None, "", []):
        return []
    items = v if isinstance(v, list) else [x for x in str(v).split(",") if x.strip()]
    out = []
    for it in items:
        name, _, arg = str(it).strip().partition(":")
        name = name.strip().lower()
        if name not in PREDICATES:
            raise StepError(f"unknown condition '{name}' (use {', '.join(PREDICATES)})")
        if name in ("hp_below", "item_present", "target_visible") and not arg.strip():
            raise StepError(f"condition '{name}' needs an argument, e.g. {name}:" + ("50" if name == "hp_below" else "e12"))
        out.append((name, arg.strip()))
    return out


def parse_steps(raw: Any, allowed: set) -> list[Step]:
    try:
        steps = json.loads(raw) if isinstance(raw, str) else raw
        if isinstance(steps, dict):
            steps = steps.get("actions", [steps])
        assert isinstance(steps, list) and steps
    except Exception:
        raise StepError('actions must be a JSON list of {"tool": ..., args...}') from None
    if len(steps) > MAX_STEPS:
        raise StepError(f"at most {MAX_STEPS} steps per call")
    out = []
    for i, st in enumerate(steps, 1):
        if not isinstance(st, dict):
            raise StepError(f"step {i} is not an object")
        st = dict(st)
        name = st.pop("tool", None)
        if name not in allowed:
            raise StepError(f"step {i}: '{name}' can't be batched or queued")
        c_if, c_unless = _parse_conds(st.pop("if", None)), _parse_conds(st.pop("unless", None))
        st.pop("wait", None)  # steps always run to completion
        if isinstance(st.get("targets"), (list, tuple)):
            # use_ability takes targets as a comma list; a plan's ["e1","e2"] reached targets.split(",") and failed
            # the whole step "'list' object has no attribute 'split'" (audit ACT-8).
            st["targets"] = ",".join(str(t) for t in st["targets"] if str(t).strip())
        out.append(Step(name, st, c_if, c_unless))
    return out


# ---------------------------------------------------------------------------
# watcher: the interrupt checks (own LuaDebug connection, shared by all runs)
# ---------------------------------------------------------------------------

def _pct(row: dict) -> float | None:
    hp, mx = row.get("hp"), row.get("max_hp")
    if isinstance(hp, (int, float)) and isinstance(mx, (int, float)) and mx > 0:
        return 100.0 * hp / mx
    return None


class Watcher:
    """Detects the default interrupts for one run of steps. check() is thread-safe."""

    def __init__(self, game, hp_below: float = HP_BELOW_PCT, ignore: set | None = None):
        self.game = game
        self.hp_below = hp_below
        self.ignore = set(ignore or ())
        self._lock = threading.Lock()
        self.events: list[dict] = []  # every event seen during the run (for the diff)
        self.start: dict = {}
        self.last: dict = {}
        self._seq = {"server": 0, "client": 0}
        self._screen = "game"
        self._ui_at = 0.0
        self._hp: dict[str, float] = {}
        self._chip: dict[str, float] = {}  # trivial damage outside combat this run, percent of max HP
        self._surf: dict[str, str | None] = {}
        self._hostiles: set[str] = set()
        self._downed: set[str] = set()
        self._traps: dict[str, list] = {}  # spotted during this run, not yet on anyone's way: uuid -> pos
        self._pos: dict[str, list] = {}  # party positions at the previous poll

    def _watch(self) -> dict:
        w = self.game.try_rpc("act.watch") or {}
        return w if isinstance(w, dict) else {}

    def _screen_now(self) -> str:
        ui = self.game.try_rpc("ui.state", context="client") or {}
        return (ui.get("screen") or "game") if isinstance(ui, dict) else "game"

    def baseline(self) -> None:
        with self._lock:
            for ctx in ("server", "client"):
                res = self.game.try_rpc("events.since", {"seq": 10**12, "limit": 1}, context=ctx) or {}
                self._seq[ctx] = res.get("latest", 0) if isinstance(res, dict) else 0
            self.start = self.last = self._watch()
            self._screen = self._screen_now()
            self._ui_at = time.monotonic()
            for row in self.start.get("party") or []:
                p = _pct(row)
                if p is not None:
                    self._hp[row.get("uuid")] = p
                self._surf[row.get("uuid")] = row.get("surface")
            self._hostiles = {s.get("uuid") for s in self.start.get("seen") or [] if s.get("hostile")}
            # Already down when the run started: not news (S018: every combat_turn, the one healing Claude
            # too, stopped on its first step with "e4 Claude is downed").
            self._downed = {row.get("uuid") for row in self.start.get("party") or [] if row.get("downed")}
            self._pos = {row.get("uuid"): row.get("pos") for row in self.start.get("party") or [] if row.get("pos")}

    def settle(self) -> None:
        """After a step that was meant to change the screen (talk_to, choose_dialogue): accept it."""
        with self._lock:
            self._screen = self._screen_now()
            self._ui_at = time.monotonic()

    def _new_events(self) -> list[dict]:
        out = []
        for ctx in ("server", "client"):
            res = self.game.try_rpc("events.since", {"seq": self._seq[ctx], "limit": 200}, context=ctx)
            if not isinstance(res, dict):
                continue
            evs = res.get("events") or []
            if evs:
                self._seq[ctx] = evs[-1].get("seq", self._seq[ctx])
            out.extend(evs)
        self.events.extend(out)
        return out

    def _party_check(self, w: dict) -> tuple[str, str] | None:
        """HP (downed, below the threshold, any damage outside combat) and harmful surfaces underfoot.
        Surface: only stepping INTO one (or into a different one) counts, so a walk that starts in a fire
        to get out of it is not stopped by it."""
        hit = None
        for row in w.get("party") or []:
            u, p = row.get("uuid"), _pct(row)
            surf, was = row.get("surface"), self._surf.get(u)
            self._surf[u] = surf
            if hit is None and surf and surf != was and "surface" not in self.ignore:
                hit = "surface", f"{u} is standing in {surf}"
            if row.get("downed"):
                if u not in self._downed and "hp" not in self.ignore:
                    self._downed.add(u)
                    return "hp", f"{u} is downed"
                continue
            self._downed.discard(u)  # back up: going down again is news
            if p is None:
                continue
            before = self._hp.get(u, 100.0)
            self._hp[u] = p
            if p < before - 0.1:
                if p < self.hp_below and "hp" not in self.ignore:
                    return "hp", f"{u} HP {row.get('hp')}/{row.get('max_hp')}"
                chip = self._chip.get(u, 0.0) + (before - p)
                if before - p < DAMAGE_MIN_PCT and chip < DAMAGE_SUM_PCT:
                    self._chip[u] = chip
                    continue
                self._chip[u] = 0.0
                if not w.get("combat") and "damage" not in self.ignore and hit is None:
                    hit = "damage", f"{u} took damage outside combat (HP {row.get('hp')}/{row.get('max_hp')})" \
                        + (f" in {surf}" if surf else "")
        return hit

    def _trap_check(self, w: dict, tool: str | None) -> tuple[str, str] | None:
        """A trap spotted during this run that lies on a walker's way (see TRAP_AHEAD_M)."""
        rows = [r for r in w.get("party") or [] if isinstance(r.get("pos"), (list, tuple)) and len(r["pos"]) == 3]
        prev, self._pos = self._pos, {r.get("uuid"): r["pos"] for r in rows}
        if not self._traps or "trap" in self.ignore or tool in TRAP_SAFE_TOOLS:
            return None
        for tu, tp in list(self._traps.items()):
            for r in rows:
                (x, y, z), was = r["pos"], prev.get(r.get("uuid"))
                if abs(tp[1] - y) > TRAP_FLOOR_M:
                    continue
                dx, dz = tp[0] - x, tp[2] - z
                d = (dx * dx + dz * dz) ** 0.5
                hit = d <= TRAP_CLOSE_M
                if not hit and was and len(was) == 3:
                    vx, vz = x - was[0], z - was[2]
                    v = (vx * vx + vz * vz) ** 0.5
                    if v >= TRAP_MOVING_M:
                        ahead = (dx * vx + dz * vz) / v
                        side = abs(dx * vz - dz * vx) / v
                        hit = 0 < ahead <= TRAP_AHEAD_M and side <= TRAP_SIDE_M
                if hit:
                    del self._traps[tu]
                    return "trap", (f"TRAP spotted on the way: {tu} {d:.1f}m from {r.get('uuid')}; the walk stopped "
                                    "short of it (disarm it, or walk around it)")
        return None

    def check(self, tool: str | None, next_tool: str | None, ui: bool | None = None) -> tuple[str, str] | None:
        """(kind, reason) if the run must stop, else None. tool = the step running (or just run)."""
        with self._lock:
            handled = {tool, next_tool}
            for ev in self._new_events():
                k = ev.get("kind")
                if k == "teleport_blocked" and "teleport" not in self.ignore:
                    return "teleport", "the engine tried to teleport a character (blocked)"
                if k == "combat_started" and "combat" not in self.ignore and not self.start.get("combat"):
                    return "combat", "combat started"
                if k == "dialogue_started" and "dialogue" not in self.ignore and not handled & PROMPTS["dialogue"] \
                        and not (ev.get("data") or ev).get("automated"):
                    return "dialogue", "a conversation started"
                if k == "spotted":
                    d = ev.get("data") or {}
                    item, pos = d.get("item") or {}, d.get("pos")
                    if d.get("kind") == "trap" and isinstance(pos, (list, tuple)) and len(pos) == 3 \
                            and isinstance(item, dict) and item.get("uuid"):
                        self._traps[item["uuid"]] = list(pos)
            w = self._watch()
            if w:
                self.last = w
                if w.get("combat") and not self.start.get("combat") and "combat" not in self.ignore:
                    return "combat", "combat started"
                hit = self._party_check(w) or self._trap_check(w, tool)
                if hit:
                    return hit
                if "hostile" not in self.ignore:
                    for s in w.get("seen") or []:
                        if s.get("hostile") and not s.get("dead") and s.get("uuid") not in self._hostiles:
                            self._hostiles.add(s.get("uuid"))
                            return "hostile", f"hostile in view: {s.get('uuid')}"
            now = time.monotonic()
            if ui or (ui is None and now - self._ui_at >= UI_EVERY_S):
                self._ui_at = now
                scr = self._screen_now()
                if scr in PROMPTS and scr != self._screen and "prompt" not in self.ignore \
                        and not handled & PROMPTS[scr]:
                    self._screen = scr
                    return "prompt", f"{scr} on screen"
                if scr not in PROMPTS:
                    self._screen = scr
        return None


# ---------------------------------------------------------------------------
# executor: runs steps; a monitor thread cancels the running job on an interrupt
# ---------------------------------------------------------------------------

@dataclass
class Report:
    lines: list = field(default_factory=list)
    done: int = 0
    skipped: int = 0
    stopped: str | None = None  # why, if it stopped early
    stop_kind: str | None = None
    left: list = field(default_factory=list)  # Step.short() of steps not run
    last_result: Any = None


class Executor:
    """One run of steps for one character.

    call_tool(step, who) -> (text, status, result): runs the tool in the current thread (the server
    makes it batch-quiet and wires _act's on_start to self.track).
    predicate(name, arg, who, watch) -> bool.
    """

    def __init__(self, call_tool: Callable, watcher: Watcher, cancel_job: Callable, who: str | None = None,
                 predicate: Callable | None = None):
        self.call_tool = call_tool
        self.watcher = watcher
        self.cancel_job = cancel_job
        self.who = who
        self.predicate = predicate
        self.current: tuple[str, int] | None = None
        self.current_step: Step | None = None
        self.stop = threading.Event()  # cancel_queue
        self.interrupt: tuple[str, str] | None = None
        self._lock = threading.Lock()

    def track(self, context: str, job_id: int) -> None:
        with self._lock:
            self.current = (context, job_id)
            if self.interrupt or self.stop.is_set():
                self._cancel_current()

    def _cancel_current(self) -> None:
        if self.current:
            ctx, jid = self.current
            try:
                self.cancel_job(ctx, jid)
            except Exception:
                pass

    def halt(self, kind: str, why: str) -> None:
        with self._lock:
            if self.interrupt is None:
                self.interrupt = (kind, why)
            self._cancel_current()

    def _monitor(self, done: threading.Event, peek_next: Callable) -> None:
        while not done.wait(WATCH_EVERY_S):
            if self.stop.is_set():
                self.halt("cancel", "cancelled")
                return
            st = self.current_step
            nxt = peek_next()
            try:
                hit = self.watcher.check(st.tool if st else None, nxt.tool if nxt else None)
            except Exception:
                hit = None
            if hit:
                self.halt(*hit)
                return

    def _conds_ok(self, step: Step) -> tuple[bool, str]:
        if not (step.cond_if or step.cond_unless) or self.predicate is None:
            return True, ""
        w = self.watcher.last or {}
        who = step.args.get("who") or self.who
        for name, arg in step.cond_if:
            if not self.predicate(name, arg, who, w):
                return False, f"if {name}{':' + arg if arg else ''}"
        for name, arg in step.cond_unless:
            if self.predicate(name, arg, who, w):
                return False, f"unless {name}{':' + arg if arg else ''}"
        return True, ""

    def run(self, next_step: Callable[[], Step | None], peek_next: Callable[[], Step | None]) -> Report:
        rep = Report()
        done = threading.Event()
        mon = threading.Thread(target=self._monitor, args=(done, peek_next), name="actq-monitor", daemon=True)
        mon.start()
        i = 0
        try:
            while True:
                if self.stop.is_set() and not self.interrupt:
                    self.interrupt = ("cancel", "cancelled")
                if self.interrupt:
                    break
                step = next_step()
                if step is None:
                    break
                i += 1
                ok, why = self._conds_ok(step)
                if not ok:
                    rep.skipped += 1
                    rep.lines.append(f"{i}. {step.short()}: skipped ({why})")
                    continue
                self.current_step = step
                self.current = None
                if step.tool == "wait":
                    text, status = self._wait_step(step), "done"
                    result = None
                else:
                    try:
                        text, status, result = self.call_tool(step, self.who)
                    except TypeError as e:
                        text, status, result = f"{step.tool}: bad arguments ({e})", "failed", None
                    except Exception as e:  # GameError etc. become a failed step
                        text, status, result = f"{step.tool}: FAILED: {e}", "failed", None
                self.current_step = None
                rep.lines.append(f"{i}. {text}")
                if status == "done":
                    rep.done += 1
                    rep.last_result = result if result is not None else rep.last_result
                    if step.tool in ("talk_to", "choose_dialogue", "roll_check", "react"):
                        self.watcher.settle()
                    if not self.interrupt:
                        nxt = peek_next()
                        hit = self.watcher.check(step.tool, nxt.tool if nxt else None, ui=False)
                        if hit:
                            self.interrupt = hit
                    continue
                if not self.interrupt:
                    self.interrupt = ("failed", f"step {i} {step.short()} {status}")
                break
        finally:
            done.set()
        if self.interrupt:
            rep.stop_kind, rep.stopped = self.interrupt
            while True:
                s = next_step()
                if s is None:
                    break
                rep.left.append(s.short())
        return rep

    def _wait_step(self, step: Step) -> str:
        secs = max(0.2, min(float(step.args.get("seconds", 3.0) or 3.0), 30.0))
        end = time.monotonic() + secs
        while time.monotonic() < end and not self.interrupt and not self.stop.is_set():
            time.sleep(0.05)
        return f"wait: waited {round(secs - max(0.0, end - time.monotonic()), 1)}s"


# ---------------------------------------------------------------------------
# per-character queues
# ---------------------------------------------------------------------------

@dataclass
class CharQueue:
    who: str
    steps: collections.deque = field(default_factory=collections.deque)
    thread: threading.Thread | None = None
    executor: Executor | None = None
    ran: int = 0
    last_report: Report | None = None
    finished_at: float = 0.0


class QueueManager:
    """FIFO per character, each run by a worker thread while steps remain.

    make_executor(who) -> Executor; on_report(who, report) is called when a run ends (the server turns
    it into a queue_done / queue_interrupted event).
    """

    def __init__(self, make_executor: Callable[[str], Executor], on_report: Callable[[str, Report], None]):
        self.make_executor = make_executor
        self.on_report = on_report
        self.queues: dict[str, CharQueue] = {}
        self.lock = threading.RLock()

    def add(self, who: str, steps: list[Step], replace: bool = False) -> CharQueue:
        with self.lock:
            q = self.queues.setdefault(who, CharQueue(who))
            if replace:
                q.steps.clear()
                if q.executor is not None:
                    q.executor.stop.set()
                    q.executor.halt("cancel", "replaced by a new queue")
                    self._join_locked(q)
            q.steps.extend(steps)
            if q.thread is None or not q.thread.is_alive():
                q.executor = self.make_executor(who)
                q.thread = threading.Thread(target=self._work, args=(q, q.executor), name=f"actq-{who[:8]}",
                                            daemon=True)
                q.thread.start()
            return q

    def _join_locked(self, q: CharQueue) -> None:
        t = q.thread
        if t is not None and t.is_alive() and t is not threading.current_thread():
            self.lock.release()
            try:
                t.join(5.0)
            finally:
                self.lock.acquire()

    def _work(self, q: CharQueue, ex: Executor) -> None:
        q.ran = 0

        def nxt() -> Step | None:
            with self.lock:
                if not q.steps:
                    return None
                q.ran += 1
                return q.steps.popleft()

        def peek() -> Step | None:
            with self.lock:
                return q.steps[0] if q.steps else None

        try:
            ex.watcher.baseline()
            rep = ex.run(nxt, peek)
        except Exception as e:  # never die silently
            rep = Report(stopped=f"queue error: {e}", stop_kind="error")
        with self.lock:
            q.last_report = rep
            q.finished_at = time.time()
        try:
            self.on_report(q.who, rep)
        except Exception:
            pass

    def cancel(self, who: str | None = None) -> list[tuple[str, int, str | None]]:
        """Flush queue(s) and stop the running step. Returns [(who, steps dropped, running step)]."""
        out = []
        with self.lock:
            for w, q in list(self.queues.items()):
                if who is not None and w != who:
                    continue
                n = len(q.steps)
                q.steps.clear()
                running = None
                if q.thread is not None and q.thread.is_alive() and q.executor is not None:
                    st = q.executor.current_step
                    running = st.short() if st else None
                    q.executor.stop.set()
                    q.executor.halt("cancel", "cancelled")
                if n or running:
                    out.append((w, n, running))
        return out

    def busy(self, who: str | None = None) -> bool:
        with self.lock:
            return any((q.thread is not None and q.thread.is_alive()) or q.steps
                       for w, q in self.queues.items() if who is None or w == who)

    def status(self) -> list[dict]:
        with self.lock:
            out = []
            for w, q in self.queues.items():
                alive = q.thread is not None and q.thread.is_alive()
                if not alive and not q.steps:
                    continue
                st = q.executor.current_step if q.executor else None
                out.append({"who": w, "running": st.short() if st else None, "waiting": [s.short() for s in q.steps],
                            "ran": q.ran})
            return out

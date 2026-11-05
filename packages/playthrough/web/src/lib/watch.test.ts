import { describe, expect, it } from "vitest";
import type { Action, LogEntry, WatchSnapshot } from "./types";
import { applyEvent, fromSnapshot, LIMITS, says, screenSrc, secondsLeft } from "./watch";

const action = (t: number, tool = "look", kind: Action["kind"] = "read"): Action => ({
  t, tool, args: {}, ms: 5, result: `r${t}`, ok: true, kind,
});
const say = (clock: string, text: string): LogEntry => ({ clock, kind: "say", text, session: "S001", playthrough: "pt" });

function snap(over: Partial<WatchSnapshot> = {}): WatchSnapshot {
  return {
    v: 1, playthrough: "pt", runner: { playthrough: "pt", session: "S001", deadline: 1000 }, session: "S001",
    log: [say("10:00:00", "hello")], actions: [action(1)], party: null, goal: "free the gnome",
    cost: { total_usd: 10, last_session_usd: 4, sessions: 2 }, latency: null, screenshot: { t: 50, name: "a.jpg" },
    stream_id: "g-3", ...over,
  };
}

describe("watch reducer", () => {
  it("starts from the snapshot", () => {
    const s = fromSnapshot(snap());
    expect(s.live).toBe(true);
    expect(s.screenshotRev).toBe(50);
    expect(fromSnapshot(snap({ runner: null })).live).toBe(false);
  });

  it("appends log lines and tool calls in order and drops repeats from the snapshot overlap", () => {
    let s = fromSnapshot(snap());
    s = applyEvent(s, "log", say("10:00:00", "hello")); // already in the snapshot
    s = applyEvent(s, "log", say("10:00:05", "north"));
    s = applyEvent(s, "action", action(1)); // repeat
    s = applyEvent(s, "action", action(2));
    expect(s.log.map((e) => e.text)).toEqual(["hello", "north"]);
    expect(s.actions.map((a) => a.t)).toEqual([1, 2]);
    expect(says(s).map((e) => e.text)).toEqual(["north", "hello"]);
  });

  it("caps the lists", () => {
    let s = fromSnapshot(snap({ actions: [] }));
    for (let i = 0; i < LIMITS.actions + 30; i++) s = applyEvent(s, "action", action(i));
    expect(s.actions.length).toBe(LIMITS.actions);
    expect(s.actions[0]?.t).toBe(30);
  });

  it("ignores other playthroughs and the shared action feed when not live", () => {
    let s = fromSnapshot(snap({ runner: null }));
    const before = s;
    s = applyEvent(s, "action", action(9));
    s = applyEvent(s, "party", { where: "x", mode: null, gold: 1, party: [], t: 1, source: "get_status" });
    s = applyEvent(s, "log", { ...say("10:00:09", "elsewhere"), playthrough: "other" });
    expect(s).toBe(before);
  });

  it("status of another playthrough ends live; status of this one starts it", () => {
    let s = fromSnapshot(snap());
    s = applyEvent(s, "status", { playthrough: "other" });
    expect(s.live).toBe(false);
    expect(s.runner).toBeNull();
    s = applyEvent(s, "status", { playthrough: "pt", session: "S002" });
    expect(s.live).toBe(true);
    expect(s.runner?.session).toBe("S002");
  });

  it("a new session is marked in the log", () => {
    const s = applyEvent(fromSnapshot(snap()), "session", { playthrough: "pt", session: "S002" });
    expect(s.session).toBe("S002");
    expect(s.log.at(-1)).toMatchObject({ kind: "header", text: "session S002" });
    expect(s.actions).toEqual([]); // the calls shown are the new session's own
  });

  it("the game panel shows the recorder's live view while it records, else the agent's screenshot", () => {
    expect(screenSrc(true, fromSnapshot(snap({ screenshot: null })))).toBe("/api/v1/screen.mjpg");
    expect(screenSrc(true, fromSnapshot(snap()))).toBe("/api/v1/screen.mjpg");
    expect(screenSrc(false, fromSnapshot(snap()))).toBe("/api/v1/screenshot?rev=50");
    expect(screenSrc(false, fromSnapshot(snap({ screenshot: null })))).toBeNull();
  });

  it("the [end] line adds the session's cost", () => {
    const s = applyEvent(fromSnapshot(snap()), "log", {
      clock: "11:00:00", kind: "end", text: "success turns=3 duration=1.0min cost=$2.50", playthrough: "pt",
    });
    expect(s.cost).toEqual({ total_usd: 12.5, last_session_usd: 2.5, sessions: 3 });
  });

  it("party, screenshots, actions_reset and memory", () => {
    let s = fromSnapshot(snap());
    s = applyEvent(s, "party", { where: "Grove", mode: "exploring", gold: 3, party: [], t: 2, source: "look" });
    expect(s.party?.where).toBe("Grove");
    s = applyEvent(s, "action", action(3, "screenshot", "vision"));
    expect(s.screenshotRev).toBe(51);
    s = applyEvent(s, "actions_reset", {});
    expect(s.actions).toEqual([]);
    expect(s.party).toBeNull();
    s = applyEvent(s, "memory", { sha: "a", t: 1, subject: "S001", session: "S001", playthrough: "pt" });
    s = applyEvent(s, "memory", { sha: "b", t: 2, subject: "x", session: null, playthrough: "other" });
    expect(s.memory.map((c) => c.sha)).toEqual(["a"]);
    expect(applyEvent(s, "unknown", {})).toBe(s);
  });

  it("operator messages: the snapshot's, then this playthrough's stream rows", () => {
    const row = { id: "m1", t: 5, text: "go north", status: "queued" as const };
    expect(fromSnapshot(snap()).operator).toEqual({ enabled: false, messages: [] }); // a server without them
    let s = fromSnapshot(snap({ operator: { enabled: true, messages: [row] } }));
    expect(s.operator.enabled).toBe(true);
    const done = { ...row, status: "delivered" as const, delivered: { t: 9, session: "S001", tool: "travel_to" } };
    s = applyEvent(s, "operator", { ...done, playthrough: "pt" });
    expect(s.operator.messages.map((m) => m.status)).toEqual(["delivered"]);
    s = applyEvent(s, "operator", { ...row, playthrough: "pt" }); // the stream's replay of its queued row
    expect(s.operator.messages.map((m) => m.status)).toEqual(["delivered"]);
    const before = s;
    expect(applyEvent(s, "operator", { ...row, id: "m2", playthrough: "other" })).toBe(before);
    expect(applyEvent(s, "operator", { ...row, id: "m2" }).operator.messages.map((m) => m.id)).toEqual(["m2", "m1"]);
    const idle = fromSnapshot(snap({ runner: null, operator: { enabled: true, messages: [] } }));
    expect(applyEvent(idle, "operator", { ...row, id: "m2" })).toBe(idle); // no playthrough named: the live one's
  });

  it("seconds left", () => {
    expect(secondsLeft({ playthrough: "pt", deadline: 1000 }, 940)).toBe(60);
    expect(secondsLeft({ playthrough: "pt", deadline: 1000, session_ended: 990 }, 940)).toBeNull();
    expect(secondsLeft(null, 1)).toBeNull();
  });
});

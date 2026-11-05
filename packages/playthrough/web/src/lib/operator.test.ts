import { describe, expect, it } from "vitest";
import { clock } from "./format";
import { canSend, guidedLabel, guidedLine, guidedSessions, MAX_SHOWN, mergeMessage, nextAction, sendError, sessionLive, statusText, textProblem } from "./operator";
import type { Action, OperatorMessage } from "./types";

const queued = (id: string, t: number, text = `text ${id}`): OperatorMessage => ({ id, t, text, status: "queued" });
const delivered = (id: string, t: number, at: number, session = "S004", tool = "mcp__bg3__travel_to"): OperatorMessage => ({
  ...queued(id, t), status: "delivered", delivered: { t: at, session, tool },
});
const call = (t: number, tool: string): Action => ({ t, tool, args: {}, ms: 5, result: "", ok: true, kind: "act" });

describe("operator messages", () => {
  it("merges snapshot and stream rows newest first, one per id", () => {
    let l = [queued("m1", 10)].reduce(mergeMessage, [] as OperatorMessage[]);
    l = mergeMessage(l, queued("m2", 20));
    l = mergeMessage(l, delivered("m1", 10, 30));
    expect(l.map((m) => [m.id, m.status])).toEqual([["m2", "queued"], ["m1", "delivered"]]);
  });

  it("a replayed queued row never undoes a delivery", () => {
    const l = [delivered("m1", 10, 30)];
    expect(mergeMessage(l, queued("m1", 10))).toBe(l);
  });

  it("keeps the newest MAX_SHOWN", () => {
    let l: OperatorMessage[] = [];
    for (let i = 0; i < MAX_SHOWN + 5; i++) l = mergeMessage(l, queued(`m${i}`, i));
    expect(l.length).toBe(MAX_SHOWN);
    expect(l[0]?.id).toBe(`m${MAX_SHOWN + 4}`);
  });

  it("says where a message is", () => {
    expect(statusText(queued("m1", 1), true)).toBe("queued");
    expect(statusText(queued("m1", 1), false)).toBe("queued (next session)");
    expect(statusText(delivered("m1", 1, 1792311200), true)).toBe(`delivered ${clock(1792311200)} in S004 via travel_to`);
    expect(sessionLive(true, { playthrough: "pt", session: "S004" })).toBe(true);
    expect(sessionLive(true, { playthrough: "pt", session: "S004", session_ended: 5 })).toBe(false);
    expect(sessionLive(false, null)).toBe(false);
  });

  it("finds the agent's first call after the one that carried the message", () => {
    const m = delivered("m1", 10, 100);
    // the walk started before the message and is logged after its result, which carried the message
    const calls = [call(90, "look"), call(99, "mcp__bg3__look"), call(101, "travel_to"), call(102, "look"), call(103, "talk_to")];
    expect(nextAction(m, calls, "S004")?.t).toBe(102);
    expect(nextAction(m, calls.slice(0, 3), "S004")).toBeNull(); // nothing since
    expect(nextAction(m, calls, "S005")).toBeNull(); // another session's calls
    expect(nextAction(queued("m2", 10), calls, "S004")).toBeNull();
  });

  it("labels guided sessions", () => {
    expect(guidedLabel({ enabled: true, sent: 3, delivered: 2 })).toBe("guided (3 messages)");
    expect(guidedLabel({ enabled: true, sent: 1, delivered: 1 })).toBe("guided (1 message)");
    expect(guidedLabel({ enabled: true, sent: 0, delivered: 0 })).toBe("guided (0 messages)");
    expect(guidedLabel({ enabled: false, sent: 0, delivered: 0 })).toBeNull();
    expect(guidedLabel(null)).toBeNull();
    expect(guidedLabel(undefined)).toBeNull();
  });

  it("lists the guided sessions", () => {
    const g = guidedSessions([
      { session: "S003", started: 1, minutes: 5 },
      { session: "S004", started: 2, minutes: 5, operator: { enabled: true, sent: 2, delivered: 2 } },
      { session: "S005", started: 3, minutes: 5, operator: null },
      { session: "S006", started: 4, minutes: 5, operator: { enabled: true, sent: 1, delivered: 0 } },
    ]);
    expect(g).toEqual({ S004: "guided (2 messages)", S006: "guided (1 message)" });
    expect(guidedLine(g)).toBe("S004 guided (2 messages) · S006 guided (1 message)");
    expect(guidedLine({})).toBeNull();
  });

  it("checks the text and who may send", () => {
    expect(textProblem("  \n ")).toBe("Write a message first");
    expect(textProblem("x".repeat(1000))).toBeNull();
    expect(textProblem("x".repeat(1001))).toBe("At most 1000 characters");
    expect(canSend("localhost", null)).toBe(true);
    expect(canSend("127.0.0.1", null)).toBe(true);
    expect(canSend("127.1.2.3", null)).toBe(true);
    expect(canSend("[::1]", null)).toBe(true);
    expect(canSend("dev2.tailnet", null)).toBe(false);
    expect(canSend("dev2.tailnet", "tok")).toBe(true);
    expect(canSend("127.example.com", null)).toBe(false);
    expect(sendError(403, "bad or missing token")).toBe("needs the operator token");
    expect(sendError(429, "wait 5 s between messages")).toBe("wait 5 s between messages");
  });
});

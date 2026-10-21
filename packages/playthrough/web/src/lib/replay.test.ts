import { describe, expect, it } from "vitest";
import { coverage, indexAt, locate, recent, step, ticks } from "./replay";
import type { ReplayEvent, Segment } from "./types";

const ev = (t: number, kind: "action" | "say" = "action"): ReplayEvent =>
  kind === "say" ? { t, kind, say: "say", text: `s${t}` } : { t, kind, tool: "look", args: {}, ms: 1, ok: true, result: "" };
const seg = (file: string, start: number, end: number | null): Segment => ({ file, run: "r", start, end, bytes: 1 });

describe("replay", () => {
  const events = [ev(10), ev(20, "say"), ev(20), ev(30)];

  it("finds the event at a time and steps between events", () => {
    expect(indexAt(events, 5)).toBe(-1);
    expect(indexAt(events, 20)).toBe(2);
    expect(indexAt(events, 99)).toBe(3);
    expect(step(events, 10, 1)).toBe(20);
    expect(step(events, 20, 1)).toBe(30);
    expect(step(events, 30, 1)).toBe(30);
    expect(step(events, 25, -1)).toBe(20);
    expect(step(events, 10, -1)).toBe(10);
    expect(recent(events, 20, 2).map((e) => e.t)).toEqual([20, 20]);
  });

  it("maps a time to a video segment and offset, like recorder.locate", () => {
    const segs = [seg("a", 0, 600), seg("b", 600, null)];
    expect(locate(segs, -1)).toBeNull();
    expect(locate(segs, 12.5)).toEqual({ seg: segs[0], offset: 12.5 });
    expect(locate(segs, 600)?.seg.file).toBe("b");
    expect(locate([seg("a", 0, 10), seg("c", 20, 30)], 15)).toBeNull(); // a gap
  });

  it("scrubber ticks and video coverage", () => {
    expect(ticks(events, 10, 30)).toEqual([{ pct: 50, kind: "say" }]);
    expect(coverage([seg("a", 0, 15), seg("b", 25, null)], 10, 30)).toEqual([[0, 25], [75, 100]]);
  });
});

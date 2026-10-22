import { describe, expect, it } from "vitest";
// the real e2e run's bench output (tests/fixtures/bench; test_bench.py validates them against the bench schemas)
import claudeMan from "../../../tests/fixtures/bench/e2e-naut-claude-r001/manifest.json?raw";
import claudeRes from "../../../tests/fixtures/bench/e2e-naut-claude-r001/result.json?raw";
import piRes from "../../../tests/fixtures/bench/e2e-naut-pi-r001/result.json?raw";
import { counters, curve, dur, goldRows, luckLine, marks, thinkPct, tokens, verdict } from "./bench";
import type { BenchManifest, BenchResult } from "./types";

const claude = JSON.parse(claudeRes) as BenchResult;
const pi = JSON.parse(piRes) as BenchResult;
const man = JSON.parse(claudeMan) as BenchManifest;

describe("bench", () => {
  it("gives the verdict: pass, fail with the outcome, invalid with the reason", () => {
    expect(verdict(claude)).toEqual({ text: "pass", level: "ok" });
    expect(verdict(pi)).toEqual({ text: "fail (over budget)", level: "bad" });
    expect(verdict({ ...pi, valid: false, invalid_reason: "harness_failure" })).toEqual({
      text: "invalid (harness_failure)", level: "warn",
    });
  });

  it("formats durations, tokens and the think share of the wall clock", () => {
    expect(dur(1042.7)).toBe("17m 23s");
    expect(dur(45)).toBe("45s");
    expect(dur(null)).toBe("–");
    expect([tokens(378), tokens(28342), tokens(14683826)]).toEqual(["378", "28.3 k", "14.7 M"]);
    expect(thinkPct(claude)).toBe(43); // 451.36 / 1050
    expect(thinkPct({ ...claude, wall_s: null })).toBeNull();
  });

  it("draws only the checkpoints with a value", () => {
    const c = curve(claude.grade!.curve.points, 240, 60, 4)!;
    expect(c.dots).toEqual([{ x: 81.3, y: 4, at_s: 600, value: 4, peak: 4 }]); // 600 of 1800 s: 4 + 232/3; the top
    expect(c.line).toBe("81.3,4");
    expect(c.max).toBe(4);
    expect(c.ticks).toEqual([
      { x: 81.3, at_s: 600, reached: true }, { x: 158.7, at_s: 1200, reached: false }, { x: 236, at_s: 1800, reached: false },
    ]);
    const p = curve(pi.grade!.curve.points)!;
    expect(p.dots.map((d) => d.value)).toEqual([0, 0, 18]);
    expect(p.line.split(" ")).toHaveLength(3);
    expect(curve([{ at_s: 600, value: null, peak: null }])).toBeNull();
  });

  it("splits the gold by method and lists the non-zero counters", () => {
    expect(goldRows(pi.methods!)[0]).toEqual({ method: "looted", gold: 18, pct: 100 });
    expect(goldRows(pi.methods!).map((r) => r.pct)).toEqual([100, 0, 0, 0, 0]);
    expect(counters(pi.methods!)).toEqual([]);
    expect(counters({ ...pi.methods!, kills: 3, spent: 20 })).toEqual(["20 gold spent", "3 kills"]);
  });

  it("states the luck z from the party's view", () => {
    expect(luckLine(null)).toBe("no roll log");
    expect(luckLine({ n: 40, z: -1.25, party: 31, other: 9, skipped: 2, nat20: 3, nat1: 1 })).toBe(
      "z −1.25 over 40 rolls · 3 nat 20 · 1 nat 1",
    );
  });

  it("marks the goal, the gold changes and timed choices inside the session", () => {
    const withPick: BenchResult = {
      ...claude,
      choices: { picks: [{ t: 1792620500, scene: "Pod", options: ["a", "b"], picked: 0, pick: "a", reason: null },
                         { t: null, scene: "Later", options: [], picked: null, pick: "x", reason: null }], fit: null },
    };
    // S001 ran 1792619833.2 → 1792620744.4; the run (and t_pass's clock) started at manifest.created 1792619697.6
    const m = marks(man, withPick, 1792619833.2, 1792620744.4);
    expect(m.map((x) => [x.kind, x.label])).toEqual([
      ["gold", "gold +4 (looted)"], ["choice", "Pod: a"], ["pass", "goal reached at 17m 23s"],
    ]);
    expect(m[2]!.pct).toBe(99.6);
    expect(marks(man, claude, 0, 100)).toEqual([]); // another session's window
  });
});

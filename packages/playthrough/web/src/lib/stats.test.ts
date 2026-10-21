import { describe, expect, it } from "vitest";
import { checkAllLine, eventLine, hours, meets, passClass, spark, thermalLine } from "./stats";

const TH = { t: 1000, line: "x", cpu: 51, gpu: 47, pause_cpu_c: 92, pause_gpu_c: 85 };

describe("goals", () => {
  it("judges each op", () => {
    expect([meets("<", 1.9, 2), meets("<", 2, 2), meets("==", 0, 0), meets(">=", 10, 10)]).toEqual([true, false, true, true]);
    expect([passClass(true), passClass(false), passClass(null), passClass(undefined)]).toEqual(["pass", "fail", "", ""]);
  });

  it("draws a sparkline over the points it has, with the target and the last point's verdict", () => {
    expect(spark([1, null], 2, "<")).toBeNull();
    const s = spark([3, null, 1], 2, "<", 90, 20)!;
    expect(s.points).toBe("2,2 88,18");
    expect(s.targetY).toBe(10);
    expect(s.last).toEqual({ x: 88, y: 18, ok: true });
    expect(spark([5, 5], 5, ">=")!.targetY).toBe(10);   // flat: the middle
  });
});

describe("machine lines", () => {
  it("marks heat at the pause limits and stale readings", () => {
    expect(thermalLine(TH, 1010)).toEqual({ text: "CPU 51°C · GPU 47°C", hot: false, stale: false });
    expect(thermalLine({ ...TH, gpu: 85 }, 1045)).toEqual({ text: "CPU 51°C · GPU 85°C (45 s old)", hot: true, stale: true });
    expect(thermalLine({ ...TH, cpu: null }, 1300).text).toBe("CPU ? · GPU 47°C (5 min old)");
  });

  it("summarises check_all", () => {
    expect(checkAllLine({ ok: true, counts: { PASS: 9, WARN: 1, FAIL: 0 }, session: "S004", t: 400 }, 1000))
      .toEqual({ text: "check_all 9 pass, 1 warn, 0 fail after S004 · 10 min ago", level: "warn" });
    expect(checkAllLine({ ok: true, counts: { PASS: 3, FAIL: 2 } }, 0).level).toBe("bad");
    expect(checkAllLine({ ok: false, why: "timed out" }, 0)).toEqual({ text: "check_all timed out", level: "warn" });
  });

  it("formats unattended hours and runner events", () => {
    expect([hours(null), hours(0.5), hours(2.25)]).toEqual(["–", "30 min", "2.3 h"]);
    expect(eventLine({ t: 1, kind: "recovered", fault: "crash", seconds: 40, how: "relaunch" }))
      .toEqual({ text: "recovered crash in 40 s: relaunch", level: "ok" });
    expect(eventLine({ t: 1, kind: "launch_gave_up", error: "3 crashes" }).level).toBe("bad");
    expect(eventLine({ t: 1, kind: "stopped", why: "STOP file" })).toEqual({ text: "runner stopped: STOP file", level: "" });
  });
});

// Pure helpers for the Stats screen: goal pass/fail, the trend sparkline, temperature and unattended lines.
// Ported from the retired runner/dashboard/index.html.

import type { CheckAll, GoalOp, Num, Thermal } from "./types";

export const OP_TEXT: Record<GoalOp, string> = { "<": "<", "==": "=", ">=": "≥" };

export function meets(op: GoalOp, v: number, target: number): boolean {
  return op === "<" ? v < target : op === "==" ? v === target : v >= target;
}

/** "pass" / "fail" / "" for a pass flag (null or missing = not judged). */
export function passClass(p: boolean | null | undefined): "" | "pass" | "fail" {
  return p === null || p === undefined ? "" : p ? "pass" : "fail";
}

export interface Spark {
  points: string;
  targetY: number;
  last: { x: number; y: number; ok: boolean };
}

/** An SVG polyline over the series (gaps skipped) with the target as a dashed line; null below two points. */
export function spark(series: Num[], target: number, op: GoalOp, w = 90, h = 20): Spark | null {
  const pts = series.map((v, i) => [i, v] as const).filter((p): p is readonly [number, number] => p[1] != null);
  if (pts.length < 2) return null;
  const ys = pts.map((p) => p[1]).concat([target]);
  const lo = Math.min(...ys);
  const hi = Math.max(...ys);
  const n = series.length - 1 || 1;
  const x = (i: number) => (i / n) * (w - 4) + 2;
  const y = (v: number) => (hi === lo ? h / 2 : h - 2 - ((v - lo) / (hi - lo)) * (h - 4));
  const [li, lv] = pts[pts.length - 1]!;
  return {
    points: pts.map(([i, v]) => `${round(x(i))},${round(y(v))}`).join(" "),
    targetY: round(y(target)),
    last: { x: round(x(li)), y: round(y(lv)), ok: meets(op, lv, target) },
  };
}

const round = (v: number) => Math.round(v * 10) / 10;

export interface ThermalLine {
  text: string;
  hot: boolean;
  stale: boolean;
}

/** "CPU 51°C · GPU 47°C (2 min old)": hot at the runner's heat-pause limits, stale after 30 s. */
export function thermalLine(th: Thermal, now: number): ThermalLine {
  const c = (v: number | null) => (v === null ? "?" : `${v}°C`);
  const age = Math.round(now - th.t);
  const old = age > 30 ? ` (${age > 120 ? `${Math.round(age / 60)} min` : `${age} s`} old)` : "";
  return {
    text: `CPU ${c(th.cpu)} · GPU ${c(th.gpu)}${old}`,
    hot: (th.cpu !== null && th.cpu >= th.pause_cpu_c) || (th.gpu !== null && th.gpu >= th.pause_gpu_c),
    stale: age > 30,
  };
}

/** runner.status.json check_all (tools/dev/check_all.py between sessions) as one line and a level. */
export function checkAllLine(c: CheckAll, now: number): { text: string; level: "ok" | "warn" | "bad" } {
  const n = c.counts ?? {};
  const ago = c.t ? ` · ${Math.round((now - c.t) / 60)} min ago` : "";
  const what = c.ok ? `${n.PASS ?? 0} pass, ${n.WARN ?? 0} warn, ${n.FAIL ?? 0} fail` : c.why || "no verdict";
  const level = !c.ok ? "warn" : n.FAIL ? "bad" : n.WARN ? "warn" : "ok";
  return { text: `check_all ${what}${c.session ? ` after ${c.session}` : ""}${ago}`, level };
}

export function hours(v: Num): string {
  return v === null || v === undefined ? "–" : v < 1 ? `${Math.round(v * 60)} min` : `${v.toFixed(1)} h`;
}

export interface RunnerEvent {
  t: number;
  kind: string;
  fault?: string;
  detail?: string;
  seconds?: number;
  how?: string;
  error?: string;
  why?: string;
  attempt?: number;
  save?: string;
}

const BAD_EVENTS = ["fault", "recovery_failed", "steam_refused", "setup_failed", "launch_failed", "launch_gave_up"];

/** One unattended-run event (live.unattended_payload "events") as text and a level. */
export function eventLine(e: RunnerEvent): { text: string; level: "ok" | "bad" | "" } {
  const text =
    e.kind === "fault" ? `FAULT ${e.fault}: ${e.detail ?? ""}`
    : e.kind === "recovered" ? `recovered ${e.fault} in ${e.seconds ?? "?"} s: ${e.how ?? ""}`
    : e.kind === "recovery_failed" ? `recovery FAILED (${e.fault}): ${e.error ?? ""}`
    : e.kind === "steam_refused" ? `launch refused, Steam not offline: ${e.why ?? ""}`
    : e.kind === "setup_failed" ? `setup failed: ${e.error ?? ""}`
    : e.kind === "launch_failed" ? `launch ${e.attempt ?? "?"} died (${e.save ?? ""}): ${e.error ?? ""}`
    : e.kind === "launch_gave_up" ? `GAVE UP: crash loop: ${e.error ?? ""}`
    : `runner stopped: ${e.why ?? ""}`;
  return { text, level: e.kind === "recovered" ? "ok" : BAD_EVENTS.includes(e.kind) ? "bad" : "" };
}

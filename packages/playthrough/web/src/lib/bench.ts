// The benchmark grade card (screens/Bench.svelte) and the Replay ticks: pure helpers over a run's result.json.
import type { BenchManifest, BenchResult, CurvePoint, Methods } from "./types";

export type Level = "ok" | "bad" | "warn";

/** pass / fail (outcome) / invalid (reason): an invalid run is rerun and never scored. */
export function verdict(r: BenchResult): { text: string; level: Level } {
  if (!r.valid) return { text: `invalid${r.invalid_reason ? ` (${r.invalid_reason})` : ""}`, level: "warn" };
  if (r.passed) return { text: "pass", level: "ok" };
  return { text: `fail (${r.outcome.replace("_", " ")})`, level: "bad" };
}

/** Seconds as "17m 23s" / "45s"; "–" for null. */
export function dur(s: number | null | undefined): string {
  if (s === null || s === undefined) return "–";
  const r = Math.round(s);
  return r < 60 ? `${r}s` : `${Math.floor(r / 60)}m ${String(r % 60).padStart(2, "0")}s`;
}

/** A token count as 378 / 28.3 k / 14.7 M. */
export function tokens(n: number): string {
  return n < 1000 ? String(n) : n < 1e6 ? `${(n / 1000).toFixed(1)} k` : `${(n / 1e6).toFixed(1)} M`;
}

/** The model's own time as a share of the wall clock (D7 reports both); null when either is missing. */
export function thinkPct(r: BenchResult): number | null {
  return r.think_s === null || !r.wall_s ? null : Math.round((100 * r.think_s) / r.wall_s);
}

export interface Curve {
  line: string; // polyline points over the checkpoints that have a value
  dots: { x: number; y: number; at_s: number; value: number; peak: number | null }[];
  max: number;
  end_s: number;
  ticks: { x: number; at_s: number; reached: boolean }[]; // every checkpoint, for the time axis
}

/** The progress curve (the grader's checkpoints of one fact) in a w×h box, x from 0 to the last checkpoint, y up
 * from 0 to the highest value. Only the checkpoints with a value are drawn (none is assumed at time 0; a null value
 * is a checkpoint the run didn't reach). null when no checkpoint has a value. */
export function curve(points: CurvePoint[], w = 240, h = 60, pad = 4): Curve | null {
  const known = points.filter((p): p is CurvePoint & { value: number } => p.value !== null);
  if (!known.length) return null;
  const end_s = Math.max(...points.map((p) => p.at_s));
  const max = Math.max(1, ...known.map((p) => Math.max(p.value, p.peak ?? 0)));
  const x = (s: number) => round(pad + (s / (end_s || 1)) * (w - 2 * pad));
  const y = (v: number) => round(h - pad - (v / max) * (h - 2 * pad));
  const dots = known.map((p) => ({ x: x(p.at_s), y: y(p.value), at_s: p.at_s, value: p.value, peak: p.peak }));
  const ticks = points.map((p) => ({ x: x(p.at_s), at_s: p.at_s, reached: p.value !== null }));
  return { line: dots.map((d) => `${d.x},${d.y}`).join(" "), dots, max, end_s, ticks };
}

const round = (v: number) => Math.round(v * 10) / 10;

/** Gold by method, biggest first, each with its share of the gained gold (methods.py METHODS). */
export function goldRows(m: Methods): { method: string; gold: number; pct: number }[] {
  const rows = Object.entries(m.gold_by_method).map(([method, gold]) => ({ method, gold }));
  const total = rows.reduce((a, r) => a + r.gold, 0);
  return rows
    .sort((a, b) => b.gold - a.gold)
    .map((r) => ({ ...r, pct: total ? Math.round((100 * r.gold) / total) : 0 }));
}

/** The method counters that aren't zero, as "3 kills"-style chips. */
export function counters(m: Methods): string[] {
  const c: [number, string][] = [
    [m.spent, "gold spent"], [m.lost, "gold lost"], [m.crimes, "crimes"], [m.kills, "kills"],
    [m.traders_killed, "traders killed"], [m.pickpockets, "pickpockets"], [m.pickpockets_failed, "pickpockets failed"],
  ];
  return c.filter(([n]) => n).map(([n, what]) => `${n} ${what}`);
}

/** "z −1.25 over 40 rolls · 3 nat 20 · 1 nat 1" (bg3bench.luck: the party's view; + is lucky). */
export function luckLine(l: BenchResult["luck"]): string {
  if (!l) return "no roll log";
  const z = `${l.z < 0 ? "−" : "+"}${Math.abs(l.z).toFixed(2)}`;
  return `z ${z} over ${l.n} roll${l.n === 1 ? "" : "s"} · ${l.nat20} nat 20 · ${l.nat1} nat 1`;
}

export interface Mark {
  pct: number;
  t: number;
  kind: "pass" | "gold" | "choice";
  label: string;
}

/** Replay scrubber marks inside [t0, t1]: the goal (run start + t_pass_s; t_pass is on the scenario's clock, game
 * time by default, so the mark is approximate), each gold change and each timed choice. Sorted by time. */
export function marks(man: BenchManifest, r: BenchResult, t0: number, t1: number): Mark[] {
  const span = Math.max(1, t1 - t0);
  const out: Omit<Mark, "pct">[] = [];
  if (r.t_pass_s !== null) out.push({ t: man.created + r.t_pass_s, kind: "pass", label: `goal reached at ${dur(r.t_pass_s)}` });
  for (const c of r.methods?.changes ?? [])
    out.push({ t: c.t, kind: "gold", label: `gold ${c.delta > 0 ? "+" : ""}${c.delta} (${c.method})` });
  for (const p of r.choices?.picks ?? []) if (p.t !== null) out.push({ t: p.t, kind: "choice", label: `${p.scene}: ${p.pick}` });
  return out
    .filter((m) => m.t >= t0 && m.t <= t1)
    .sort((a, b) => a.t - b.t)
    .map((m) => ({ ...m, pct: round(((m.t - t0) / span) * 100) }));
}

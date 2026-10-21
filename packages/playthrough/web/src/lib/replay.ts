// The Replay screen: one clock (epoch s) drives the event list and the recorded video. Pure functions only.
import type { ReplayEvent, Segment } from "./types";

/** Index of the last event at or before t (-1 before the first). Events are sorted by t. */
export function indexAt(events: { t: number }[], t: number): number {
  let lo = 0, hi = events.length - 1, ans = -1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (events[mid]!.t <= t) {
      ans = mid;
      lo = mid + 1;
    } else hi = mid - 1;
  }
  return ans;
}

/** The segment showing time t and the offset into it (recorder.py locate), or null in a gap. */
export function locate(segments: Segment[], t: number): { seg: Segment; offset: number } | null {
  for (const s of segments) {
    const end = s.end ?? Infinity;
    if (s.start <= t && t < end) return { seg: s, offset: Math.round((t - s.start) * 1000) / 1000 };
  }
  return null;
}

/** The time of the next (dir 1) or previous (dir -1) event from t; t itself when there is none. */
export function step(events: { t: number }[], t: number, dir: 1 | -1): number {
  const i = indexAt(events, t);
  if (dir === 1) {
    for (let j = Math.max(0, i); j < events.length; j++) if (events[j]!.t > t) return events[j]!.t;
    return t;
  }
  for (let j = i; j >= 0; j--) if (events[j]!.t < t) return events[j]!.t;
  return t;
}

/** The last n events up to t, newest first (the right column). */
export function recent(events: ReplayEvent[], t: number, n = 12): ReplayEvent[] {
  const i = indexAt(events, t);
  return events.slice(Math.max(0, i - n + 1), i + 1).reverse();
}

/** Scrubber ticks for the agent's words and memory edits, as 0-100 % of the session. */
export function ticks(events: ReplayEvent[], t0: number, t1: number): { pct: number; kind: string }[] {
  const span = Math.max(1, t1 - t0);
  return events
    .filter((e) => e.kind !== "action")
    .map((e) => ({ pct: Math.min(100, Math.max(0, ((e.t - t0) / span) * 100)), kind: e.kind }));
}

/** Coverage of the session by video, as [from %, to %] runs. */
export function coverage(segments: Segment[], t0: number, t1: number): [number, number][] {
  const span = Math.max(1, t1 - t0);
  const pct = (t: number) => Math.min(100, Math.max(0, ((t - t0) / span) * 100));
  return segments.map((s) => [pct(s.start), pct(s.end ?? t1)] as [number, number]).filter(([a, b]) => b > a);
}

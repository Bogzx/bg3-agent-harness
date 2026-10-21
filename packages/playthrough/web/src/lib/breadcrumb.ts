// The breadcrumb plot: /positions plus live "position" rows, projected into an SVG box. Pure functions only.
import type { PositionRow, Positions } from "./types";

export const GAP_S = 120; // positions.py GAP_S: a longer gap starts a new segment
export const MAX_LIVE_POINTS = 4000;

export const emptyPositions = (): Positions => ({ level: null, levels: [], segments: [], party: [], t: null, bounds: null });

function lead(row: PositionRow) {
  return row.party.find((m) => m.active) ?? row.party[0];
}

function grow(b: Positions["bounds"], x: number, z: number): NonNullable<Positions["bounds"]> {
  if (!b) return { min_x: x, max_x: x, min_z: z, max_z: z };
  return { min_x: Math.min(b.min_x, x), max_x: Math.max(b.max_x, x), min_z: Math.min(b.min_z, z), max_z: Math.max(b.max_z, z) };
}

/** A live row: the plot follows the party to a new level (fresh segments), else extends or splits the track. */
export function applyPosition(p: Positions, row: PositionRow): Positions {
  const m = lead(row);
  if (!m || (p.t !== null && row.t <= p.t)) return p;
  const party = row.party.map((x) => ({ name: x.name, x: x.x, z: x.z, active: Boolean(x.active) }));
  const levels = p.levels.some((l) => l.level === row.level)
    ? p.levels.map((l) => (l.level === row.level ? { ...l, samples: l.samples + 1, last_t: row.t } : l))
    : [...p.levels, { level: row.level, region: row.region, samples: 1, first_t: row.t, last_t: row.t }];
  const pt: [number, number, number] = [row.t, m.x, m.z];
  if (row.level !== p.level) {
    return { ...p, level: row.level, levels, segments: [[pt]], party, t: row.t, bounds: grow(null, m.x, m.z) };
  }
  const segs = p.segments.slice();
  const last = segs[segs.length - 1];
  if (last && p.t !== null && row.t - p.t <= GAP_S) segs[segs.length - 1] = [...last, pt];
  else segs.push([pt]);
  let total = segs.reduce((n, s) => n + s.length, 0);
  while (total > MAX_LIVE_POINTS && segs.length) {
    const first = segs[0]!;
    if (first.length > 1) segs[0] = first.slice(1);
    else segs.shift();
    total--;
  }
  return { ...p, levels, segments: segs, party, t: row.t, bounds: grow(p.bounds, m.x, m.z) };
}

export interface Plot {
  lines: string[]; // SVG polyline points, one per segment
  dots: { name: string; cx: number; cy: number; active: boolean }[];
  start: { cx: number; cy: number } | null;
  scale_m: number; // metres per 50 px (the scale bar)
}

/** North-up projection into w x h with `pad`: x to the right, z upwards (minimap X = world x, Y = world z), one
 * scale for both axes, centred. */
export function project(p: Positions, w: number, h: number, pad = 12): Plot {
  const b = p.bounds;
  if (!b) return { lines: [], dots: [], start: null, scale_m: 0 };
  const xs = p.party.map((m) => m.x);
  const zs = p.party.map((m) => m.z);
  const minX = Math.min(b.min_x, ...xs), maxX = Math.max(b.max_x, ...xs);
  const minZ = Math.min(b.min_z, ...zs), maxZ = Math.max(b.max_z, ...zs);
  const spanX = Math.max(maxX - minX, 10), spanZ = Math.max(maxZ - minZ, 10);
  const k = Math.min((w - 2 * pad) / spanX, (h - 2 * pad) / spanZ);
  const ox = pad + (w - 2 * pad - (maxX - minX) * k) / 2;
  const oy = pad + (h - 2 * pad - (maxZ - minZ) * k) / 2;
  const px = (x: number) => Math.round((ox + (x - minX) * k) * 10) / 10;
  const py = (z: number) => Math.round((oy + (maxZ - z) * k) * 10) / 10;
  const first = p.segments[0]?.[0];
  return {
    lines: p.segments.filter((s) => s.length > 1).map((s) => s.map(([, x, z]) => `${px(x)},${py(z)}`).join(" ")),
    dots: p.party.map((m) => ({ name: m.name, cx: px(m.x), cy: py(m.z), active: m.active })),
    start: first ? { cx: px(first[1]), cy: py(first[2]) } : null,
    scale_m: Math.round(50 / k),
  };
}

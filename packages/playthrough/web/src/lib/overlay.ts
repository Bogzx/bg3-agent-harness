// /overlay?pt=<name>&panels=goal,say,party,tool,clock&theme=dark: what the stream overlay shows. Pure.
export const PANELS = ["goal", "say", "party", "tool", "clock"] as const;
export type Panel = (typeof PANELS)[number];

export interface OverlayOptions {
  pt: string | null; // null = the runner's current playthrough
  panels: Set<Panel>;
  theme: "dark" | "light";
}

export function parseOverlay(search: string): OverlayOptions {
  const q = new URLSearchParams(search);
  const asked = (q.get("panels") ?? "").split(",").map((s) => s.trim()).filter(Boolean);
  const panels = new Set<Panel>(asked.length ? (asked.filter((p) => (PANELS as readonly string[]).includes(p)) as Panel[]) : PANELS);
  return { pt: q.get("pt") || null, panels, theme: q.get("theme") === "light" ? "light" : "dark" };
}

/** A line cut to fit the bar: whole words, an ellipsis when cut. */
export function clip(text: string, max: number): string {
  const one = text.replace(/\s+/g, " ").trim();
  if (one.length <= max) return one;
  const cut = one.slice(0, max - 1);
  const sp = cut.lastIndexOf(" ");
  return `${sp > max * 0.6 ? cut.slice(0, sp) : cut}…`;
}

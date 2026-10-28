// Small display formatters shared by the screens.

export function usd(v: number | null | undefined): string {
  return v === null || v === undefined ? "–" : `$${v.toFixed(2)}`;
}

export function num(v: number | null | undefined, digits = 0, unit = ""): string {
  return v === null || v === undefined ? "–" : `${v.toFixed(digits)}${unit}`;
}

export function clock(epoch: number | null | undefined): string {
  if (!epoch) return "–";
  const d = new Date(epoch * 1000);
  return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });
}

export function date(epoch: number | null | undefined): string {
  if (!epoch) return "";
  return new Date(epoch * 1000).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit",
                                                    hour12: false });
}

export function mmss(seconds: number | null): string {
  if (seconds === null) return "–";
  const s = Math.abs(seconds);
  const m = Math.floor(s / 60);
  return `${seconds < 0 ? "-" : ""}${m}:${String(s % 60).padStart(2, "0")}`;
}

export function hpPct(hp: number, max: number): number {
  return max > 0 ? Math.max(0, Math.min(100, Math.round((100 * hp) / max))) : 0;
}

/** One line of a tool call's arguments, for the call list. */
export function argLine(args: Record<string, unknown>, max = 80): string {
  const parts = Object.entries(args).map(([k, v]) => `${k}=${typeof v === "string" ? v : JSON.stringify(v)}`);
  const s = parts.join(" ");
  return s.length > max ? `${s.slice(0, max - 1)}…` : s;
}

// Hash routes: #/watch/<pt>, #/stats/<pt>, #/memory/<pt>[/<file path>], #/timeline/<pt>, #/story/<pt>,
// #/replay/<pt>[/<session>], #/launch[/<pt>]. No server fallback needed. /overlay is its own page (main.ts).

export type Screen = "watch" | "stats" | "memory" | "timeline" | "story" | "replay" | "launch";
export const SCREENS: Screen[] = ["watch", "stats", "memory", "timeline", "story", "replay", "launch"];

export interface Route {
  screen: Screen;
  pt: string | null;
  file: string | null;
}

export function parseRoute(hash: string): Route {
  const parts = hash.replace(/^#\/?/, "").split("/");
  const screen = (SCREENS as string[]).includes(parts[0] ?? "") ? (parts[0] as Screen) : "watch";
  const pt = parts[1] ? decodeURIComponent(parts[1]) : null;
  const file = parts.length > 2 ? parts.slice(2).map(decodeURIComponent).join("/") : null;
  return { screen, pt, file: file || null };
}

export function href(r: Route): string {
  const base = `#/${r.screen}${r.pt ? `/${encodeURIComponent(r.pt)}` : ""}`;
  return r.file && r.pt ? `${base}/${r.file.split("/").map(encodeURIComponent).join("/")}` : base;
}

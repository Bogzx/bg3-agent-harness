// Hash routes: #/preflight, #/doctor, #/saves, #/new, #/queue, #/results, #/videos, #/settings, #/about. No server fallback needed.

export type Screen = "preflight" | "doctor" | "saves" | "new" | "queue" | "results" | "videos" | "settings" | "about";
export const SCREENS: Screen[] = ["preflight", "doctor", "saves", "new", "queue", "results", "videos", "settings", "about"];
export const LABELS: Record<Screen, string> = {
  preflight: "Preflight", doctor: "Doctor", saves: "Saves", new: "New run", queue: "Queue", results: "Results", videos: "Videos", settings: "Settings",
  about: "About",
};

export function parseRoute(hash: string): Screen {
  const first = hash.replace(/^#\/?/, "").split("/")[0] ?? "";
  return (SCREENS as string[]).includes(first) ? (first as Screen) : "preflight";
}

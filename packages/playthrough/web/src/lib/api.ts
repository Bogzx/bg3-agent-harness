import type {
  Commit, MemoryFile, PlaythroughConfig, PlaythroughRow, Positions, RecorderState, ReplayPayload, Runner, Segment, SupervisorView,
  TimelinePayload, WatchSnapshot,
} from "./types";

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

async function getJson<T>(path: string, init?: RequestInit): Promise<T> {
  const r = await fetch(path, { ...init, headers: { Accept: "application/json", ...(init?.headers ?? {}) } });
  if (!r.ok) {
    let msg = `${r.status} ${r.statusText}`;
    try {
      const body = (await r.json()) as { error?: string };
      if (body.error) msg = body.error;
    } catch {
      // not JSON: keep the status line
    }
    throw new ApiError(r.status, msg);
  }
  return (await r.json()) as T;
}

const pt = (name: string) => `/api/v1/playthroughs/${encodeURIComponent(name)}`;

export const api = {
  status: () => getJson<{ v: number; runner: Runner }>("/api/v1/status"),
  playthroughs: () => getJson<{ v: number; playthroughs: PlaythroughRow[] }>("/api/v1/playthroughs"),
  watch: (name: string) => getJson<WatchSnapshot>(`${pt(name)}/watch`),
  memoryTree: (name: string) => getJson<{ v: number; files: MemoryFile[] }>(`${pt(name)}/memory/tree`),
  memoryFile: (name: string, path: string) =>
    getJson<{ v: number; path: string; text: string; history: Commit[] }>(
      `${pt(name)}/memory/file?path=${encodeURIComponent(path)}`,
    ),
  memoryLog: (name: string) => getJson<{ v: number; commits: Commit[] }>(`${pt(name)}/memory/log`),
  sessions: (name: string) =>
    getJson<{ v: number; sessions: { session: string; started: number | null; minutes: number | null }[] }>(
      `${pt(name)}/sessions`,
    ),
  replay: (name: string, session: string) => getJson<ReplayPayload>(`${pt(name)}/replay/${encodeURIComponent(session)}`),
  timeline: (name: string) => getJson<TimelinePayload>(`${pt(name)}/timeline`),
  positions: (name: string, level?: string) =>
    getJson<Positions>(`${pt(name)}/positions${level ? `?level=${encodeURIComponent(level)}` : ""}`),
  recorder: () => getJson<{ v: number; recorder: RecorderState | null }>("/api/v1/recorder"),
  videoIndex: () => getJson<{ v: number; segments: Segment[] }>("/api/v1/video/index"),
  supervisor: () => getJson<{ v: number; supervisor: SupervisorView | null; profiles: string[] }>("/api/v1/supervisor"),
  config: (name: string) =>
    getJson<{ v: number; playthrough: string; config: PlaythroughConfig; sessions: number }>(`${pt(name)}/config`),
  control: (name: string, what: "start" | "resume" | "stop", body: object = {}) =>
    getJson<{ v: number; supervisor: SupervisorView }>(`${pt(name)}/${what}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }),
};

export const STREAM_TYPES = ["status", "session", "log", "action", "party", "memory", "position", "supervisor", "actions_reset", "hub_error"];

/** The SSE stream from `lastId` on. EventSource resends the last id it saw on reconnect by itself; a "reset" means
 * the server lost our place (it restarted): the caller reloads its snapshot. Returns a close function. */
export function openStream(
  lastId: string | null,
  onEvent: (type: string, data: unknown) => void,
  onReset: () => void,
  onState: (open: boolean) => void,
): () => void {
  const es = new EventSource(`/api/v1/stream${lastId ? `?last_id=${encodeURIComponent(lastId)}` : ""}`);
  es.onopen = () => onState(true);
  es.onerror = () => onState(false);
  es.addEventListener("reset", () => onReset());
  for (const type of STREAM_TYPES) {
    es.addEventListener(type, (e) => onEvent(type, JSON.parse((e as MessageEvent<string>).data)));
  }
  return () => es.close();
}

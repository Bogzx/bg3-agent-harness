import type {
  About, DoctorReport, MatrixProgress, Plan, PlanForm, Prefs, Preflight, Prepared, ProfileRow, RenderState, RunRow,
  RunState, RunView, ScenarioRow, SettingsView, StartForm, Submission,
} from "./types";
import { HEADER } from "./token";

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

let token: string | null = null;

export function setToken(t: string | null): void {
  token = t;
}

async function call<T>(path: string, init?: RequestInit): Promise<T> {
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

function post<T>(path: string, body: object): Promise<T> {
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  if (token) headers[HEADER] = token;
  return call<T>(path, { method: "POST", headers, body: JSON.stringify(body) });
}

export const api = {
  about: () => call<About>("/api/v1/about"),
  preflight: () => call<Preflight>("/api/v1/preflight"),
  scenarios: () => call<{ scenarios: ScenarioRow[] }>("/api/v1/scenarios"),
  profiles: () => call<{ profiles: ProfileRow[] }>("/api/v1/profiles"),
  queue: () => call<{ matrices: MatrixProgress[] }>("/api/v1/queue"),
  results: () => call<{ runs: RunRow[] }>("/api/v1/results"),
  settings: () => call<SettingsView>("/api/v1/settings"),
  plan: (form: PlanForm) => post<Plan>("/api/v1/plan", form),
  setKey: (name: string, value: string) => post<{ name: string; masked: string | null }>("/api/v1/settings/key", { name, value }),
  setSiteToken: (value: string) => post<{ site_token: string | null }>("/api/v1/settings/site-token", { value }),
  setPrefs: (prefs: Partial<Prefs>) => post<{ prefs: Prefs }>("/api/v1/settings/prefs", prefs),
  run: () => call<RunView>("/api/v1/run"),
  start: (form: StartForm) => post<{ started: RunState | null; matrix_file: string }>("/api/v1/run/start", form),
  stop: () => post<RunView>("/api/v1/run/stop", {}),
  doctor: () => call<{ report: DoctorReport | null; error: string | null }>("/api/v1/doctor"),
  submissions: () =>
    call<{ submissions: Record<string, { prepared: Prepared | null; submission: Submission | null }>; site: string | null }>(
      "/api/v1/submissions",
    ),
  prepare: (run: string) => post<Prepared>("/api/v1/submit/prepare", { run }),
  addVideo: (run: string, bundle_sha256: string, youtube_url: string) =>
    post<Prepared>("/api/v1/submit/video", { run, bundle_sha256, youtube_url }),
  upload: (run: string, bundle_sha256: string) => post<Submission>("/api/v1/submit/upload", { run, bundle_sha256 }),
  submissionStatus: (run: string) => post<Submission>("/api/v1/submit/status", { run }),
  render: (run: string) => post<{ render: RenderState | null }>("/api/v1/render", { run }),
  renderState: () => call<{ render: RenderState | null }>("/api/v1/render"),
};

import type { About, MatrixProgress, Plan, PlanForm, Prefs, Preflight, ProfileRow, RunRow, ScenarioRow, SettingsView } from "./types";
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
};

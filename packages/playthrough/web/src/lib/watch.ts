// The Watch screen's state: a snapshot from /watch, then stream events applied in order. Pure functions only.
import type { Action, Commit, Cost, Latency, LogEntry, Party, Runner, WatchSnapshot } from "./types";

export const LIMITS = { log: 400, actions: 200, memory: 20 };

export interface WatchState {
  pt: string;
  live: boolean; // this playthrough is the runner's current one: the shared actions feed is its
  runner: Runner | null;
  session: string | null;
  log: LogEntry[];
  actions: Action[];
  party: Party | null;
  goal: string | null;
  cost: Cost;
  latency: Latency | null;
  screenshotRev: number; // 0: no agent screenshot
  memory: Commit[];
}

export function fromSnapshot(s: WatchSnapshot): WatchState {
  return {
    pt: s.playthrough,
    live: s.runner !== null,
    runner: s.runner,
    session: s.session,
    log: s.log.slice(-LIMITS.log),
    actions: s.actions.slice(-LIMITS.actions),
    party: s.party,
    goal: s.goal,
    cost: s.cost,
    latency: s.latency,
    screenshotRev: s.screenshot ? s.screenshot.t : 0,
    memory: [],
  };
}

const logKey = (e: LogEntry) => `${e.session ?? ""}|${e.clock}|${e.kind}|${e.text}`;
const actionKey = (a: Action) => `${a.t}|${a.tool}`;

/** Append items not already among the last few (the snapshot and the stream can overlap by a line or two). */
function appendNew<T>(list: T[], item: T, key: (x: T) => string, cap: number, window = 50): T[] {
  const k = key(item);
  for (let i = Math.max(0, list.length - window); i < list.length; i++) {
    if (key(list[i] as T) === k) return list;
  }
  const out = [...list, item];
  return out.length > cap ? out.slice(out.length - cap) : out;
}

const END_COST_RE = /cost=\$([\d.]+)/;

function withEndCost(cost: Cost, text: string): Cost {
  const m = END_COST_RE.exec(text);
  if (!m) return cost;
  const usd = Number(m[1]);
  return {
    total_usd: Math.round(((cost.total_usd ?? 0) + usd) * 100) / 100,
    last_session_usd: usd,
    sessions: cost.sessions + 1,
  };
}

export function applyEvent(s: WatchState, type: string, data: unknown): WatchState {
  switch (type) {
    case "status": {
      const r = data as Runner;
      if (r.playthrough !== s.pt) return s.live ? { ...s, live: false, runner: null } : s;
      return { ...s, live: true, runner: r };
    }
    case "session": {
      const d = data as { playthrough: string | null; session: string | null };
      if (d.playthrough !== s.pt || d.session === s.session) return s;
      const mark: LogEntry = { clock: null, kind: "header", text: `session ${d.session ?? "?"}`, session: d.session };
      // the tool calls shown are the session's own: a new session starts with none
      return { ...s, session: d.session, log: appendNew(s.log, mark, logKey, LIMITS.log), actions: [] };
    }
    case "log": {
      const e = data as LogEntry;
      if (e.playthrough && e.playthrough !== s.pt) return s;
      const log = appendNew(s.log, e, logKey, LIMITS.log);
      if (log === s.log) return s;
      return { ...s, log, cost: e.kind === "end" ? withEndCost(s.cost, e.text) : s.cost };
    }
    case "action": {
      if (!s.live) return s;
      const a = data as Action;
      const actions = appendNew(s.actions, a, actionKey, LIMITS.actions);
      const shot = a.kind === "vision" ? s.screenshotRev + 1 : s.screenshotRev;
      return actions === s.actions ? s : { ...s, actions, screenshotRev: shot };
    }
    case "actions_reset":
      return s.live ? { ...s, actions: [], party: null } : s; // a new session: the old party is the hub's no more
    case "party":
      return s.live ? { ...s, party: data as Party } : s;
    case "memory": {
      const c = data as Commit;
      if (c.playthrough !== s.pt) return s;
      return { ...s, memory: [c, ...s.memory].slice(0, LIMITS.memory) };
    }
    default:
      return s;
  }
}

/** The GAME panel's picture: the recorder's live view while it records, else the latest agent screenshot. */
export function screenSrc(recording: boolean, s: WatchState): string | null {
  if (recording) return "/api/v1/screen.mjpg";
  return s.screenshotRev ? `/api/v1/screenshot?rev=${s.screenshotRev}` : null;
}

/** The agent's own words, newest first. */
export function says(s: WatchState, n = 6): LogEntry[] {
  return s.log.filter((e) => e.kind === "say").slice(-n).reverse();
}

/** Seconds left in the session from the runner's deadline, or null. */
export function secondsLeft(r: Runner | null, now: number): number | null {
  if (!r || !r.deadline || r.session_ended) return null;
  return Math.round(r.deadline - now);
}

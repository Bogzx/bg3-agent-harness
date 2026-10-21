// The v1 API payloads (packages/playthrough/src/bg3play/api.py). Every payload has v: 1.

export interface Runner {
  playthrough: string | null;
  session?: string | null;
  started?: number | null;
  deadline?: number | null;
  hard_stop?: number | null;
  session_ended?: number | null;
  model?: string | null;
  effort?: string | null;
  session_minutes?: number | null;
  status_line?: string | null;
}

export interface PlaythroughRow {
  name: string;
  current: boolean;
  sessions: number;
  last_session: string | null;
  cost_usd: number | null;
  minutes: number;
}

export interface LogEntry {
  clock: string | null;
  kind: string;
  text: string;
  session?: string | null;
  playthrough?: string | null;
}

export interface Action {
  t: number | null;
  tool: string;
  args: Record<string, unknown>;
  ms: number | null;
  result: string;
  ok: boolean;
  kind: "read" | "act" | "vision" | "save";
}

export interface Member {
  name: string;
  classes: string;
  hp: number;
  max_hp: number;
  temp_hp: number;
  active: boolean;
  statuses: string[];
  downed: boolean;
  dead: boolean;
  in_combat: boolean;
}

export interface Party {
  where: string | null;
  mode: string | null;
  gold: number | null;
  party: Member[];
  t: number | null;
  source: string;
}

export interface Latency {
  apm: number | null;
  busy_pct: number | null;
  think_overlap_pct: number | null;
  idle_median_s: number | null;
  long_gaps: number | null;
  bridge_rt_ms: number | null;
}

export interface Cost {
  total_usd: number | null;
  last_session_usd: number | null;
  sessions: number;
}

export interface WatchSnapshot {
  v: number;
  playthrough: string;
  runner: Runner | null;
  session: string | null;
  log: LogEntry[];
  actions: Action[];
  party: Party | null;
  goal: string | null;
  cost: Cost;
  latency: Latency | null;
  screenshot: { t: number; name: string } | null;
  stream_id: string;
}

export interface Commit {
  sha: string;
  t: number;
  subject: string;
  session: string | null;
  playthrough?: string;
}

export interface MemoryFile {
  path: string;
  size: number;
  mtime: number;
}

export interface TaggedText {
  text: string;
  tag: string | null;
}

export interface Decision {
  id: string;
  title: string;
  session: string | null;
  session_id: string | null;
  t: number | null;
  choice: string[];
  why: string[];
  consequences: TaggedText[];
  expected: TaggedText[];
  reversible: string | null;
  notes: string[];
}

export interface Fight {
  session: string;
  t: string | null;
  region: string | null;
  grade: string | null;
  rounds: number | null;
  duration_s: number | null;
  reloaded: boolean;
  enemies: string[];
  downed: number;
  died: string[];
}

export interface TimelinePayload {
  v: number;
  playthrough: string;
  sessions: { session: string; started: number | null; minutes: number | null; outcome: string | null }[];
  decisions: Decision[];
  fights: Fight[];
}

export interface StreamEvent {
  type: string;
  data: unknown;
}

export interface PositionRow {
  t: number;
  level: string;
  region: string;
  party: { name: string; x: number; y: number; z: number; active?: boolean }[];
  playthrough?: string | null;
}

export interface LevelSeen {
  level: string;
  region: string | null;
  samples: number;
  first_t: number;
  last_t: number;
}

/** /positions: the lead's [t, x, z] segments for one level (map X = world x, map Y = world z). */
export interface Positions {
  v?: number;
  playthrough?: string;
  level: string | null;
  levels: LevelSeen[];
  segments: [number, number, number][][];
  party: { name: string; x: number; z: number; active: boolean }[];
  t: number | null;
  bounds: { min_x: number; max_x: number; min_z: number; max_z: number } | null;
}

export interface RecorderState {
  recording: boolean;
  enabled: boolean;
  reason: string;
  segments: number;
  bytes: number;
}

export interface Segment {
  file: string;
  run: string;
  start: number;
  end: number | null;
  bytes: number;
}

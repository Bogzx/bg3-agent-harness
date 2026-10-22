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

export interface SupervisorView {
  playthrough: string | null;
  state: "idle" | "running" | "stopping";
  detail: string;
  session: string | null;
}

/** playthrough.toml as JSON (config.py to_dict). */
export interface PlaythroughConfig {
  name: string;
  difficulty: string;
  agent: { profile: string; client: string; model: string; effort: string; condition: string };
  character: { brief: string; roleplay: string; name: string; constraints: string };
  session: { minutes: number; hours: number; max_cost_usd: number };
  keeper: { enabled: boolean; model: string; effort: string; minutes: number };
  recording: { enabled: boolean; fps: number; height: number };
  game?: { autoskip: boolean };
}

export type ReplayEvent =
  | { t: number; kind: "action"; tool: string; args: Record<string, unknown>; ms: number | null; ok: boolean; result: string }
  | { t: number; kind: "say"; say: string; text: string }
  | { t: number; kind: "memory"; subject: string; sha: string };

export interface ReplayPayload {
  v: number;
  playthrough: string;
  session: string;
  started: number;
  ended: number;
  events: ReplayEvent[];
  segments: Segment[];
}

// The live views the old dashboard showed (packages/playthrough/src/bg3play/dashboard.py, live.py, story.py).

export interface Thermal {
  t: number;
  line: string;
  cpu: number | null;
  gpu: number | null;
  pause_cpu_c: number;
  pause_gpu_c: number;
}

export interface SessionClock {
  deadline: number | null;
  left_s: number | null;
  source: string | null;
  phase: "idle" | "play" | "wrapup" | "over";
  started?: number | null;
  hard_stop?: number | null;
}

export interface GameMember {
  name: string;
  classes: string;
  hp: number | null;
  max_hp: number | null;
  temp_hp: number | null;
  statuses: string[];
  downed: boolean;
  dead: boolean;
  in_combat: boolean;
  active: boolean;
}

export interface GameView {
  reachable: boolean;
  error: string | null;
  t?: number;
  note?: string | null;
  status?: {
    region: string | null;
    subregion: string | null;
    level: string | null;
    gold: number | null;
    mode: string;
    turn_based: boolean;
    combat: Record<string, unknown> | null;
    party: GameMember[];
  } | null;
}

export interface CheckAll {
  ok: boolean;
  t?: number;
  session?: string;
  why?: string;
  counts?: { PASS?: number; WARN?: number; FAIL?: number };
  fails?: string[];
}

export interface LivePayload {
  v: number;
  now: number;
  playthrough: string | null;
  runner: { session?: string; check_all?: CheckAll } & Record<string, unknown>;
  clock: SessionClock;
  game: GameView;
  events: { t: number | null; kind: string | null; text: string }[];
  screen: { enabled: boolean; error: string | null; t: number | null; mime: string | null; note?: string | null };
  thermal: Thermal | null;
}

export type Num = number | null | undefined;

export interface LatencyLive {
  busy_now?: boolean;
  current_idle_s?: Num;
  in_dialogue?: boolean;
  in_combat?: boolean;
  idle_explore_median_s?: Num;
  idle_median_s?: Num;
  idle_p90_s?: Num;
  long_gaps?: { dialogue?: number } | null;
  think_median_s?: Num;
  api_source?: string;
  api_s_per_turn?: Num;
  ms_per_out_tok?: Num;
  rt_read_median_ms?: Num;
  apm?: Num;
  calls_pm?: Num;
  reads_pm?: Num;
  actions_pm?: Num;
  busy_pct?: Num;
  pipelining_pct?: Num;
  think_overlap_pct?: Num;
  targets?: Record<string, { value?: Num; pass?: boolean | null }>;
  timeline?: { buckets?: string[] } | null;
}

export interface LatencyRow {
  session: string;
  minutes?: Num;
  idle_median_s?: Num;
  idle_p90_s?: Num;
  idle_explore_median_s?: Num;
  long_gaps?: Num;
  long_gaps_dialogue?: Num;
  think_median_s?: Num;
  api_s_per_turn?: Num;
  ms_per_out_tok?: Num;
  rt_read_median_ms?: Num;
  apm?: Num;
  busy_pct?: Num;
  pipelining_pct?: Num;
  think_overlap_pct?: Num;
  pass?: Record<string, boolean | null>;
}

export interface LatencyPayload {
  v: number;
  live: LatencyLive | null;
  history: LatencyRow[];
  targets: Record<string, number>;
  files: Record<string, string | null>;
}

export interface Milestones {
  act: number | string;
  reached: number;
  total: number;
  next: string | null;
  items: { label: string; reached: boolean; t?: number | null; session?: string | null }[];
}

export interface ProgressPayload {
  v: number;
  live: {
    session?: string | null;
    running?: boolean;
    points: number;
    per_hour: Num;
    events: number;
    stuck: boolean;
    stuck_min?: Num;
    since_progress_min?: Num;
    minutes?: Num;
    recent?: { t: number; points: number; kind: string; what: string }[];
  } | null;
  history: {
    session: string;
    minutes?: Num;
    points?: Num;
    per_hour?: Num;
    events?: Num;
    longest_no_progress_min?: Num;
    stuck_episodes?: Num;
    stuck_min?: Num;
  }[];
  stuck_after_min: number;
  milestones: Milestones | null;
}

export type GoalOp = "<" | "==" | ">=";

export interface Goal {
  key: string;
  label: string;
  unit: string;
  op: GoalOp;
  target: number;
  digits: number;
  series: Num[];
  best: Num;
  pass_rate: Num;
}

export interface GoalSession {
  session: string;
  live?: boolean;
  short?: boolean;
  minutes?: Num;
  points?: Num;
  values: Record<string, Num>;
  pass: Record<string, boolean | null>;
  judged?: number;
  passed?: number;
}

export interface GoalsPayload {
  v: number;
  goals: Goal[];
  sessions: GoalSession[];
  milestones: Milestones | null;
  unattended: Unattended | null;
}

export interface Unattended {
  live: boolean;
  current_h: Num;
  longest_h: Num;
  total_h: Num;
  runs: number;
  last_stop: string | null;
  faults: Record<string, number>;
  fault_total: number;
  recovered: number;
  failed: number;
  steam_refused: number;
  launch_failed: number;
  alert: { t: number; msg?: string; kind?: string } | null;
  steam_offline: boolean | null;
  recovery_s_median: Num;
  recovery_s_max: Num;
  events: import("./stats").RunnerEvent[];
}

export interface StoryItem {
  name: string;
  slot?: string;
  rarity?: string | null;
  story?: boolean;
}

export interface StoryMember {
  name: string;
  class?: string;
  hp?: number;
  max_hp?: number;
  active?: boolean;
  statuses?: string[];
  equipped?: StoryItem[];
  notable?: StoryItem[];
  camp?: boolean;
}

export interface StoryPayload {
  v: number;
  playthrough: string;
  exists: boolean;
  dir: string;
  decisions: Decision[];
  chapters: { file: string; title: string; sections: { heading: string | null; items: string[] }[] }[];
  overview: { title: string | null; header: string[]; now: string | null; main_thread: string | null; party: string[];
              commitments: string[] };
  status: { where: string | null; mode: string | null; gold: number | null; when: string | null; party: StoryMember[] };
  others: StoryMember[];
}

export interface DiffLine {
  kind: "add" | "del" | "hunk" | "ctx" | "meta";
  text: string;
}

/** profiles/*.toml as the server parsed it (bg3play/client_profiles.py over runner/profiles.py). */
export interface ClientProfile {
  name: string;
  client: string | null;
  model: string | null;
  effort: string;
  provider: string | null;
  api_key_env: string | null;
  error: string | null;
}

// A benchmark run's grade (bench.py): manifest.json and result.json as bg3bench wrote them
// (packages/bench/schema/{manifest,result}.v1.schema.json), only the fields the screens read.

export interface CurvePoint {
  at_s: number;
  value: number | null;
  peak: number | null;
}

export interface GoldChange {
  t: number;
  delta: number;
  method: string;
}

export interface Methods {
  gold_by_method: Record<string, number>;
  spent: number;
  lost: number;
  crimes: number;
  kills: number;
  traders_killed: number;
  pickpockets: number;
  pickpockets_failed: number;
  changes: GoldChange[];
}

export interface ChoicePick {
  t: number | null;
  scene: string;
  options: string[];
  picked: number | null;
  pick: string;
  reason: string | null;
}

export interface BenchResult {
  run_id: string;
  outcome: "reached" | "timeout" | "over_budget" | "gave_up" | "runner_error";
  valid: boolean;
  invalid_reason: string | null;
  passed: boolean;
  t_pass_s: number | null;
  grade: { violations: { t_s: number; node?: unknown }[]; curve: { fact: string; points: CurvePoint[] }; progress: number | null } | null;
  methods: Methods | null;
  cost_usd: number | null;
  tokens: { input: number; output: number; cache_read: number; cache_write: number };
  wall_s: number | null;
  play_s: number | null;
  think_s: number | null;
  luck: { n: number; z: number; party: number; other: number; skipped: number; nat20: number; nat1: number } | null;
  reloads: number;
  deaths: { fights: number; downs: number; deaths: number; wipes: number };
  fairness: { leak: number; check: number; blocks: number };
  choices?: {
    picks: ChoicePick[];
    fit: { status: "ok" | "judge_failed" | "no_decisions"; score: number | null; judge_model: string | null; summary?: string } | null;
  };
}

export interface BenchManifest {
  run_id: string;
  created: number;
  series: string;
  run: number;
  scenario: { name: string; digest: string | null; start_save_sha1: string | null; timing: string | null };
  client: { name: string; version: string | null };
  model: { slug: string; provider_requested: string | null; reasoning_effort: string | null };
  caps: { run_hours: number | null; max_cost: number | null; max_tokens: number | null };
  sandbox: string;
}

export type BenchGrade = { manifest: BenchManifest; result: BenchResult } | { error: string };

export interface BenchPayload {
  v: number;
  playthrough: string;
  bench: BenchGrade | null;
}

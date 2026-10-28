// The shapes bg3bench-serve's /api/v1 returns (packages/bench/src/bg3bench/serve).

export type Status = "PASS" | "WARN" | "FAIL";

export interface Check {
  status: Status;
  name: string;
  detail: string;
}

export interface Preflight {
  checks: Check[];
  fails: number;
  warns: number;
  ready: boolean;
}

export type SaveStatus = "installed" | "missing" | "wrong_hash" | "stray" | "unknown" | "none";

export interface ScenarioRow {
  name: string;
  title: string | null;
  task: string | null;
  family: string | null;
  holdout: boolean;
  save: string | null;
  save_sha1: string | null;
  seed: number | null;
  difficulty: string | null;
  goal: Record<string, unknown> | null;
  cap_s: number | null;
  max_cost: number | null;
  conditions: string[];
  not_ready: string | null;
  save_status: { status: SaveStatus; detail: string };
}

export interface ProfileRow {
  name: string;
  client?: string;
  model?: string;
  effort?: string | null;
  status?: string;
  paid?: boolean;
  api_key_env?: string | null;
  key_set?: boolean | null;
  offered: boolean;
  why: string | null;
  error?: string;
}

export interface Job {
  id: string;
  model: string;
  client: string;
  scenario: string;
  condition: string;
  repeat: number;
  max_cost: number | null;
  profile: string | null;
}

export interface Plan {
  jobs: Job[];
  reserve_usd: number;
  budget: { file: string; cap: number | null; spent: number | null; error: string | null };
  fits_budget: boolean;
  needs_max_cost: string[];
  skipped: Record<string, string>;
  gaps: Record<string, string[]>;
  youtube: { verified: boolean | null; long_scenarios: string[]; warn: boolean };
}

export interface PlanForm {
  profiles: string[];
  scenarios: string[];
  condition: string;
  repeats: number;
  max_cost_usd: number | null;
  allow_unverified: boolean;
}

export interface StartForm extends PlanForm {
  record: boolean;
  submit_when_done: boolean;
}

export interface MatrixProgress {
  matrix: string;
  state?: string;
  stop_reason?: string | null;
  current?: { id?: string; scenario?: string; attempt?: number } | null;
  jobs?: number;
  done?: number;
  pending?: number;
  gave_up?: number;
  runs?: number;
  invalid_runs?: number;
  passed?: number;
  cap_usd?: number | null;
  spent_usd?: number | null;
  updated?: number;
}

export interface RunRow {
  run_id: string | null;
  dir: string;
  series: string | null;
  scenario: string | null;
  model: string | null;
  client: string | null;
  condition: string | null;
  created: string | null;
  passed: boolean | null;
  score: number | null;
  outcome: string | null;
  t_pass_s: number | null;
  wall_s: number | null;
  cost_usd: number | null;
  valid: boolean | null;
  invalid_reason: string | null;
  pilot: string[];
  luck_z: number | null;
  saves_bytes: number;
}

export interface Prefs {
  youtube_verified: boolean | null;
  record_cap_gb: number;
}

export interface SettingsView {
  dotenv: string;
  dotenv_ignored: boolean;
  keys: { name: string; masked: string | null }[];
  site_token: string | null;
  prefs: Prefs;
}

export interface About {
  bench_version: string;
  suite: string;
  not_affiliated: string;
  site: string | null;
}

/** runtime/panel/<name>.json: what Start was told, and what Submit when done did per run dir. */
export interface RunChoices {
  name: string;
  record: boolean;
  submit_when_done: boolean;
  auto: Record<string, { t: number; bundle_sha256?: string | null; accepted?: boolean | null; error?: string }>;
}

export interface RunState {
  name: string;
  pid?: number;
  started?: number;
  log?: string;
  running: boolean;
  returncode?: number | null;
  stopping: boolean;
  choices?: RunChoices | null;
}

/** A matrix play.py bench is playing that this panel didn't start (another panel, a terminal, a restart). */
export interface FoundMatrix {
  name: string;
  pid: number;
  matrix_file: string;
  running: boolean;
  stopping: boolean;
  choices: RunChoices | null;
}

export interface RunView {
  run: RunState | null;
  found: FoundMatrix | null;
  can_start: boolean;
}

/** bg3harness.doctor's report: report(doctor()). */
export interface DoctorReport {
  ready: boolean;
  fails: number;
  warns: number;
  checks: { name: string; status: "PASS" | "WARN" | "FAIL"; detail: string; fix: string }[];
}

export interface CheckResult {
  code: string;
  ok: boolean | null;
  severity?: string;
  detail?: string;
}

export interface Prepared {
  run: string;
  bundle_sha256: string;
  bundle_dir: string;
  files: { name: string; bytes: number }[];
  verdict: { accepted: boolean; flags?: string[]; checks: CheckResult[] };
  attempt: { n: number; unsubmitted_valid: number };
}

export interface Submission {
  id: string;
  status: string;
  bundle_sha256: string;
  youtube_id: string;
  flags?: string[];
  checks?: CheckResult[];
  run_url?: string;
  retry_after?: string;
}

export interface RenderState {
  run: string;
  running: boolean;
  returncode?: number | null;
  log?: string;
  video?: string | null;
  video_bytes?: number | null;
}

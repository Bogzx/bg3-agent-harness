// Pure helpers for the New run and Results screens.

import type { Plan, PlanForm, ProfileRow, RunRow, SaveStatus } from "./types";

export const YOUTUBE_LIMIT_S = 15 * 60;

export function minutes(seconds: number | null | undefined): string {
  return seconds === null || seconds === undefined ? "–" : `${Math.round(seconds / 60)} min`;
}

export function bytes(n: number): string {
  if (n < 1024) return `${n} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let v = n / 1024;
  let i = 0;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i += 1;
  }
  return `${v.toFixed(v < 10 ? 1 : 0)} ${units[i]}`;
}

export const SAVE_LABELS: Record<SaveStatus, string> = {
  installed: "installed", missing: "missing", wrong_hash: "wrong hash", stray: "stray copy", unknown: "unknown",
  none: "no save",
};

/** The YouTube note for the scenarios picked, before Start (null: nothing to say). */
export function youtubeNote(yt: Plan["youtube"]): string | null {
  if (!yt.long_scenarios.length) return null;
  const names = yt.long_scenarios.join(", ");
  if (yt.verified === true) return null;
  const ask = yt.verified === null ? " (answer the question in Settings)" : "";
  return (
    `${names}: the run can last longer than 15 minutes, and YouTube only takes longer uploads from a phone-verified ` +
    `account${ask}. Verify the account before the run, or the evidence video can't be uploaded.`
  );
}

/** Why Plan can't be pressed yet (empty: it can). */
export function formProblems(form: PlanForm, profiles: ProfileRow[]): string[] {
  const out: string[] = [];
  if (!form.profiles.length) out.push("pick a model profile");
  if (!form.scenarios.length) out.push("pick at least one scenario");
  if (!Number.isInteger(form.repeats) || form.repeats < 1 || form.repeats > 20) out.push("repeats: 1 to 20");
  const paid = profiles.some((p) => form.profiles.includes(p.name) && p.paid);
  if (paid && (form.max_cost_usd === null || !(form.max_cost_usd > 0))) out.push("a paid model needs a max cost per run");
  for (const p of profiles) {
    if (form.profiles.includes(p.name) && p.api_key_env && p.key_set === false) {
      out.push(`${p.api_key_env} is not set (Settings)`);
    }
  }
  return out;
}

/** A run's verdict as one short line. */
export function verdict(r: RunRow): string {
  if (r.valid === false) return `invalid: ${r.invalid_reason ?? "?"}`;
  if (r.passed === true) return "pass";
  if (r.passed === false) return "fail";
  if (r.score !== null) return `score ${r.score > 0 ? "+" : ""}${r.score}`;
  return r.outcome ?? "–";
}

/** Each run's attempt number within its cell (scenario, model, condition), oldest first: run dir -> [n, of]. */
export function attempts(runs: RunRow[]): Map<string, [number, number]> {
  const cells = new Map<string, RunRow[]>();
  for (const r of runs) {
    const key = `${r.scenario}\u0000${r.model}\u0000${r.condition}`;
    const list = cells.get(key) ?? [];
    list.push(r);
    cells.set(key, list);
  }
  const out = new Map<string, [number, number]>();
  for (const list of cells.values()) {
    const sorted = [...list].sort((a, b) => (a.created ?? "").localeCompare(b.created ?? "") || a.dir.localeCompare(b.dir));
    sorted.forEach((r, i) => out.set(r.dir, [i + 1, sorted.length]));
  }
  return out;
}

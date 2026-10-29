// Pure helpers for the New run, Queue and Results screens.

import type { Plan, PlanForm, Prepared, ProfileRow, RunChoices, RunRow, SaveStatus } from "./types";

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
    const sorted = [...list].sort((a, b) => (a.created ?? 0) - (b.created ?? 0) || a.dir.localeCompare(b.dir));
    sorted.forEach((r, i) => out.set(r.dir, [i + 1, sorted.length]));
  }
  return out;
}

/** Why Start can't be pressed for a plan (null: it can); the server checks the same again. */
export function startProblem(plan: Plan | null, running: boolean): string | null {
  if (running) return "a matrix is running (see Queue): stop it first";
  if (!plan) return "plan first";
  if (!plan.jobs.length) return "no job to run";
  if (plan.needs_max_cost.length) return `give a max cost for ${plan.needs_max_cost.join(", ")}`;
  if (plan.budget.error) return "no usable budget file";
  if (!plan.fits_budget) return "the reserve doesn't fit the budget";
  if (Object.values(plan.gaps).some((g) => g.length)) return "the condition can't be played exactly (a pilot)";
  return null;
}

/** Why Upload can't be pressed for a prepared bundle (null: it can). */
export function uploadProblem(p: Prepared | null, youtubeUrl: string, siteToken: boolean): string | null {
  if (!p) return "validate first";
  if (!p.verdict.accepted) return "the local validation rejects this bundle";
  if (!siteToken) return "save the site token in Settings";
  if (!youtubeId(youtubeUrl)) return "paste the YouTube URL of the evidence video";
  return null;
}

/** The 11-character id of a youtube.com/watch?v=, youtu.be/ or youtube.com/shorts/ URL (null: not one). */
export function youtubeId(url: string): string | null {
  let u: URL;
  try {
    u = new URL(url.trim());
  } catch {
    return null;
  }
  if (u.protocol !== "https:" && u.protocol !== "http:") return null;
  const host = u.hostname.toLowerCase().replace(/^(www|m)\./, "");
  let id: string | null = null;
  if (host === "youtu.be") id = u.pathname.slice(1);
  else if (host === "youtube.com" && u.pathname === "/watch") id = u.searchParams.get("v");
  else if (host === "youtube.com" && u.pathname.startsWith("/shorts/")) id = u.pathname.slice("/shorts/".length);
  return id && /^[A-Za-z0-9_-]{11}$/.test(id) ? id : null;
}

/** "Record on · Submit when done off" for a matrix's choices. */
export function choicesLine(c: RunChoices | null | undefined): string | null {
  if (!c) return null;
  return `Record ${c.record ? "on" : "off"} · Submit when done ${c.submit_when_done ? "on" : "off"}`;
}

/** What Submit when done did: "2 validated (1 accepted), 1 failed: <first error>"; null when nothing yet. */
export function autoSummary(c: RunChoices | null | undefined): string | null {
  const outcomes = Object.values(c?.auto ?? {});
  if (!outcomes.length) return null;
  const failed = outcomes.filter((o) => o.error);
  const validated = outcomes.length - failed.length;
  const accepted = outcomes.filter((o) => !o.error && o.accepted).length;
  const parts = [`${validated} validated (${accepted} accepted)`];
  if (failed.length) parts.push(`${failed.length} failed: ${failed[0]?.error ?? ""}`);
  return parts.join(", ");
}

/** Where a finished render stands with its bundle: added (the bundle hash changed), refused, or still to be added. */
export function renderNote(r: Prepared["render"], out: string | undefined): string {
  if (!r || r.dir !== out) return "adding its sync table and contact sheet to the bundle…";
  if (r.error) return `its sync table and contact sheet weren't added to the bundle: ${r.error}`;
  return "its sync table and contact sheet are in the bundle (the bundle hash changed; the run hash didn't)";
}

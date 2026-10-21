// The Launcher form: from a playthrough's config to the POST start/resume body. Pure functions only.
import type { PlaythroughConfig, SupervisorView } from "./types";

export const DIFFICULTIES = ["Explorer", "Balanced", "Tactician"] as const;
export const NAME_RE = /^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/;

export interface LaunchForm {
  name: string;
  profile: string; // "" = the config's client/model
  brief: string;
  difficulty: string;
  minutes: number;
  recording: boolean;
}

export function formFrom(cfg: PlaythroughConfig): LaunchForm {
  return {
    name: cfg.name, profile: cfg.agent.profile, brief: cfg.character.brief, difficulty: cfg.difficulty || "Explorer",
    minutes: cfg.session.minutes, recording: cfg.recording.enabled,
  };
}

/** The body for POST start/resume (supervisor.LAUNCH_KEYS), or the first problem with the form. */
export function launchBody(f: LaunchForm): { body: Record<string, unknown> } | { error: string } {
  if (!NAME_RE.test(f.name)) return { error: "Name: letters, digits, . _ - (max 64)" };
  if (!(f.minutes > 0 && f.minutes <= 600)) return { error: "Session minutes: 1-600" };
  if (!(DIFFICULTIES as readonly string[]).includes(f.difficulty)) return { error: "Pick a difficulty" };
  return {
    body: { profile: f.profile, brief: f.brief.trim(), difficulty: f.difficulty, session_minutes: f.minutes, recording: f.recording },
  };
}

export type Action = "start" | "resume" | "stop";

/** Which buttons the Launcher offers for playthrough `name` with `sessions` behind it. */
export function actions(name: string, sessions: number, sup: SupervisorView | null): Action[] {
  if (!sup) return [];
  if (sup.state !== "idle") return sup.playthrough === name && sup.state === "running" ? ["stop"] : [];
  return sessions > 0 ? ["resume"] : ["start"];
}

// Timeline rows: choices (decisions.md) and fights grouped by session, newest session first. Pure functions only.
import type { Decision, Fight, TimelinePayload } from "./types";

export type Item = { kind: "choice"; d: Decision } | { kind: "fight"; f: Fight };

export interface SessionRow {
  session: string;
  n: number;
  started: number | null;
  minutes: number | null;
  outcome: string | null;
  items: Item[];
  choices: number;
  fights: number;
  deaths: number;
}

export interface Filter {
  choices: boolean;
  fights: boolean;
  hardOnly: boolean; // fights with a downed or dead party member, or a reload
}

const num = (s: string | null | undefined): number | null => {
  const m = /^S(\d+)/.exec(s ?? "");
  return m ? Number(m[1]) : null;
};

export const sessionId = (n: number) => `S${String(n).padStart(3, "0")}`;

export function hardFight(f: Fight): boolean {
  return f.downed > 0 || f.died.length > 0 || f.reloaded;
}

export function buildRows(t: TimelinePayload, filter: Filter): SessionRow[] {
  const rows = new Map<number, SessionRow>();
  const row = (n: number): SessionRow => {
    let r = rows.get(n);
    if (!r) {
      r = { session: sessionId(n), n, started: null, minutes: null, outcome: null, items: [], choices: 0,
            fights: 0, deaths: 0 };
      rows.set(n, r);
    }
    return r;
  };
  for (const s of t.sessions) {
    const n = num(s.session);
    if (n === null) continue;
    Object.assign(row(n), { started: s.started, minutes: s.minutes, outcome: s.outcome });
  }
  if (filter.choices) {
    for (const d of t.decisions) {
      const n = num(d.session_id ?? d.session);
      if (n === null) continue;
      const r = row(n);
      r.items.push({ kind: "choice", d });
      r.choices++;
    }
  }
  if (filter.fights) {
    const fights = [...t.fights].sort((a, b) => (a.t ?? "").localeCompare(b.t ?? ""));
    for (const f of fights) {
      if (filter.hardOnly && !hardFight(f)) continue;
      const n = num(f.session);
      if (n === null) continue;
      const r = row(n);
      r.items.push({ kind: "fight", f });
      r.fights++;
      r.deaths += f.died.length;
    }
  }
  return [...rows.values()].filter((r) => r.items.length > 0 || r.started !== null).sort((a, b) => b.n - a.n);
}

export function fightLabel(f: Fight): string {
  const foes = f.enemies.slice(0, 3).join(", ") + (f.enemies.length > 3 ? ` +${f.enemies.length - 3}` : "");
  return foes || f.region || "fight";
}

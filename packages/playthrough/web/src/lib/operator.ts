// Operator messages: the Watch screen's list (the snapshot's, then the stream's `operator` rows) and the "guided"
// labels of the sessions they were on in. Pure functions only.
import { clock } from "./format";
import type { Action, OperatorMessage, Runner, SessionOperator, SessionRow } from "./types";

export const MAX_TEXT = 1000; // the server's limit (400 above it)
export const MAX_SHOWN = 50;

/** The list with `row` in it, newest first: a row replaces the one with its id. A queued row after a delivered one
 * is a redelivery (the client died before the model saw it), so the message is queued again. The stream is ordered:
 * a replayed queued row from the snapshot overlap is followed by its delivered row. */
export function mergeMessage(list: OperatorMessage[], row: OperatorMessage): OperatorMessage[] {
  const out = [row, ...list.filter((m) => m.id !== row.id)].sort((a, b) => b.t - a.t);
  return out.slice(0, MAX_SHOWN);
}

/** The list with the POST's own echo of a message just sent, unless the stream already brought that message (its
 * delivered row can come before the POST's answer). */
export function mergeSent(list: OperatorMessage[], row: OperatorMessage): OperatorMessage[] {
  return list.some((m) => m.id === row.id) ? list : mergeMessage(list, row);
}

const bare = (tool: string) => tool.replace("mcp__bg3__", "");

/** A session of this playthrough is playing now: a queued message goes out in its next tool result. */
export function sessionLive(live: boolean, r: Runner | null): boolean {
  return live && r !== null && !r.session_ended;
}

/** "queued", "queued (next session)" or "delivered 14:02:11 in S004 via travel_to". */
export function statusText(m: OperatorMessage, playing: boolean): string {
  if (m.status === "delivered" && m.delivered) {
    const d = m.delivered;
    return `delivered ${clock(d.t)} in ${d.session} via ${bare(d.tool)}`;
  }
  return playing ? "queued" : "queued (next session)";
}

/** The agent's first tool call after the one whose result carried `m`, from the session's calls (oldest first), or
 * null (not delivered, delivered in another session, or nothing since). The carrier is the first call of the
 * delivering tool logged at or after the delivery: the bridge marks it delivered while it builds that result, and
 * logs the call when the result is done. */
export function nextAction(m: OperatorMessage, actions: Action[], session: string | null): Action | null {
  const d = m.delivered;
  if (m.status !== "delivered" || !d || d.session !== session) return null;
  const carrier = actions.findIndex((a) => a.t !== null && a.t >= d.t && bare(a.tool) === bare(d.tool));
  return carrier >= 0 ? (actions[carrier + 1] ?? null) : null;
}

/** "guided (3 messages)" for a session the option was on in, else null. N counts the messages the agent got in
 * that session (delivered), not the ones posted during it: one posted between sessions reaches the next session. */
export function guidedLabel(op: SessionOperator | null | undefined): string | null {
  if (!op?.enabled) return null;
  return `guided (${op.delivered} message${op.delivered === 1 ? "" : "s"})`;
}

/** The guided sessions in `rows`, session -> "guided (N messages)". */
export function guidedSessions(rows: SessionRow[]): Record<string, string> {
  const out: Record<string, string> = {};
  for (const r of rows) {
    const label = guidedLabel(r.operator);
    if (label) out[r.session] = label;
  }
  return out;
}

/** "S004 guided (2 messages) · S005 guided (1 message)", or null when no session was guided. */
export function guidedLine(guided: Record<string, string>): string | null {
  const parts = Object.entries(guided).map(([s, label]) => `${s} ${label}`);
  return parts.length ? parts.join(" · ") : null;
}

/** The send box's problem with `text` (null: it can go). */
export function textProblem(text: string): string | null {
  if (!text.trim()) return "Write a message first";
  if (text.length > MAX_TEXT) return `At most ${MAX_TEXT} characters`;
  return null;
}

/** The send box shows on localhost (the names the server's is_local_host takes) or with a saved token; anyone else
 * reads the list only. The server still decides: a --token server refuses localhost without the token too. */
export function canSend(hostname: string, token: string | null): boolean {
  return Boolean(token) || hostname === "localhost" || hostname === "[::1]" || /^127(\.\d{1,3}){3}$/.test(hostname);
}

/** What a failed POST says to the operator. */
export function sendError(status: number, message: string): string {
  return status === 403 ? "needs the operator token" : message;
}

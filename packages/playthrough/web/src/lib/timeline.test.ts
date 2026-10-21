import { describe, expect, it } from "vitest";
import { buildRows, fightLabel, goodOutcome, hardFight } from "./timeline";
import type { Decision, Fight, TimelinePayload } from "./types";

const decision = (id: string, session: string | null): Decision => ({
  id, title: `t ${id}`, session, session_id: session, t: null, choice: [], why: [], consequences: [], expected: [],
  reversible: null, notes: [],
});
const fight = (session: string, t: string, over: Partial<Fight> = {}): Fight => ({
  session, t, region: "Grove", grade: "clean", rounds: 2, duration_s: 30, reloaded: false, enemies: ["Goblin"],
  downed: 0, died: [], ...over,
});

const payload: TimelinePayload = {
  v: 1, playthrough: "pt",
  sessions: [{ session: "S002", started: 100, minutes: 30, outcome: "success" },
             { session: "S003", started: 200, minutes: 20, outcome: "error_during_execution" }],
  decisions: [decision("D-01", "S001"), decision("D-02", "S003"), decision("D-03", null)],
  fights: [fight("S003", "2026-10-10T20:40:00", { died: ["Gale"] }), fight("S003", "2026-10-10T20:30:00"),
           fight("S002", "2026-10-10T19:00:00", { downed: 1 })],
};

describe("timeline rows", () => {
  it("groups choices and fights per session, newest session first, fights in time order", () => {
    const rows = buildRows(payload, { choices: true, fights: true, hardOnly: false });
    expect(rows.map((r) => r.session)).toEqual(["S003", "S002", "S001"]);
    const s3 = rows[0]!;
    expect(s3.items.map((i) => (i.kind === "choice" ? i.d.id : i.f.t))).toEqual([
      "D-02", "2026-10-10T20:30:00", "2026-10-10T20:40:00"]);
    expect([s3.choices, s3.fights, s3.deaths, s3.outcome]).toEqual([1, 2, 1, "error_during_execution"]);
    expect(rows[2]!.started).toBeNull(); // S001: a decision from before the recorded sessions
  });

  it("filters", () => {
    const fightsOnly = buildRows(payload, { choices: false, fights: true, hardOnly: true });
    expect(fightsOnly.flatMap((r) => r.items.map((i) => i.kind))).toEqual(["fight", "fight"]);
    const choicesOnly = buildRows(payload, { choices: true, fights: false, hardOnly: false });
    expect(choicesOnly.map((r) => [r.session, r.items.length])).toEqual([["S003", 1], ["S002", 0], ["S001", 1]]);
  });

  it("labels and hard fights", () => {
    expect(hardFight(fight("S1", "", { reloaded: true }))).toBe(true);
    expect(hardFight(fight("S1", ""))).toBe(false);
    expect(fightLabel(fight("S1", "", { enemies: ["a", "b", "c", "d", "e"] }))).toBe("a, b, c +2");
    expect(fightLabel(fight("S1", "", { enemies: [] }))).toBe("Grove");
  });

  it("a session the runner stopped at the milestone is not a failure", () => {
    expect(goodOutcome("milestone")).toBe(true);
    expect(goodOutcome("success")).toBe(true);
    expect(goodOutcome("hang")).toBe(false);
    expect(goodOutcome("error_during_execution")).toBe(false);
  });
});

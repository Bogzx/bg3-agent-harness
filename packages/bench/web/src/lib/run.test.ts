import { describe, expect, it } from "vitest";
import { attempts, bytes, formProblems, minutes, verdict, youtubeNote } from "./run";
import type { PlanForm, ProfileRow, RunRow } from "./types";

const form: PlanForm = {
  profiles: ["deepseek-flash"], scenarios: ["act1_get_item"], condition: "rules", repeats: 1, max_cost_usd: 2,
  allow_unverified: false,
};
const prof: ProfileRow = {
  name: "deepseek-flash", client: "pi", paid: true, api_key_env: "DEEPSEEK_API_KEY", key_set: true, offered: true,
  why: null,
};

function run(over: Partial<RunRow>): RunRow {
  return {
    run_id: "r", dir: "d", series: null, scenario: "s", model: "m", condition: "rules", client: "pi",
    created: null, passed: null, score: null, outcome: null, t_pass_s: null, wall_s: null, cost_usd: null,
    valid: true, invalid_reason: null, pilot: [], luck_z: null, saves_bytes: 0, ...over,
  };
}

describe("youtubeNote", () => {
  it("warns about long scenarios unless the account is verified", () => {
    expect(youtubeNote({ verified: null, long_scenarios: ["a"], warn: true })).toMatch(/^a: .*15 minutes.*Settings/);
    expect(youtubeNote({ verified: false, long_scenarios: ["a", "b"], warn: true })).toMatch(/^a, b: /);
    expect(youtubeNote({ verified: true, long_scenarios: ["a"], warn: false })).toBeNull();
    expect(youtubeNote({ verified: null, long_scenarios: [], warn: false })).toBeNull();
  });
});

describe("formProblems", () => {
  it("accepts a complete form", () => {
    expect(formProblems(form, [prof])).toEqual([]);
  });
  it("names what's missing", () => {
    expect(formProblems({ ...form, profiles: [], scenarios: [] }, [prof])).toEqual([
      "pick a model profile", "pick at least one scenario",
    ]);
    expect(formProblems({ ...form, max_cost_usd: null }, [prof])).toEqual(["a paid model needs a max cost per run"]);
    expect(formProblems({ ...form, repeats: 0 }, [prof])).toEqual(["repeats: 1 to 20"]);
    expect(formProblems(form, [{ ...prof, key_set: false }])).toEqual(["DEEPSEEK_API_KEY is not set (Settings)"]);
  });
});

describe("verdict", () => {
  it("says pass, fail, score or why invalid", () => {
    expect(verdict(run({ passed: true }))).toBe("pass");
    expect(verdict(run({ passed: false }))).toBe("fail");
    expect(verdict(run({ score: 12 }))).toBe("score +12");
    expect(verdict(run({ score: -3 }))).toBe("score -3");
    expect(verdict(run({ valid: false, invalid_reason: "operator" }))).toBe("invalid: operator");
    expect(verdict(run({ outcome: "timeout" }))).toBe("timeout");
  });
});

describe("attempts", () => {
  it("numbers the runs of a cell oldest first", () => {
    const rs = [
      run({ dir: "b", created: "2026-10-26" }),
      run({ dir: "a", created: "2026-10-25" }),
      run({ dir: "c", created: "2026-10-25", model: "other" }),
    ];
    const n = attempts(rs);
    expect(n.get("a")).toEqual([1, 2]);
    expect(n.get("b")).toEqual([2, 2]);
    expect(n.get("c")).toEqual([1, 1]);
  });
});

describe("formatters", () => {
  it("formats sizes and minutes", () => {
    expect(bytes(500)).toBe("500 B");
    expect(bytes(15 * 1024 * 1024)).toBe("15 MB");
    expect(bytes(1.5 * 1024 ** 3)).toBe("1.5 GB");
    expect(minutes(2700)).toBe("45 min");
    expect(minutes(null)).toBe("–");
  });
});

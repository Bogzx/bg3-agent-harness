import { describe, expect, it } from "vitest";
import {
  attempts, autoSummary, bytes, choicesLine, formProblems, minutes, renderNote, startProblem, uploadProblem, verdict,
  youtubeId, youtubeNote,
} from "./run";
import type { Plan, PlanForm, Prepared, ProfileRow, RunChoices, RunRow } from "./types";

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
      run({ dir: "b", created: 1792953630.85 }), // epoch seconds, as the server sends them
      run({ dir: "a", created: 999 }), // older: as text "999" would sort after "1792..."
      run({ dir: "c", created: 999, model: "other" }),
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

const okPlan: Plan = {
  jobs: [{ id: "j", model: "m", client: "pi", profile: "p", scenario: "s", condition: "rules", repeat: 1, max_cost: 1 }],
  reserve_usd: 1, budget: { file: "b", cap: 5, spent: 0, error: null }, fits_budget: true, needs_max_cost: [],
  skipped: {}, gaps: { p: [] }, youtube: { verified: true, long_scenarios: [], warn: false },
};

describe("startProblem", () => {
  it("allows a plan that fits and refuses the rest", () => {
    expect(startProblem(okPlan, false)).toBeNull();
    expect(startProblem(okPlan, true)).toMatch(/running/);
    expect(startProblem(null, false)).toBe("plan first");
    expect(startProblem({ ...okPlan, jobs: [] }, false)).toBe("no job to run");
    expect(startProblem({ ...okPlan, needs_max_cost: ["s"] }, false)).toMatch(/max cost for s/);
    expect(startProblem({ ...okPlan, budget: { ...okPlan.budget, error: "gone" } }, false)).toMatch(/budget file/);
    expect(startProblem({ ...okPlan, fits_budget: false }, false)).toMatch(/doesn't fit/);
    expect(startProblem({ ...okPlan, gaps: { p: ["memory"] } }, false)).toMatch(/pilot/);
  });
});

describe("youtubeId and uploadProblem", () => {
  const id = "dQw4w9WgXcQ";
  it("reads the three URL shapes only", () => {
    for (const u of [`https://www.youtube.com/watch?v=${id}`, `https://youtu.be/${id}`, `https://m.youtube.com/shorts/${id}`]) {
      expect(youtubeId(u)).toBe(id);
    }
    for (const u of ["", id, `https://evil.example/watch?v=${id}`, `ftp://youtu.be/${id}`, "https://youtu.be/short"]) {
      expect(youtubeId(u)).toBeNull();
    }
  });
  it("needs an accepted bundle, the site token and a video URL", () => {
    const p: Prepared = {
      run: "r", bundle_sha256: "ab", bundle_dir: "d", files: [], attempt: { n: 1, unsubmitted_valid: 0 },
      verdict: { accepted: true, checks: [] },
    };
    expect(uploadProblem(p, `https://youtu.be/${id}`, true)).toBeNull();
    expect(uploadProblem(null, "", true)).toBe("validate first");
    expect(uploadProblem({ ...p, verdict: { accepted: false, checks: [] } }, "", true)).toMatch(/rejects/);
    expect(uploadProblem(p, `https://youtu.be/${id}`, false)).toMatch(/site token/);
    expect(uploadProblem(p, "https://vimeo.com/1", true)).toMatch(/YouTube URL/);
  });
});

describe("choicesLine and autoSummary", () => {
  const c: RunChoices = { name: "m", record: true, submit_when_done: true, auto: {} };
  it("names the choices", () => {
    expect(choicesLine(c)).toBe("Record on · Submit when done on");
    expect(choicesLine({ ...c, record: false, submit_when_done: false })).toBe("Record off · Submit when done off");
    expect(choicesLine(null)).toBeNull();
  });
  it("counts what Submit when done did", () => {
    expect(autoSummary(c)).toBeNull();
    expect(autoSummary(undefined)).toBeNull();
    const auto = {
      d1: { t: 1, bundle_sha256: "ab", accepted: true },
      d2: { t: 2, bundle_sha256: "cd", accepted: false },
      d3: { t: 3, error: "pack refused it" },
    };
    expect(autoSummary({ ...c, auto })).toBe("2 validated (1 accepted), 1 failed: pack refused it");
    expect(autoSummary({ ...c, auto: { d1: auto.d1 } })).toBe("1 validated (1 accepted)");
  });
});

describe("renderNote", () => {
  it("says whether the render is in the bundle", () => {
    expect(renderNote(undefined, "/r/render-1")).toMatch(/^adding/);
    expect(renderNote({ dir: "/r/render-1", added: 1 }, "/r/render-2")).toMatch(/^adding/);
    expect(renderNote({ dir: "/r/render-1", added: 1 }, "/r/render-1")).toMatch(/are in the bundle/);
    expect(renderNote({ dir: "/r/render-1", error: "too big" }, "/r/render-1")).toMatch(/weren't added.*too big/);
  });
});

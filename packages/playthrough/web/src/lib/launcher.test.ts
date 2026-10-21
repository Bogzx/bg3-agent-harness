import { describe, expect, it } from "vitest";
import { actions, formFrom, launchBody, profileLabel } from "./launcher";
import type { ClientProfile, PlaythroughConfig, SupervisorView } from "./types";

const cfg: PlaythroughConfig = {
  name: "ilse", difficulty: "", agent: { profile: "", client: "claude", model: "opus", effort: "" },
  character: { brief: "a dwarf cleric", roleplay: "strict", name: "", constraints: "" },
  session: { minutes: 60, hours: 0, max_cost_usd: 0 }, keeper: { enabled: true, model: "", effort: "low", minutes: 5 },
  recording: { enabled: true, fps: 15, height: 720 },
};
const sup = (state: SupervisorView["state"], playthrough: string | null = null): SupervisorView => ({
  playthrough, state, detail: "", session: null,
});

describe("launcher", () => {
  it("builds the start body from the config", () => {
    const f = formFrom(cfg);
    expect(f.difficulty).toBe("Explorer");
    expect(launchBody({ ...f, profile: "codex", brief: "  a bard " })).toEqual({
      body: { profile: "codex", brief: "a bard", difficulty: "Explorer", session_minutes: 60, max_cost: 0, recording: true },
    });
    expect(launchBody({ ...f, name: "../x" })).toEqual({ error: "Name: letters, digits, . _ - (max 64)" });
    expect(launchBody({ ...f, minutes: 0 })).toHaveProperty("error");
  });

  it("labels client profiles and needs a cost cap for pi", () => {
    const pi: ClientProfile = { name: "deepseek-flash", client: "pi", model: "deepseek-flash", effort: "high",
                                provider: "deepseek", api_key_env: "DEEPSEEK_API_KEY", error: null };
    const cc: ClientProfile = { name: "cc", client: "claude", model: "claude-opus-5-5", effort: "", provider: null,
                                api_key_env: null, error: null };
    const bad: ClientProfile = { ...cc, name: "bad", client: null, model: null, error: "bad.toml: model must be a non-empty string" };
    expect(profileLabel(pi)).toBe("deepseek-flash: pi · deepseek-flash via deepseek · high");
    expect(profileLabel(cc)).toBe("cc: claude · claude-opus-5-5");
    expect(profileLabel(bad)).toBe("bad: can't be used (bad.toml: model must be a non-empty string)");
    const f = formFrom(cfg);
    const all = [pi, cc, bad];
    expect(launchBody({ ...f, profile: "deepseek-flash" }, all)).toEqual({ error: "A pi profile needs a cost cap (USD per session)" });
    expect(launchBody({ ...f, profile: "deepseek-flash", maxCost: 2 }, all)).toHaveProperty("body.max_cost", 2);
    expect(launchBody({ ...f, profile: "cc" }, all)).toHaveProperty("body.profile", "cc");
    expect(launchBody({ ...f, profile: "bad" }, all)).toHaveProperty("error");
    expect(launchBody({ ...f, maxCost: -1 }, all)).toHaveProperty("error");
  });

  it("offers start, resume or stop", () => {
    expect(actions("ilse", 0, sup("idle"))).toEqual(["start"]);
    expect(actions("ilse", 3, sup("idle"))).toEqual(["resume"]);
    expect(actions("ilse", 3, sup("running", "ilse"))).toEqual(["stop"]);
    expect(actions("other", 0, sup("running", "ilse"))).toEqual([]);
    expect(actions("ilse", 3, sup("stopping", "ilse"))).toEqual([]);
    expect(actions("ilse", 0, null)).toEqual([]);
  });
});

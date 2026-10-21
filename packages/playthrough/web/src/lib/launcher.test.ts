import { describe, expect, it } from "vitest";
import { actions, formFrom, launchBody } from "./launcher";
import type { PlaythroughConfig, SupervisorView } from "./types";

const cfg: PlaythroughConfig = {
  name: "ilse", difficulty: "", agent: { profile: "", client: "claude", model: "opus", effort: "" },
  character: { brief: "a dwarf cleric", roleplay: "strict", name: "", constraints: "" },
  session: { minutes: 60, hours: 0 }, keeper: { enabled: true, model: "", effort: "low", minutes: 5 },
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
      body: { profile: "codex", brief: "a bard", difficulty: "Explorer", session_minutes: 60, recording: true },
    });
    expect(launchBody({ ...f, name: "../x" })).toEqual({ error: "Name: letters, digits, . _ - (max 64)" });
    expect(launchBody({ ...f, minutes: 0 })).toHaveProperty("error");
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

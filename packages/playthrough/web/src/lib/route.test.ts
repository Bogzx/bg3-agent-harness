import { describe, expect, it } from "vitest";
import { href, parseRoute } from "./route";

describe("routes", () => {
  it("round-trips screens, playthroughs and nested files", () => {
    for (const r of [
      { screen: "watch" as const, pt: "claude-run-2", file: null },
      { screen: "memory" as const, pt: "a b", file: "sessions/S096.md" },
      { screen: "timeline" as const, pt: "x", file: null },
    ]) {
      expect(parseRoute(href(r))).toEqual(r);
    }
  });

  it("defaults to watch", () => {
    expect(parseRoute("")).toEqual({ screen: "watch", pt: null, file: null });
    expect(parseRoute("#/nope/pt")).toEqual({ screen: "watch", pt: "pt", file: null });
  });
});

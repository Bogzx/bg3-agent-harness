import { describe, expect, it } from "vitest";
import { parseRoute } from "./route";

describe("parseRoute", () => {
  it("reads the screen", () => {
    expect(parseRoute("#/results")).toBe("results");
    expect(parseRoute("#new")).toBe("new");
    expect(parseRoute("#/doctor")).toBe("doctor");
  });
  it("defaults to preflight", () => {
    expect(parseRoute("")).toBe("preflight");
    expect(parseRoute("#/nope")).toBe("preflight");
  });
});

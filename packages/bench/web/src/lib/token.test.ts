import { describe, expect, it } from "vitest";
import { DEV_KEY, resolveToken, SLOT } from "./token";

function store(init: Record<string, string> = {}) {
  const m = new Map(Object.entries(init));
  return { getItem: (k: string) => m.get(k) ?? null, setItem: (k: string, v: string) => void m.set(k, v), m };
}

describe("resolveToken", () => {
  it("takes the served meta tag first", () => {
    expect(resolveToken("abc", "?token=zzz", store())).toBe("abc");
  });
  it("falls back to ?token= under the dev server and keeps it", () => {
    const s = store();
    expect(resolveToken(SLOT, "?token=devtok", s)).toBe("devtok");
    expect(s.m.get(DEV_KEY)).toBe("devtok");
    expect(resolveToken(SLOT, "", s)).toBe("devtok");
  });
  it("is null with nothing to go on", () => {
    expect(resolveToken(null, "", store())).toBeNull();
    expect(resolveToken(SLOT, "", store())).toBeNull();
  });
});

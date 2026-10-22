import { describe, expect, it } from "vitest";
import { TOKEN_KEY, authHeaders, takeToken } from "./auth";

function store(init: Record<string, string> = {}) {
  const m = new Map(Object.entries(init));
  return { getItem: (k: string) => m.get(k) ?? null, setItem: (k: string, v: string) => void m.set(k, v), m };
}

describe("token", () => {
  it("takes ?token= out of the query and keeps it for the writes", () => {
    const s = store();
    expect(takeToken("?token=s3%20cret&x=1", s)).toBe("?x=1");
    expect(s.m.get(TOKEN_KEY)).toBe("s3 cret");
    expect(authHeaders(s)).toEqual({ Authorization: "Bearer s3 cret" });
    expect(takeToken("?token=abc", s)).toBe("");
    expect(authHeaders(s)).toEqual({ Authorization: "Bearer abc" });
  });

  it("leaves the query and the saved token alone without one", () => {
    const s = store({ [TOKEN_KEY]: "old" });
    expect(takeToken("", s)).toBeNull();
    expect(takeToken("?x=1", s)).toBeNull();
    expect(takeToken("?token=", s)).toBe("");
    expect(authHeaders(s)).toEqual({ Authorization: "Bearer old" });
  });

  it("sends no header without a token", () => {
    expect(authHeaders(store())).toEqual({});
  });
});

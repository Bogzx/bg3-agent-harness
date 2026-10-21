import { describe, expect, it } from "vitest";
import { clip, parseOverlay, PANELS } from "./overlay";

describe("overlay options", () => {
  it("parses panels, playthrough and theme", () => {
    const all = parseOverlay("");
    expect([...all.panels]).toEqual([...PANELS]);
    expect(all.pt).toBeNull();
    expect(all.theme).toBe("dark");
    const o = parseOverlay("?pt=ilse&panels=party,%20goal,bogus&theme=light");
    expect([...o.panels]).toEqual(["party", "goal"]);
    expect(o.pt).toBe("ilse");
    expect(o.theme).toBe("light");
  });

  it("clips on word boundaries", () => {
    expect(clip("short  line\n", 20)).toBe("short line");
    expect(clip("the goblin camp is north of the village", 20)).toBe("the goblin camp is…");
  });
});

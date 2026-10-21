import { describe, expect, it } from "vitest";
import { applyPosition, emptyPositions, GAP_S, project } from "./breadcrumb";
import type { PositionRow, Positions } from "./types";

const row = (t: number, x: number, z: number, level = "A"): PositionRow => ({
  t, level, region: level, party: [{ name: "Gale", x: x - 1, y: 0, z }, { name: "Tav", x, y: 0, z, active: true }],
});

describe("breadcrumb", () => {
  it("extends, splits at a gap and follows a level change", () => {
    let p = applyPosition(emptyPositions(), row(10, 0, 0));
    p = applyPosition(p, row(12, 5, 5));
    p = applyPosition(p, row(12, 9, 9)); // not newer: ignored
    expect(p.segments).toEqual([[[10, 0, 0], [12, 5, 5]]]);
    p = applyPosition(p, row(12 + GAP_S + 1, 6, 6));
    expect(p.segments.length).toBe(2);
    expect(p.party.find((m) => m.active)?.name).toBe("Tav");
    p = applyPosition(p, row(500, 100, 100, "B"));
    expect(p.level).toBe("B");
    expect(p.segments).toEqual([[[500, 100, 100]]]);
    expect(p.levels.map((l) => [l.level, l.samples])).toEqual([["A", 3], ["B", 1]]);
    expect(p.bounds).toEqual({ min_x: 100, max_x: 100, min_z: 100, max_z: 100 });
  });

  it("projects north-up with one scale and the party dots", () => {
    const p: Positions = {
      level: "A", levels: [], t: 2, party: [{ name: "Tav", x: 100, z: 0, active: true }],
      segments: [[[1, 0, 0], [2, 100, 50]]], bounds: { min_x: 0, max_x: 100, min_z: 0, max_z: 50 },
    };
    const plot = project(p, 220, 120, 10);
    // 200 x 100 usable, span 100 x 50 m: 2 px/m, centred
    expect(plot.lines).toEqual(["10,110 210,10"]);
    expect(plot.dots).toEqual([{ name: "Tav", cx: 210, cy: 110, active: true }]);
    expect(plot.start).toEqual({ cx: 10, cy: 110 });
    expect(plot.scale_m).toBe(25);
    expect(project(emptyPositions(), 100, 100).lines).toEqual([]);
  });
});
